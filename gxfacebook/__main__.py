import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from sanic import Sanic, response
from yt_dlp import YoutubeDL

from .cookies import get_cookie_file, get_httpx_cookies
from .db import (
    extract_cdn_expiration,
    generate_media_id,
    get_media,
    get_media_by_path,
    init_db,
    is_expired,
    log_request,
    save_media,
)
from .gallery import extract_all_images, render_multi_image_tags
from .ip_logger import get_client_ip, log_request_ip

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

# Create app
app = Sanic("FacebookReelDownloader")

# Configure loggers
access_logger = logging.getLogger("access_logger")
error_logger = logging.getLogger("error_logger")

formatter = logging.Formatter("%(asctime)s - %(message)s")

# File loggers
access_handler = logging.FileHandler("access.log")
access_handler.setFormatter(formatter)
access_logger.addHandler(access_handler)

error_handler = logging.FileHandler("errors.log")
error_handler.setFormatter(formatter)
error_logger.addHandler(error_handler)

# Stdout / stderr stream handlers for journalctl & console visibility
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setFormatter(formatter)
access_logger.addHandler(stdout_handler)

stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setFormatter(formatter)
error_logger.addHandler(stderr_handler)

access_logger.setLevel(logging.INFO)
error_logger.setLevel(logging.ERROR)


class YDLLogger:
    """Silent logger for yt-dlp to avoid console stderr spam on unavailable posts."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


# Middleware to log every request
@app.middleware("request")
async def log_facebook_requests(request):
    ip = get_client_ip(request)
    method = request.method
    path = request.path

    # Log to ips.log using append flag
    log_request_ip(request)

    access_logger.info(f"{ip} - {method} {path}")


# Startup listener to initialize SQLite database
@app.before_server_start
async def setup_db(app, *args, **kwargs):
    await init_db()


def get_base_url(request=None) -> str:
    """Derive base URL from request, respecting forwarded reverse proxy headers."""
    if request is not None:
        proto = request.headers.get("x-forwarded-proto", request.scheme or "http").split(",")[0].strip()
        host = request.headers.get("x-forwarded-host", request.host or "localhost").split(",")[0].strip()
        return f"{proto}://{host}"
    return ""


# Helper function to format numbers with K/M suffixes
def format_number(num):
    if num is None:
        return "0"

    num = int(num)
    if num >= 1_000_000:
        val = f"{num / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{val}M"
    elif num >= 1_000:
        val = f"{num / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{val}K"
    else:
        return str(num)


def extract_clean_image(meta_image: str | None, page_html: str = "") -> str | None:
    """Extract clean CDN photo URL directly, without Facebook watermark."""
    if meta_image and "scontent" in meta_image and "lookaside.fbsbx.com" not in meta_image:
        return meta_image

    if page_html:
        unescaped = page_html.replace(r"\/", "/")
        matches = re.findall(r'https://scontent[^\s"\'<>]+', unescaped)
        for m in matches:
            m = html.unescape(m)
            if any(b in m.lower() for b in ["lookaside", ".ico", "favicon", "rsrc.php", "16x16", "32x32"]):
                continue
            return m
    return None


async def fetch_facebook_metadata(fb_url: str) -> dict:
    """Fetch OpenGraph metadata from Facebook page with Discordbot UA and optional cookies."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    cookies = get_httpx_cookies()
    try:
        async with httpx.AsyncClient(headers=headers, cookies=cookies or None, follow_redirects=True, timeout=10.0) as client:
            resp = await client.get(fb_url)
            html_text = resp.text
            soup = BeautifulSoup(html_text, "html.parser")
            meta = {}
            for tag in soup.find_all("meta"):
                prop = tag.get("property") or tag.get("name")
                content = tag.get("content")
                if prop and content:
                    meta[prop.lower()] = content

            all_images = extract_all_images(html_text, max_images=4)
            raw_img = meta.get("og:image") or meta.get("twitter:image")
            clean_img = extract_clean_image(raw_img, html_text)
            if not clean_img and all_images:
                clean_img = all_images[0]
            elif clean_img and clean_img not in all_images:
                all_images.insert(0, clean_img)

            raw_title = (
                meta.get("og:title")
                or meta.get("twitter:title")
                or (soup.title.string if soup.title else "")
                or ""
            ).strip()
            title = raw_title
            for suffix in [" | Facebook", " - Facebook"]:
                if title.endswith(suffix):
                    title = title[:-len(suffix)].strip()
                    break
            if not title:
                title = "Facebook Post"

            desc = meta.get("og:description") or meta.get("twitter:description") or meta.get("description") or ""

            return {
                "title": title,
                "description": desc,
                "image": clean_img,
                "images": all_images,
                "url": str(resp.url),
                "has_video": bool(meta.get("og:video") or meta.get("og:video:url") or meta.get("og:video:secure_url")),
            }
    except Exception as e:
        error_logger.warning(f"[Metadata Fetch Error] {fb_url} - {e}")
        return {}


def is_valid_image_post(meta: dict, is_video_request: bool) -> bool:
    """Check if the fetched metadata represents a valid image post."""
    if is_video_request:
        return False

    img = meta.get("image")
    if not img or "lookaside.fbsbx.com" in img:
        return False

    title = (meta.get("title") or "").strip().lower()
    blocked_titles = [
        "log in or sign up to view",
        "log in to facebook",
        "log into facebook",
        "page not found",
        "content not found",
        "error",
    ]
    if any(title == b or title.startswith(b) for b in blocked_titles):
        return False

    return True


def build_stats_string(data: dict = None, is_video: bool = False, photo_count: int = 0) -> str:
    """Format like, share, comment, and view metrics (e.g. 💬 12 ❤️ 1.5K 🔁 34 👁️ 50K)."""
    if not data:
        data = {}

    title = str(data.get("title") or "")
    desc = str(data.get("description") or "")
    combined_text = f"{title} {desc}"

    # 1. Comments
    comment_count = data.get("comment_count")
    if comment_count is not None:
        c_str = format_number(comment_count)
    else:
        c_match = re.search(r'(\d+(?:\.\d+)?[KM]?)\s*comments?', combined_text, re.IGNORECASE)
        c_str = c_match.group(1) if c_match else "0"

    # 2. Reactions / Likes
    reaction_count = data.get("like_count") or data.get("reaction_count")
    if reaction_count is not None:
        r_str = format_number(reaction_count)
    else:
        r_match = re.search(r'(\d+(?:\.\d+)?[KM]?)\s*(?:reactions?|likes?)', combined_text, re.IGNORECASE)
        r_str = r_match.group(1) if r_match else "0"

    # 3. Shares
    share_count = data.get("share_count") or data.get("repost_count")
    if share_count is not None:
        s_str = format_number(share_count)
    else:
        s_match = re.search(r'(\d+(?:\.\d+)?[KM]?)\s*shares?', combined_text, re.IGNORECASE)
        s_str = s_match.group(1) if s_match else None

    # 4. Views
    view_count = data.get("view_count")
    if view_count is not None:
        v_str = format_number(view_count)
    else:
        v_match = re.search(r'(\d+(?:\.\d+)?[KM]?)\s*views?', combined_text, re.IGNORECASE)
        v_str = v_match.group(1) if v_match else None

    parts = [f"💬 {c_str}", f"❤️ {r_str}"]
    if s_str and s_str != "0":
        parts.append(f"🔁 {s_str}")
    if v_str and v_str != "0":
        parts.append(f"👁️ {v_str}")
    elif is_video:
        parts.append(f"👁️ {v_str or '0'}")

    if photo_count > 1:
        parts.append(f"· {photo_count} Photos")

    return " ".join(parts)


# Video embed HTML generator
def render_embed(
    fb_path: str,
    video_url: str,
    video_info: dict = None,
    media_id: str | None = None,
    base_url: str | None = None,
    request=None,
):
    full_url = f"https://www.facebook.com/{fb_path}"

    if request is not None and not base_url:
        base_url = get_base_url(request)

    if media_id:
        stream_url = f"{base_url.rstrip('/')}/media/{media_id}" if base_url else f"/media/{media_id}"
    elif base_url:
        mid = generate_media_id(fb_path)
        stream_url = f"{base_url.rstrip('/')}/media/{mid}"
    else:
        stream_url = video_url

    og_title = build_stats_string(video_info, is_video=True)

    # Use clean video thumbnail instead of hardcoded Facebook icon
    thumbnail_url = (video_info or {}).get("thumbnail")
    if not thumbnail_url or "lookaside.fbsbx.com" in thumbnail_url:
        thumbnail_url = "https://static.xx.fbcdn.net/rsrc.php/yo/r/iRmz9lCMBD2.ico"

    width = (video_info or {}).get("width") or 720
    height = (video_info or {}).get("height") or 1280

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="theme-color" content="#007FFF"/>
<meta property="og:url" content="{full_url}"/>
<meta property="og:title" content="Facebook Reel"/>
<meta property="og:description" content="FacebookFix -- FB embed fix"/>
<meta http-equiv="refresh" content="0; url={full_url}"/>
<meta name="twitter:card" content="player"/>
<meta name="twitter:title" content="Facebook Reel"/>
<meta name="twitter:image" content="{thumbnail_url}"/>
<meta name="twitter:player:width" content="{width}"/>
<meta name="twitter:player:height" content="{height}"/>
<meta name="twitter:player:stream" content="{stream_url}"/>
<meta name="twitter:player:stream:content_type" content="video/mp4"/>
<meta property="og:site_name" content="{og_title}"/>
<meta property="og:image" content="{thumbnail_url}"/>
<meta property="og:video" content="{stream_url}"/>
<meta property="og:video:secure_url" content="{stream_url}"/>
<meta property="og:video:type" content="video/mp4"/>
<meta property="og:video:width" content="{width}"/>
<meta property="og:video:height" content="{height}"/>
<link rel="alternate" href="{full_url}" type="application/json+oembed" title="{og_title}"/>
</head>
<body>
Redirecting you to the post in a moment.
<a href="{full_url}">Or click here.</a>
</body>
</html>"""


# Image embed HTML generator
def render_image_embed(
    fb_path: str,
    image_url: str,
    title: str = None,
    description: str = None,
    images: list[str] = None,
    meta: dict = None,
):
    full_url = f"https://www.facebook.com/{fb_path}"
    clean_title = (title or "Facebook Post").strip()
    clean_desc = (description or "").strip()
    if not clean_desc:
        clean_desc = "FacebookFix -- FB embed fix"

    escaped_title = html.escape(clean_title, quote=True)
    escaped_desc = html.escape(clean_desc, quote=True)

    img_list = images if (images and len(images) > 0) else ([image_url] if image_url else [])
    image_tags = render_multi_image_tags(img_list)

    stats_info = dict(meta or {})
    if title and not stats_info.get("title"):
        stats_info["title"] = title
    if description and not stats_info.get("description"):
        stats_info["description"] = description

    og_title = build_stats_string(stats_info, is_video=False, photo_count=len(img_list))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="theme-color" content="#007FFF"/>
<meta property="og:url" content="{full_url}"/>
<meta property="og:site_name" content="{og_title}"/>
<meta property="og:type" content="article"/>
<meta property="og:title" content="{escaped_title}"/>
<meta property="og:description" content="{escaped_desc}"/>
{image_tags}
<meta http-equiv="refresh" content="0; url={full_url}"/>
<link rel="alternate" href="{full_url}" type="application/json+oembed" title="{og_title}"/>
</head>
<body>
Redirecting you to the post in a moment.
<a href="{full_url}">Or click here.</a>
</body>
</html>"""


# Error embed HTML generator
def render_error_embed(
    fb_path: str,
    title: str = "⚠️ Content Unavailable",
    description: str = "This video or post is private, age-restricted (18+), or has been removed.",
):
    full_url = f"https://www.facebook.com/{fb_path}"
    escaped_title = html.escape(title, quote=True)
    escaped_desc = html.escape(description, quote=True)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="theme-color" content="#ED4245"/>
<meta property="og:url" content="{full_url}"/>
<meta property="og:site_name" content="Facebook"/>
<meta property="og:title" content="{escaped_title}"/>
<meta property="og:description" content="{escaped_desc}"/>
<meta name="twitter:card" content="summary"/>
<meta name="twitter:title" content="{escaped_title}"/>
<meta name="twitter:description" content="{escaped_desc}"/>
<meta http-equiv="refresh" content="0; url={full_url}"/>
</head>
<body>
<p>{escaped_title}</p>
<p>{escaped_desc}</p>
<p>Redirecting you to Facebook in a moment. <a href="{full_url}">Click here if not redirected.</a></p>
</body>
</html>"""


# Video downloader with silent logger to prevent stderr spam
def fbmatch(url: str):
    ydl_opts = {
        "format": "hd",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "forceurl": True,
        "noplaylist": True,
        "logger": YDLLogger(),
    }
    cookie_file = get_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return info.get("url"), info
        except Exception as e:
            error_logger.warning(f"[yt-dlp] {url} - {e}")
            return None, None


# Validate path
def is_valid_path(path: str) -> bool:
    return (
        bool(re.fullmatch(r"[\w\-/]+", path)) and ".." not in path and "//" not in path
    )


def is_video_path_request(path: str) -> bool:
    lower = path.lower()
    return bool(
        lower.startswith(("share/r/", "share/v/", "reel/", "reels/", "watch", "videos/"))
        or "/reel/" in lower
        or "/videos/" in lower
        or "/watch" in lower
    )


# Static files
app.static("/static", "static", name="static")
app.static("/style.css", "static/style.css", name="style_css")

# Homepage
@app.get("/favicon.ico")
async def favicon(request):
    return await response.file("static/favicon.ico")


@app.get("/")
async def homepage(request):
    return await response.file("static/index.html")


# Media proxy route
@app.get("/media/<media_id:str>")
async def media_proxy(request, media_id: str):
    media = await get_media(media_id)

    if not media:
        media = await get_media_by_path(media_id)

    if not media:
        # Try resolving if media_id matches a path
        cleaned = media_id.strip("/")
        if is_valid_path(cleaned):
            fb_url = f"https://www.facebook.com/{quote(cleaned)}"
            if is_video_path_request(cleaned):
                video_url, vidinfo = await asyncio.to_thread(fbmatch, fb_url)
                if video_url:
                    extra = {
                        "comment_count": (vidinfo or {}).get("comment_count"),
                    }
                    media = await save_media(
                        media_id=media_id,
                        fb_path=cleaned,
                        media_type="video",
                        hot_url=video_url,
                        thumbnail_url=(vidinfo or {}).get("thumbnail"),
                        title=(vidinfo or {}).get("title"),
                        width=(vidinfo or {}).get("width"),
                        height=(vidinfo or {}).get("height"),
                        extra_data=extra,
                        expires_at=extract_cdn_expiration(video_url),
                    )
            else:
                meta = await fetch_facebook_metadata(fb_url)
                if meta.get("image"):
                    media = await save_media(
                        media_id=media_id,
                        fb_path=cleaned,
                        media_type="image",
                        hot_url=meta["image"],
                        thumbnail_url=meta["image"],
                        title=meta.get("title"),
                        description=meta.get("description"),
                        expires_at=extract_cdn_expiration(meta["image"]),
                    )

    if not media:
        return response.text("Media not found", status=404)

    # If media found and not expired, redirect to hot CDN URL
    if not is_expired(media.get("expires_at")):
        return response.redirect(media["hot_url"], status=302)

    # Media is expired -> re-fetch fresh hot link
    fb_path = media.get("fb_path")
    fb_url = f"https://www.facebook.com/{quote(fb_path)}"
    media_type = media.get("media_type", "video")

    fresh_url = None
    vidinfo = None
    meta = None

    if media_type == "video":
        fresh_url, vidinfo = await asyncio.to_thread(fbmatch, fb_url)
    elif media_type in ("image", "album"):
        meta = await fetch_facebook_metadata(fb_url)
        fresh_url = meta.get("image")

    if fresh_url:
        fresh_expires_at = extract_cdn_expiration(fresh_url)
        thumb = (vidinfo or {}).get("thumbnail") if vidinfo else (meta.get("image") if meta else None)
        title = (vidinfo or {}).get("title") if vidinfo else (meta.get("title") if meta else None)
        width = (vidinfo or {}).get("width") if vidinfo else None
        height = (vidinfo or {}).get("height") if vidinfo else None

        await save_media(
            media_id=media["media_id"],
            fb_path=fb_path,
            media_type=media_type,
            hot_url=fresh_url,
            thumbnail_url=thumb,
            title=title,
            width=width,
            height=height,
            expires_at=fresh_expires_at,
            extra_data=media.get("extra_data"),
        )
        return response.redirect(fresh_url, status=302)

    # Fallback to existing hot_url if re-fetch fails
    return response.redirect(media["hot_url"], status=302)


# Embed route
@app.get("/<path:path>")
async def embed_facebook_content(request, path):
    cleaned_path = path.strip("/")
    client_ip = get_client_ip(request)

    if not is_valid_path(cleaned_path):
        error_logger.error(f"[Invalid Path] {client_ip} tried '{path}'")
        return response.text("Invalid Facebook path", status=400)

    base_url = get_base_url(request)

    # Check database cache first
    cached = await get_media_by_path(cleaned_path)
    if cached and not is_expired(cached.get("expires_at")):
        if cached["media_type"] == "video":
            vidinfo = {
                "title": cached.get("title") or "",
                "thumbnail": cached.get("thumbnail_url"),
                "width": cached.get("width"),
                "height": cached.get("height"),
            }
            if cached.get("extra_data"):
                try:
                    extra = json.loads(cached["extra_data"])
                    if isinstance(extra, dict):
                        vidinfo.update(extra)
                except Exception:
                    pass
            print(f"[OK - Cached Video] {client_ip} requested /{path}", flush=True)
            await log_request(
                fb_path=cleaned_path,
                media_id=cached.get("media_id"),
                ip_address=client_ip,
                request_timestamp=time.time(),
                like_count=cached.get("like_count") if cached.get("like_count") is not None else vidinfo.get("like_count"),
                comment_count=cached.get("comment_count") if cached.get("comment_count") is not None else vidinfo.get("comment_count"),
                view_count=cached.get("view_count") if cached.get("view_count") is not None else vidinfo.get("view_count"),
            )
            return response.html(
                render_embed(
                    cleaned_path,
                    cached["hot_url"],
                    vidinfo,
                    media_id=cached["media_id"],
                    base_url=base_url,
                ),
                status=200,
            )
        elif cached["media_type"] in ("image", "album"):
            print(f"[OK - Cached Image] {client_ip} requested /{path}", flush=True)
            cached_images = None
            cached_meta = {}
            if cached.get("extra_data"):
                try:
                    extra = json.loads(cached["extra_data"])
                    if isinstance(extra, dict):
                        cached_meta = extra
                        cached_images = extra.get("images")
                except Exception:
                    pass
            await log_request(
                fb_path=cleaned_path,
                media_id=cached.get("media_id"),
                ip_address=client_ip,
                request_timestamp=time.time(),
                like_count=cached.get("like_count") if cached.get("like_count") is not None else (cached_meta.get("like_count") or cached_meta.get("reaction_count")),
                comment_count=cached.get("comment_count") if cached.get("comment_count") is not None else cached_meta.get("comment_count"),
                view_count=cached.get("view_count") if cached.get("view_count") is not None else cached_meta.get("view_count"),
            )
            return response.html(
                render_image_embed(
                    cleaned_path,
                    cached["hot_url"],
                    title=cached.get("title"),
                    description=cached.get("description"),
                    images=cached_images,
                    meta=cached_meta,
                ),
                status=200,
            )

    encoded_path = quote(cleaned_path)
    fb_url = f"https://www.facebook.com/{encoded_path}"
    if request.query_string:
        fb_url += f"?{request.query_string}"

    is_video = is_video_path_request(cleaned_path)

    if is_video:
        # Reel or video request: fetch video stream via yt-dlp
        video_url, vidinfo = await asyncio.to_thread(fbmatch, fb_url)
        if video_url:
            print(f"[OK - Video] {client_ip} requested /{path}", flush=True)
            media_id = generate_media_id(cleaned_path)
            expires_at = extract_cdn_expiration(video_url)
            extra = {
                "comment_count": (vidinfo or {}).get("comment_count"),
                "like_count": (vidinfo or {}).get("like_count"),
                "repost_count": (vidinfo or {}).get("repost_count"),
                "view_count": (vidinfo or {}).get("view_count"),
            }
            await save_media(
                media_id=media_id,
                fb_path=cleaned_path,
                media_type="video",
                hot_url=video_url,
                thumbnail_url=(vidinfo or {}).get("thumbnail"),
                title=(vidinfo or {}).get("title"),
                width=(vidinfo or {}).get("width"),
                height=(vidinfo or {}).get("height"),
                extra_data=extra,
                expires_at=expires_at,
                like_count=(vidinfo or {}).get("like_count"),
                comment_count=(vidinfo or {}).get("comment_count"),
                view_count=(vidinfo or {}).get("view_count"),
                ip_address=client_ip,
                request_timestamp=time.time(),
            )
            return response.html(
                render_embed(
                    cleaned_path,
                    video_url,
                    vidinfo,
                    media_id=media_id,
                    base_url=base_url,
                ),
                status=200,
            )

        # Video is private, 18+, deleted, or unavailable
        error_logger.warning(f"[Video Unavailable] {client_ip} failed on {fb_url}")
        return response.html(render_error_embed(cleaned_path), status=200)

    # Non-video path (e.g. share/p/..., posts, photos):
    # Fetch OpenGraph metadata
    meta = await fetch_facebook_metadata(fb_url)

    # Check if it's a valid image post
    if is_valid_image_post(meta, is_video_request=False):
        print(f"[OK - Image] {client_ip} requested /{path}", flush=True)
        media_id = generate_media_id(cleaned_path)
        expires_at = extract_cdn_expiration(meta["image"])
        all_imgs = meta.get("images") or ([meta["image"]] if meta.get("image") else [])
        extra = {
            "images": all_imgs,
            "comment_count": meta.get("comment_count"),
            "like_count": meta.get("like_count") or meta.get("reaction_count"),
            "share_count": meta.get("share_count"),
        }
        await save_media(
            media_id=media_id,
            fb_path=cleaned_path,
            media_type="album" if len(all_imgs) > 1 else "image",
            hot_url=meta["image"],
            thumbnail_url=meta["image"],
            title=meta.get("title"),
            description=meta.get("description"),
            extra_data=extra,
            expires_at=expires_at,
            like_count=meta.get("like_count") or meta.get("reaction_count"),
            comment_count=meta.get("comment_count"),
            ip_address=client_ip,
            request_timestamp=time.time(),
        )
        return response.html(
            render_image_embed(
                cleaned_path,
                meta["image"],
                title=meta.get("title"),
                description=meta.get("description"),
                images=all_imgs,
                meta=meta,
            ),
            status=200,
        )

    # If meta indicates video or image post check failed, try yt-dlp fallback
    video_url, vidinfo = await asyncio.to_thread(fbmatch, fb_url)
    if video_url:
        print(f"[OK - Video Post] {client_ip} requested /{path}", flush=True)
        media_id = generate_media_id(cleaned_path)
        expires_at = extract_cdn_expiration(video_url)
        extra = {
            "comment_count": (vidinfo or {}).get("comment_count"),
            "like_count": (vidinfo or {}).get("like_count"),
            "repost_count": (vidinfo or {}).get("repost_count"),
            "view_count": (vidinfo or {}).get("view_count"),
        }
        await save_media(
            media_id=media_id,
            fb_path=cleaned_path,
            media_type="video",
            hot_url=video_url,
            thumbnail_url=(vidinfo or {}).get("thumbnail"),
            title=(vidinfo or {}).get("title"),
            width=(vidinfo or {}).get("width"),
            height=(vidinfo or {}).get("height"),
            extra_data=extra,
            expires_at=expires_at,
            like_count=(vidinfo or {}).get("like_count"),
            comment_count=(vidinfo or {}).get("comment_count"),
            view_count=(vidinfo or {}).get("view_count"),
            ip_address=client_ip,
            request_timestamp=time.time(),
        )
        return response.html(
            render_embed(
                cleaned_path,
                video_url,
                vidinfo,
                media_id=media_id,
                base_url=base_url,
            ),
            status=200,
        )

    # Content is unavailable / private / deleted
    error_logger.warning(f"[Content Unavailable] {client_ip} on {fb_url}")
    return response.html(render_error_embed(cleaned_path), status=200)
