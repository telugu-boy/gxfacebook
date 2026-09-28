"""Multi-image carousel extraction and rendering for Facebook posts.

Extracts high-resolution post photo URLs from Facebook HTML/DOM
and generates Discord and Twitter OpenGraph metadata tags.
"""

from __future__ import annotations

import html
import posixpath
import re
import urllib.parse
from bs4 import BeautifulSoup


def clean_image_url(url: str) -> str:
    """Clean and unescape Facebook image URL."""
    if not url:
        return ""
    # Unescape escaped slashes from JSON
    u = url.replace(r"\/", "/")
    # Unescape unicode ampersands and literal backslashes
    u = u.replace(r"\\u0026", "&").replace(r"\u0026", "&")
    # Unescape HTML entities
    u = html.unescape(u)
    while "&amp;" in u:
        u = u.replace("&amp;", "&")
    # Strip whitespace and trailing syntax delimiters
    return u.strip().rstrip("\"'();\\,>")


def is_valid_gallery_image(url: str) -> bool:
    """Determine whether a URL is a valid full-resolution post photo.

    Filters out:
    - Lookaside URLs (watermarks/crawler low-res)
    - Icons, emoticons, favicons, static resources
    - Profile avatars and small thumbnail dimensions
    """
    if not url or not isinstance(url, str):
        return False

    url_lower = url.lower()

    # Reject lookaside watermarked URLs
    if "lookaside" in url_lower or "lookaside.fbsbx.com" in url_lower:
        return False

    # Check valid Facebook CDN domain
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
    except Exception:
        return False

    if not ("scontent" in host and "fbcdn.net" in host):
        return False

    # Filter out icons, emoticons, favicons, static scripts/css
    ignored_patterns = (
        ".ico",
        "favicon",
        "rsrc.php",
        "emoji.php",
        "/emojis/",
        "/emoji/",
        "/static/",
    )
    if any(pat in url_lower for pat in ignored_patterns):
        return False

    # Filter out small dimensions commonly used for profile avatars and thumbnails
    ignored_dims = (
        "16x16",
        "32x32",
        "40x40",
        "48x48",
        "50x50",
        "60x60",
        "64x64",
        "75x75",
        "80x80",
        "96x96",
        "100x100",
        "p50x50",
        "p100x100",
        "s50x50",
        "s100x100",
        "cp0",
    )
    if any(dim in url_lower for dim in ignored_dims):
        return False

    # Filter out thumbnail filenames (_s, _q, _t)
    thumbnail_suffixes = (
        "_s.jpg",
        "_q.jpg",
        "_t.jpg",
        "_s.png",
        "_q.png",
        "_t.png",
        "_s.webp",
        "_q.webp",
        "_t.webp",
    )
    if any(url_lower.endswith(sfx) or (sfx + "?") in url_lower for sfx in thumbnail_suffixes):
        return False

    # Filter out explicit avatar or profile paths and CDN profile photo pattern (/t*-1/)
    if "/avatar" in url_lower or "/profile/" in url_lower:
        return False
    if re.search(r"/t\d+\.\d+-1/", url):
        return False

    return True


def extract_all_images(html_text: str, max_images: int = 4) -> list[str]:
    """Extract clean, full-resolution Facebook photo URLs from HTML/DOM.

    Returns up to max_images unique URLs suitable for Discord 2x2 collage rendering.
    """
    if not html_text or not isinstance(html_text, str):
        return []

    candidates: list[str] = []

    # 1. Parse OpenGraph and Twitter meta tags from DOM
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup.find_all("meta"):
            prop = (tag.get("property") or tag.get("name") or "").lower()
            if prop in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image"):
                content = tag.get("content")
                if content:
                    candidates.append(content)

        # Also check standard img tags
        for img_tag in soup.find_all("img"):
            src = img_tag.get("src") or img_tag.get("data-src")
            if src:
                candidates.append(src)
    except Exception:
        pass

    # 2. Extract from raw page HTML (including JSON blobs and scripts)
    unescaped_text = html_text.replace(r"\/", "/")
    regex_patterns = (
        r'https?://[^\s"\'<>\\]*scontent[^\s"\'<>\\]*\.xx\.fbcdn\.net/[^\s"\'<>\\]+',
        r'https?://[^\s"\'<>\\]*scontent[^\s"\'<>\\]*\.fbcdn\.net/[^\s"\'<>\\]+',
    )
    for pattern in regex_patterns:
        matches = re.findall(pattern, unescaped_text)
        candidates.extend(matches)

    # 3. Clean, filter, and deduplicate
    unique_images: list[str] = []
    seen_keys: set[str] = set()

    for cand in candidates:
        cleaned = clean_image_url(cand)
        if not is_valid_gallery_image(cleaned):
            continue

        # Deduplicate by photo filename or path identifier
        parsed = urllib.parse.urlparse(cleaned)
        filename = posixpath.basename(parsed.path)
        dedup_key = filename if filename else parsed.path

        if dedup_key in seen_keys:
            continue

        seen_keys.add(dedup_key)
        unique_images.append(cleaned)

        if len(unique_images) >= max_images:
            break

    return unique_images


def render_multi_image_tags(images: list[str]) -> str:
    """Generate HTML meta tags for multi-image Facebook posts.

    Generates:
    - Twitter card tag: <meta name="twitter:card" content="summary_large_image"/>
    - Twitter image tag for the primary image
    - Multiple og:image tags (Discord groups these into a 2x2 collage)
    """
    if not images:
        return '<meta name="twitter:card" content="summary_large_image"/>'

    tags: list[str] = ['<meta name="twitter:card" content="summary_large_image"/>']

    escaped_first = html.escape(images[0], quote=True)
    tags.append(f'<meta name="twitter:image" content="{escaped_first}"/>')

    for img in images:
        escaped_img = html.escape(img, quote=True)
        tags.append(f'<meta property="og:image" content="{escaped_img}"/>')

    return "\n".join(tags)


def render_multi_image_embed(
    fb_path: str,
    images: list[str],
    title: str | None = None,
    description: str | None = None,
) -> str:
    """Render a full HTML document embedding multiple images for Discord collage."""
    full_url = f"https://www.facebook.com/{fb_path}"
    clean_title = (title or "Facebook Post").strip()
    clean_desc = (description or "").strip()
    if not clean_desc:
        clean_desc = "FacebookFix -- FB embed fix"

    escaped_title = html.escape(clean_title, quote=True)
    escaped_desc = html.escape(clean_desc, quote=True)
    image_tags = render_multi_image_tags(images)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="theme-color" content="#007FFF"/>
<meta property="og:url" content="{full_url}"/>
<meta property="og:site_name" content="Facebook"/>
<meta property="og:type" content="article"/>
<meta property="og:title" content="{escaped_title}"/>
<meta property="og:description" content="{escaped_desc}"/>
{image_tags}
<meta http-equiv="refresh" content="0; url={full_url}"/>
<link rel="alternate" href="{full_url}" type="application/json+oembed" title="{escaped_title}"/>
</head>
<body>
Redirecting you to the post in a moment.
<a href="{full_url}">Or click here.</a>
</body>
</html>"""
