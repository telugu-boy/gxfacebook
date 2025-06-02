import logging
import re
from datetime import datetime
from urllib.parse import quote

from sanic import Sanic, response
from sanic.exceptions import NotFound
from yt_dlp import YoutubeDL

# Create app
app = Sanic("FacebookReelDownloader")

# Configure loggers
access_logger = logging.getLogger("access_logger")
error_logger = logging.getLogger("error_logger")

access_handler = logging.FileHandler("access.log")
access_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
access_logger.addHandler(access_handler)
access_logger.setLevel(logging.INFO)

error_handler = logging.FileHandler("errors.log")
error_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
error_logger.addHandler(error_handler)
error_logger.setLevel(logging.ERROR)


# Middleware to log every request
@app.middleware("request")
async def log_facebook_requests(request):
    ip = request.remote_addr
    method = request.method
    path = request.path

    # Only log paths starting with /share/
    access_logger.info(f"{ip} - {method} {path}")


# Helper function to format numbers with K/M suffixes
def format_number(num):
    if num is None:
        return "0"
    
    num = int(num)
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M".rstrip('0').rstrip('.')
    elif num >= 1_000:
        return f"{num / 1_000:.1f}K".rstrip('0').rstrip('.')
    else:
        return str(num)

# Updated embed HTML generator
def render_embed(fb_path: str, video_url: str, video_info: dict = None):
    full_url = f"https://www.facebook.com/{fb_path}"
    
    # Generate stats title if video_info is provided
    if video_info:
        import re
        
        comment_count = format_number(video_info.get('comment_count', 0))
        
        # Extract views and reactions from title
        title = video_info.get('title', '')
        view_count = "0"
        reaction_count = "0"
        
        # Look for pattern like "652K views · 237K reactions"
        view_match = re.search(r'(\d+(?:\.\d+)?[KM]?)\s*views', title, re.IGNORECASE)
        if view_match:
            view_count = view_match.group(1)
            
        reaction_match = re.search(r'(\d+(?:\.\d+)?[KM]?)\s*reactions', title, re.IGNORECASE)
        if reaction_match:
            reaction_count = reaction_match.group(1)
        
        # Create stats title
        og_title = f"💬 {comment_count} ❤️ {reaction_count} 👁️ {view_count}"
    else:
        og_title = "Facebook Reel"
    
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
<meta name="twitter:image" content="https://static.xx.fbcdn.net/rsrc.php/yo/r/iRmz9lCMBD2.ico"/>
<meta name="twitter:player:width" content="720"/>
<meta name="twitter:player:height" content="1280"/>
<meta name="twitter:player:stream" content="{video_url}"/>
<meta name="twitter:player:stream:content_type" content="video/mp4"/>
<meta property="og:site_name" content="{og_title}"/>
<meta property="og:image" content="https://static.xx.fbcdn.net/rsrc.php/yo/r/iRmz9lCMBD2.ico"/>
<meta property="og:video" content="{video_url}"/>
<meta property="og:video:secure_url" content="{video_url}"/>
<meta property="og:video:type" content="video/mp4"/>
<meta property="og:video:width" content="720"/>
<meta property="og:video:height" content="1280"/>
<link rel="alternate" href="{full_url}" type="application/json+oembed" title="{og_title}"/>
</head>
<body>
Redirecting you to the post in a moment.
<a href="{full_url}">Or click here.</a>
</body>
</html>"""

# Updated video downloader to return both URL and info
def fbmatch(url: str):
    ydl_opts = {
        "format": "hd",
        "quiet": True,
        "skip_download": True,
        "forceurl": True,
        "noplaylist": True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return info.get("url"), info
        except Exception as e:
            error_logger.error(f"[yt-dlp ERROR] {url} - {e}")
            return None, None


# Validate path
def is_valid_path(path: str) -> bool:
    return (
        bool(re.fullmatch(r"[\w\-/]+", path)) and ".." not in path and "//" not in path
    )


# Homepage

@app.get("/favicon.ico")
async def favicon(request):
    return await response.file("static/favicon.ico")

@app.get("/")
async def homepage(request):
    # Read the HTML file
    return await response.file("static/index.html")

# Embed route
@app.get("/<path:path>")
async def embed_facebook_video(request, path):
    cleaned_path = path.strip("/")

    if not is_valid_path(cleaned_path):
        error_logger.error(f"[Invalid Path] {request.remote_addr} tried '{path}'")
        return response.text("Invalid Facebook video path", status=400)

    encoded_path = quote(cleaned_path)
    fb_url = f"https://www.facebook.com/{encoded_path}"

    video_url, vidinfo = fbmatch(fb_url)
    if not video_url:
        error_logger.error(
            f"[Video Fetch Error] {request.remote_addr} failed on {fb_url}"
        )
        return response.text("Unable to retrieve video", status=500)

    print(f"[OK] {request.remote_addr} requested /{path}")

    html = render_embed(cleaned_path, video_url, vidinfo)
    return response.html(html)
