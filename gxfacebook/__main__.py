from sanic import Sanic, response
from sanic.exceptions import NotFound
from yt_dlp import YoutubeDL
from urllib.parse import quote
import re

app = Sanic("FacebookReelDownloader")

# Your fbmatch logic, slightly adapted to accept a full URL
def fbmatch(url: str):
    ydl_opts = {
        'format': 'hd',
        'quiet': True,
        'skip_download': True,
        'forceurl': True,
        'noplaylist': True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return info.get('url')
        except Exception as e:
            print(f"[ERROR] yt_dlp failed: {e}")
            return None
    

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

def is_valid_path(path: str) -> bool:
    # Allow: letters, digits, dash, underscore, slash — and no ".." or double slashes
    return (
        bool(re.fullmatch(r"[\w\-/]+", path))
        and ".." not in path
        and "//" not in path
    )
@app.get("/<path:path>")
async def embed_facebook_video(request, path):
    # Strip leading/trailing slashes
    cleaned_path = path.strip("/")

    # Validate
    if not is_valid_path(cleaned_path):
        return response.text("Invalid Facebook video path", status=400)

    # Safely encode the path
    encoded_path = quote(cleaned_path)
    fb_url = f"https://www.facebook.com/{encoded_path}"

    # Extract video
    video_url = fbmatch(fb_url)
    if not video_url:
        return response.text("Unable to retrieve video", status=500)

    # Build and return embed HTML
    html = render_embed(cleaned_path, video_url)
    return response.html(html)

