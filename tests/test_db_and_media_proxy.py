import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from sanic import Sanic

from gxfacebook.__main__ import app, get_base_url, render_embed
from gxfacebook.db import (
    _parse_count,
    extract_cdn_expiration,
    generate_media_id,
    get_media,
    get_media_by_path,
    get_request_logs,
    init_db,
    is_expired,
    log_request,
    save_media,
)


def test_generate_media_id():
    id1 = generate_media_id("share/r/19gDtehNTV")
    id2 = generate_media_id("/share/r/19gDtehNTV/")
    assert id1 == id2
    assert len(id1) == 64
    assert all(c in "0123456789abcdef" for c in id1)


def test_extract_cdn_expiration():
    # Valid hex oe
    url = "https://video.xx.fbcdn.net/v/vid.mp4?oe=6ABFE7E0&other=1"
    assert extract_cdn_expiration(url) == float(int("6ABFE7E0", 16))

    # Lowercase hex oe
    url_lower = "https://video.xx.fbcdn.net/v/vid.mp4?oe=6abfe7e0"
    assert extract_cdn_expiration(url_lower) == float(int("6abfe7e0", 16))

    # URL without oe
    now = time.time()
    url_no_oe = "https://video.xx.fbcdn.net/v/vid.mp4"
    exp = extract_cdn_expiration(url_no_oe, default_ttl_hours=24)
    assert abs(exp - (now + 24 * 3600)) < 5

    # Invalid oe
    url_invalid = "https://video.xx.fbcdn.net/v/vid.mp4?oe=not_a_hex"
    exp_invalid = extract_cdn_expiration(url_invalid)
    assert abs(exp_invalid - (now + 48 * 3600)) < 5

    # None or empty
    assert abs(extract_cdn_expiration(None) - (now + 48 * 3600)) < 5
    assert abs(extract_cdn_expiration("") - (now + 48 * 3600)) < 5


def test_is_expired():
    now = time.time()
    # Already passed
    assert is_expired(now - 10) is True
    # Inside 300-second buffer
    assert is_expired(now + 100, buffer_seconds=300) is True
    # Well in the future
    assert is_expired(now + 3600, buffer_seconds=300) is False
    # None expires_at
    assert is_expired(None) is True


@pytest.mark.asyncio
async def test_db_crud(tmp_path):
    test_db = tmp_path / "test_media.db"
    await init_db(test_db)

    # Initial save
    saved = await save_media(
        fb_path="share/r/sample1",
        media_type="video",
        hot_url="https://video.xx.fbcdn.net/v/test.mp4?oe=6ABFE7E0",
        thumbnail_url="https://thumb.xx.fbcdn.net/t.jpg",
        title="Sample Video",
        description="Sample Desc",
        width=1080,
        height=1920,
        extra_data={"comment_count": 12},
        like_count=45,
        comment_count="12",
        view_count="1.5K",
        ip_address="1.2.3.4",
        request_timestamp=1700000000.0,
        db_path=test_db,
    )
    media_id = saved["media_id"]
    assert media_id == generate_media_id("share/r/sample1")
    assert saved["title"] == "Sample Video"
    assert saved["width"] == 1080
    assert saved["height"] == 1920
    assert saved["like_count"] == 45
    assert saved["comment_count"] == 12
    assert saved["view_count"] == 1500
    assert saved["ip_address"] == "1.2.3.4"
    assert saved["request_timestamp"] == 1700000000.0

    # Retrieve by media_id
    item = await get_media(media_id, db_path=test_db)
    assert item is not None
    assert item["fb_path"] == "share/r/sample1"
    assert item["hot_url"] == "https://video.xx.fbcdn.net/v/test.mp4?oe=6ABFE7E0"
    assert item["like_count"] == 45
    assert item["comment_count"] == 12
    assert item["view_count"] == 1500
    assert item["ip_address"] == "1.2.3.4"
    assert item["request_timestamp"] == 1700000000.0

    # Retrieve by path
    by_path = await get_media_by_path("/share/r/sample1/", db_path=test_db)
    assert by_path is not None
    assert by_path["media_id"] == media_id
    assert by_path["like_count"] == 45
    assert by_path["comment_count"] == 12
    assert by_path["view_count"] == 1500
    assert by_path["ip_address"] == "1.2.3.4"

    # Verify save_media automatically wrote to request_logs
    logs = await get_request_logs(db_path=test_db)
    assert len(logs) == 1
    assert logs[0]["fb_path"] == "share/r/sample1"
    assert logs[0]["media_id"] == media_id
    assert logs[0]["ip_address"] == "1.2.3.4"
    assert logs[0]["like_count"] == 45
    assert logs[0]["comment_count"] == 12
    assert logs[0]["view_count"] == 1500
    assert logs[0]["request_timestamp"] == 1700000000.0

    # Non-existent
    assert await get_media("nonexistent_id", db_path=test_db) is None
    assert await get_media_by_path("nonexistent/path", db_path=test_db) is None

    # Update (upsert)
    updated = await save_media(
        fb_path="share/r/sample1",
        media_type="video",
        hot_url="https://video.xx.fbcdn.net/v/test_fresh.mp4?oe=7ABFE7E0",
        media_id=media_id,
        like_count=50,
        ip_address="5.6.7.8",
        db_path=test_db,
    )
    assert updated["hot_url"] == "https://video.xx.fbcdn.net/v/test_fresh.mp4?oe=7ABFE7E0"
    # Ensure title and thumbnail were preserved via COALESCE
    assert updated["title"] == "Sample Video"
    assert updated["thumbnail_url"] == "https://thumb.xx.fbcdn.net/t.jpg"
    assert updated["like_count"] == 50
    assert updated["comment_count"] == 12
    assert updated["view_count"] == 1500
    assert updated["ip_address"] == "5.6.7.8"

    # Total request logs is now 2
    logs = await get_request_logs(db_path=test_db)
    assert len(logs) == 2


def test_render_embed_proxy_url():
    # Without media_id / base_url -> defaults to video_url
    html_raw = render_embed("share/r/123", "https://direct-video.mp4")
    assert 'content="https://direct-video.mp4"' in html_raw

    # With media_id and base_url
    html_proxy = render_embed(
        "share/r/123",
        "https://direct-video.mp4",
        media_id="testmedia123",
        base_url="https://gxfacebook.example.com",
    )
    assert 'content="https://gxfacebook.example.com/media/testmedia123"' in html_proxy
    assert '<meta property="og:video" content="https://gxfacebook.example.com/media/testmedia123"/>' in html_proxy
    assert '<meta property="og:video:secure_url" content="https://gxfacebook.example.com/media/testmedia123"/>' in html_proxy
    assert '<meta name="twitter:player:stream" content="https://gxfacebook.example.com/media/testmedia123"/>' in html_proxy

    # With base_url only (auto generates media_id)
    expected_mid = generate_media_id("share/r/123")
    html_auto = render_embed(
        "share/r/123",
        "https://direct-video.mp4",
        base_url="https://gxfacebook.example.com/",
    )
    assert f'content="https://gxfacebook.example.com/media/{expected_mid}"' in html_auto


@pytest.mark.asyncio
async def test_media_proxy_unexpired_redirect():
    # Save a media record that will expire in the future
    mid = generate_media_id("share/r/unexpired")
    future_exp = time.time() + 3600
    await save_media(
        fb_path="share/r/unexpired",
        media_type="video",
        hot_url="https://video.xx.fbcdn.net/v/active.mp4",
        expires_at=future_exp,
        media_id=mid,
    )

    _, response = await app.asgi_client.get(f"/media/{mid}")
    assert response.status == 302
    assert response.headers.get("location") == "https://video.xx.fbcdn.net/v/active.mp4"


@pytest.mark.asyncio
async def test_media_proxy_expired_video_refreshes():
    mid = generate_media_id("share/r/expired_vid")
    past_exp = time.time() - 100
    await save_media(
        fb_path="share/r/expired_vid",
        media_type="video",
        hot_url="https://video.xx.fbcdn.net/v/stale.mp4",
        expires_at=past_exp,
        media_id=mid,
    )

    fresh_cdn = "https://video.xx.fbcdn.net/v/fresh.mp4?oe=7ABFE7E0"
    with patch("gxfacebook.__main__.fbmatch", return_value=(fresh_cdn, {"thumbnail": "https://thumb.jpg"})):
        _, response = await app.asgi_client.get(f"/media/{mid}")
        assert response.status == 302
        assert response.headers.get("location") == fresh_cdn

        # Check DB was updated with fresh URL
        updated_media = await get_media(mid)
        assert updated_media["hot_url"] == fresh_cdn
        assert not is_expired(updated_media["expires_at"])


@pytest.mark.asyncio
async def test_media_proxy_expired_image_refreshes():
    mid = generate_media_id("share/p/expired_img")
    past_exp = time.time() - 100
    await save_media(
        fb_path="share/p/expired_img",
        media_type="image",
        hot_url="https://scontent.xx.fbcdn.net/v/stale.jpg",
        expires_at=past_exp,
        media_id=mid,
    )

    fresh_img = "https://scontent.xx.fbcdn.net/v/fresh.jpg?oe=7ABFE7E0"
    with patch("gxfacebook.__main__.fetch_facebook_metadata", new_callable=AsyncMock) as mock_meta:
        mock_meta.return_value = {"image": fresh_img, "title": "Refreshed Photo"}
        _, response = await app.asgi_client.get(f"/media/{mid}")
        assert response.status == 302
        assert response.headers.get("location") == fresh_img

        updated_media = await get_media(mid)
        assert updated_media["hot_url"] == fresh_img


@pytest.mark.asyncio
async def test_media_proxy_not_found():
    _, response = await app.asgi_client.get("/media/0000000000000000000000000000000000000000000000000000000000000000")
    assert response.status == 404


def test_parse_count():
    assert _parse_count(None) is None
    assert _parse_count(False) is None
    assert _parse_count(True) is None
    assert _parse_count(0) == 0
    assert _parse_count(123) == 123
    assert _parse_count(123.4) == 123
    assert _parse_count("0") == 0
    assert _parse_count("123") == 123
    assert _parse_count("1,234") == 1234
    assert _parse_count("1.2K") == 1200
    assert _parse_count("2.5M") == 2500000
    assert _parse_count("1.5B") == 1500000000
    assert _parse_count("💬 50 comments") == 50
    assert _parse_count("❤️ 1.5K likes") == 1500
    assert _parse_count("") is None
    assert _parse_count("   ") is None
    assert _parse_count("invalid") is None


@pytest.mark.asyncio
async def test_log_request_and_get_request_logs(tmp_path):
    test_db = tmp_path / "test_logs.db"

    t1 = 1700000001.0
    t2 = 1700000002.0
    t3 = 1700000003.0

    await log_request(
        fb_path="share/r/post1",
        media_id="mid1",
        ip_address="1.1.1.1",
        request_timestamp=t1,
        like_count=10,
        comment_count=5,
        view_count=100,
        db_path=test_db,
    )
    await log_request(
        fb_path="share/r/post2",
        media_id="mid2",
        ip_address="2.2.2.2",
        request_timestamp=t2,
        like_count="1.2K",
        comment_count="50",
        view_count="10K",
        db_path=test_db,
    )
    await log_request(
        fb_path="share/p/post3",
        media_id="mid3",
        ip_address="3.3.3.3",
        request_timestamp=t3,
        like_count=999,
        comment_count=88,
        view_count=None,
        db_path=test_db,
    )

    # Test limit=2 (ordered newest first)
    logs_limit = await get_request_logs(limit=2, db_path=test_db)
    assert len(logs_limit) == 2
    assert logs_limit[0]["fb_path"] == "share/p/post3"
    assert logs_limit[0]["ip_address"] == "3.3.3.3"
    assert logs_limit[0]["like_count"] == 999
    assert logs_limit[1]["fb_path"] == "share/r/post2"
    assert logs_limit[1]["like_count"] == 1200
    assert logs_limit[1]["view_count"] == 10000

    # Test all logs
    all_logs = await get_request_logs(limit=10, db_path=test_db)
    assert len(all_logs) == 3
    assert all_logs[2]["fb_path"] == "share/r/post1"
    assert all_logs[2]["ip_address"] == "1.1.1.1"

    # Test nonexistent DB returns empty list
    assert await get_request_logs(db_path=tmp_path / "nonexistent.db") == []


@pytest.mark.asyncio
async def test_db_migration(tmp_path):
    import aiosqlite

    test_db = tmp_path / "legacy_media.db"

    # Create older schema without the new columns and without request_logs table
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            """
            CREATE TABLE media_cache (
                media_id TEXT PRIMARY KEY,
                fb_path TEXT NOT NULL,
                media_type TEXT NOT NULL,
                hot_url TEXT NOT NULL,
                thumbnail_url TEXT,
                title TEXT,
                description TEXT,
                width INTEGER,
                height INTEGER,
                extra_data TEXT,
                expires_at REAL,
                created_at REAL,
                updated_at REAL
            );
            """
        )
        await db.execute(
            """
            INSERT INTO media_cache (
                media_id, fb_path, media_type, hot_url, thumbnail_url,
                title, description, width, height, extra_data,
                expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "legacy123",
                "share/r/legacy",
                "video",
                "https://video.xx.fbcdn.net/v/legacy.mp4",
                "https://thumb.xx.fbcdn.net/t.jpg",
                "Legacy Video",
                "Legacy Desc",
                720,
                1280,
                None,
                time.time() + 3600,
                time.time(),
                time.time(),
            ),
        )
        await db.commit()

    # Run init_db to perform migration
    await init_db(test_db)

    # Verify PRAGMA table_info has new columns
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("PRAGMA table_info(media_cache);") as cursor:
            cols = {row[1] for row in await cursor.fetchall()}
            assert "like_count" in cols
            assert "comment_count" in cols
            assert "view_count" in cols
            assert "ip_address" in cols
            assert "request_timestamp" in cols

        # Verify request_logs table exists
        async with db.execute("PRAGMA table_info(request_logs);") as cursor:
            log_cols = {row[1] for row in await cursor.fetchall()}
            assert "fb_path" in log_cols
            assert "media_id" in log_cols
            assert "ip_address" in log_cols
            assert "request_timestamp" in log_cols
            assert "like_count" in log_cols
            assert "comment_count" in log_cols
            assert "view_count" in log_cols

    # Verify legacy item was preserved and can be retrieved
    item = await get_media("legacy123", db_path=test_db)
    assert item is not None
    assert item["title"] == "Legacy Video"
    assert item["like_count"] is None

    # Now update legacy item using save_media
    updated = await save_media(
        fb_path="share/r/legacy",
        media_type="video",
        hot_url="https://video.xx.fbcdn.net/v/legacy_fresh.mp4",
        media_id="legacy123",
        like_count="3.2K",
        comment_count="150",
        view_count="1M",
        ip_address="10.0.0.1",
        db_path=test_db,
    )
    assert updated["like_count"] == 3200
    assert updated["comment_count"] == 150
    assert updated["view_count"] == 1000000
    assert updated["ip_address"] == "10.0.0.1"

    logs = await get_request_logs(db_path=test_db)
    assert len(logs) == 1
    assert logs[0]["like_count"] == 3200


@pytest.mark.asyncio
async def test_embed_cached_logging():
    path = "share/r/test_cached_logging_reel"
    mid = generate_media_id(path)
    await save_media(
        fb_path=path,
        media_type="video",
        hot_url="https://video.xx.fbcdn.net/v/cached.mp4",
        expires_at=time.time() + 3600,
        media_id=mid,
        like_count=123,
        comment_count=45,
        view_count=678,
    )

    with patch("gxfacebook.__main__.log_request", new_callable=AsyncMock) as mock_log:
        req, resp = await app.asgi_client.get(f"/{path}")
        assert resp.status == 200
        mock_log.assert_called_once()
        _, kwargs = mock_log.call_args
        assert kwargs["fb_path"] == path
        assert kwargs["media_id"] == mid
        assert kwargs["like_count"] == 123
        assert kwargs["comment_count"] == 45
        assert kwargs["view_count"] == 678

