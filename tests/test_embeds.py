import pytest
from gxfacebook.__main__ import (
    app,
    format_number,
    is_valid_path,
    is_video_path_request,
    extract_clean_image,
    is_valid_image_post,
    render_embed,
    render_image_embed,
    render_error_embed,
)


def test_format_number():
    assert format_number(None) == "0"
    assert format_number(0) == "0"
    assert format_number(500) == "500"
    assert format_number(1000) == "1K"
    assert format_number(1500) == "1.5K"
    assert format_number(1000000) == "1M"
    assert format_number(2500000) == "2.5M"


def test_is_valid_path():
    assert is_valid_path("share/r/19gDtehNTV")
    assert is_valid_path("share/p/19d6PRtbEt")
    assert not is_valid_path("../etc/passwd")
    assert not is_valid_path("path//with//double//slash")
    assert not is_valid_path("invalid path with spaces")


def test_is_video_path_request():
    assert is_video_path_request("share/r/19gDtehNTV")
    assert is_video_path_request("share/v/12345")
    assert is_video_path_request("reel/12345")
    assert is_video_path_request("reels/12345")
    assert is_video_path_request("watch?v=123")
    assert is_video_path_request("user/videos/123")
    assert not is_video_path_request("share/p/19d6PRtbEt")
    assert not is_video_path_request("groups/123/posts/456")


def test_extract_clean_image():
    # Direct scontent URL without watermark
    scontent_url = "https://scontent-ord5-2.xx.fbcdn.net/v/t39.30808-6/photo.jpg"
    assert extract_clean_image(scontent_url) == scontent_url

    # Reject lookaside watermarked URLs
    lookaside_url = "https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id=123"
    assert extract_clean_image(lookaside_url) is None

    # Fallback to extracting from HTML
    html = f'<div><img src="{scontent_url}"></div>'
    assert extract_clean_image(lookaside_url, html) == scontent_url

    # Fallback from JSON escaped slashes in HTML
    json_html = '{"image_uri":"https:\\/\\/scontent-ord5-1.xx.fbcdn.net\\/v\\/photo2.jpg"}'
    assert extract_clean_image(None, json_html) == "https://scontent-ord5-1.xx.fbcdn.net/v/photo2.jpg"


def test_render_error_embed():
    html = render_error_embed("share/r/18YCfY4vcq")
    assert "⚠️ Content Unavailable" in html
    assert "This video or post is private, age-restricted (18+), or has been removed." in html
    assert "#ED4245" in html  # Red theme color
    assert '<meta name="twitter:card" content="summary"/>' in html
    assert '<meta http-equiv="refresh" content="0; url=https://www.facebook.com/share/r/18YCfY4vcq"/>' in html


def test_render_image_embed():
    img_url = "https://scontent-ord5-2.xx.fbcdn.net/v/photo.jpg"
    html = render_image_embed("share/p/19d6PRtbEt", img_url, title="Sample Post", description="Sample Caption")
    assert '<meta name="twitter:card" content="summary_large_image"/>' in html
    assert f'<meta property="og:image" content="{img_url}"/>' in html
    assert f'<meta name="twitter:image" content="{img_url}"/>' in html
    assert '<meta property="og:title" content="Sample Post"/>' in html
    assert '<meta property="og:description" content="Sample Caption"/>' in html
    assert "lookaside.fbsbx.com" not in html


def test_render_video_embed_uses_clean_thumbnail():
    vid_info = {
        "title": "48K views · 1.6K reactions",
        "comment_count": 50,
        "thumbnail": "https://scontent-ord5-2.xx.fbcdn.net/v/t15.5256-10/thumb.jpg",
    }
    html = render_embed("share/r/19gDtehNTV", "https://video.mp4", vid_info)
    assert '<meta property="og:video" content="https://video.mp4"/>' in html
    assert "https://scontent-ord5-2.xx.fbcdn.net/v/t15.5256-10/thumb.jpg" in html
    # Ensure hardcoded .ico is NOT used when thumbnail is available
    assert "https://static.xx.fbcdn.net/rsrc.php/yo/r/iRmz9lCMBD2.ico" not in html


@pytest.mark.asyncio
async def test_live_video_reel():
    _, response = await app.asgi_client.get("/share/r/19gDtehNTV/")
    assert response.status == 200
    assert "og:video" in response.text
    assert "/media/" in response.text
    assert "scontent" in response.text
    assert "lookaside.fbsbx.com" not in response.text


@pytest.mark.asyncio
async def test_live_image_post():
    _, response = await app.asgi_client.get("/share/p/19d6PRtbEt/")
    assert response.status == 200
    assert 'name="twitter:card" content="summary_large_image"' in response.text
    assert "og:image" in response.text
    assert "scontent" in response.text
    assert "lookaside.fbsbx.com" not in response.text
    assert "LRT station" in response.text


@pytest.mark.asyncio
async def test_live_blocked_or_unavailable_reel():
    _, response = await app.asgi_client.get("/share/r/18YCfY4vcq/")
    assert response.status == 200  # Must be 200, NOT 500!
    assert "⚠️ Content Unavailable" in response.text
    assert "This video or post is private, age-restricted (18+), or has been removed." in response.text
    assert "#ED4245" in response.text
    assert "twitter:card" in response.text


@pytest.mark.asyncio
async def test_invalid_path():
    _, response = await app.asgi_client.get("/invalid$path!@")
    assert response.status == 400


def test_render_multi_image_embed():
    imgs = [
        "https://scontent-ord5-2.xx.fbcdn.net/v/photo1.jpg",
        "https://scontent-ord5-1.xx.fbcdn.net/v/photo2.jpg",
        "https://scontent-ord5-2.xx.fbcdn.net/v/photo3.jpg",
    ]
    html = render_image_embed("share/p/123", imgs[0], title="Album Post", images=imgs)
    assert '3 Photos' in html
    assert '💬' in html and '❤️' in html
    assert 'content="https://scontent-ord5-2.xx.fbcdn.net/v/photo1.jpg"' in html
    assert 'content="https://scontent-ord5-1.xx.fbcdn.net/v/photo2.jpg"' in html
    assert 'content="https://scontent-ord5-2.xx.fbcdn.net/v/photo3.jpg"' in html
    assert html.count('<meta property="og:image"') == 3


@pytest.mark.asyncio
async def test_live_group_image_post_without_og_title():
    _, response = await app.asgi_client.get("/share/p/1QrGokTVBT/")
    assert response.status == 200
    assert 'name="twitter:card" content="summary_large_image"' in response.text
    assert "og:image" in response.text
    assert "scontent" in response.text
    assert "lookaside.fbsbx.com" not in response.text
    assert "World Cityscapes" in response.text


