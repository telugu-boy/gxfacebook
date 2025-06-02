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



# Video downloader
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
            return info.get("url")
        except Exception as e:
            error_logger.error(f"[yt-dlp ERROR] {url} - {e}")
            return None


# Embed HTML generator
def render_embed(fb_path: str, video_url: str):
    full_url = f"https://www.facebook.com/{fb_path}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="theme-color" content="#007FFF"/>
<meta property="og:url" content="{full_url}"/>
<meta property="og:description" content="Facebook Reel"/>
<meta http-equiv="refresh" content="0; url={full_url}"/>
<meta name="twitter:card" content="player"/>
<meta name="twitter:title" content="Facebook Reel"/>
<meta name="twitter:image" content="https://static.xx.fbcdn.net/rsrc.php/yo/r/iRmz9lCMBD2.ico"/>
<meta name="twitter:player:width" content="720"/>
<meta name="twitter:player:height" content="1280"/>
<meta name="twitter:player:stream" content="{video_url}"/>
<meta name="twitter:player:stream:content_type" content="video/mp4"/>
<meta property="og:site_name" content="FacebookFix -- FB embed fix"/>
<meta property="og:image" content="https://static.xx.fbcdn.net/rsrc.php/yo/r/iRmz9lCMBD2.ico"/>
<meta property="og:video" content="{video_url}"/>
<meta property="og:video:secure_url" content="{video_url}"/>
<meta property="og:video:type" content="video/mp4"/>
<meta property="og:video:width" content="720"/>
<meta property="og:video:height" content="1280"/>
<link rel="alternate" href="{full_url}" type="application/json+oembed" title="Facebook Reel"/>
</head>
<body>
Redirecting you to the post in a moment.
<a href="{full_url}">Or click here.</a>
</body>
</html>"""


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
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8" />
        <title>gxfacebook</title>
        <style>
            body {
                background-color: #fdf6e3;
                color: #2e2b29;
                font-family: 'Georgia', serif;
                max-width: 700px;
                margin: 4em auto;
                padding: 2em;
                line-height: 1.7;
                border: 1px solid #ddd0b4;
                box-shadow: 2px 2px 8px rgba(0,0,0,0.1);
                background-image: repeating-linear-gradient(45deg, transparent, transparent 2px, #f2e9d8 2px, #f2e9d8 4px);
            }
            h1 {
                font-size: 2.2em;
                text-align: center;
                color: #3b2314;
                margin-bottom: 1em;
                border-bottom: 1px dashed #8b7355;
                padding-bottom: 0.5em;
            }
            code {
                background: #eee8d5;
                padding: 2px 5px;
                font-size: 0.95em;
                border: 1px solid #d6c8a8;
                border-radius: 3px;
                font-family: "Courier New", Courier, monospace;
                color: #6b4f3b;
            }
            a {
                color: #4a3c2f;
                text-decoration: underline;
            }
            .box {
                background: #fffdf7;
                border: 1px solid #d6c8a8;
                padding: 1em;
                margin-top: 2em;
            }
        </style>
    </head>
    <body>
        <h1>FacebookFix -- Embed Facebook media in Discord, Slack, Twitter</h1>
        <div class="box">
            <p>
                FacebookFix helps you generate embeddable pages for Facebook videos and reels.
            </p>
            <p>
                To use it, take the Facebook video URL path and change <code>f</code> to <code>fix</code>:<br>
                <code>https://www.fixacebook.com/</code>
            </p>
            <p><strong>Example:</strong></p>
            <p>
                Original Facebook URL:<br>
                <code>https://www.facebook.com/share/v/1CYpws4WmF/</code>
            </p>
            <p>
                Use this link instead:<br>
                <code>https://www.fixacebook.com/share/v/1CYpws4WmF/</code>
            </p>
            <p>
                That link will show an embeddable page for the video, with OpenGraph and Twitter preview metadata.
            </p>
            <p>
                <a href="/share/v/1CYpws4WmF/">Try the example video →</a>
            </p>
        </div>
    </body>
    </html>
    """
    return response.html(html)


# Embed route
@app.get("/<path:path>")
async def embed_facebook_video(request, path):
    cleaned_path = path.strip("/")

    if not is_valid_path(cleaned_path):
        error_logger.error(f"[Invalid Path] {request.remote_addr} tried '{path}'")
        return response.text("Invalid Facebook video path", status=400)

    encoded_path = quote(cleaned_path)
    fb_url = f"https://www.facebook.com/{encoded_path}"

    video_url = fbmatch(fb_url)
    if not video_url:
        error_logger.error(
            f"[Video Fetch Error] {request.remote_addr} failed on {fb_url}"
        )
        return response.text("Unable to retrieve video", status=500)

    print(f"[OK] {request.remote_addr} requested /{path}")

    html = render_embed(cleaned_path, video_url)
    return response.html(html)
