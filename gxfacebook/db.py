import hashlib
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiosqlite

DEFAULT_DB_PATH = Path("/home/ohncal/gxfacebook/gxfacebook.db")
DB_PATH = Path(os.environ.get("GXFACEBOOK_DB", str(DEFAULT_DB_PATH)))


def get_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    return Path(os.environ.get("GXFACEBOOK_DB", str(DEFAULT_DB_PATH)))


def generate_media_id(fb_path: str) -> str:
    """Generate deterministic SHA256 hex digest for cleaned fb_path."""
    cleaned = fb_path.strip("/")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def extract_cdn_expiration(url: str | None, default_ttl_hours: int = 48) -> float:
    """
    Facebook CDN URLs often include oe=<hex_timestamp>.
    Convert hex to integer unix timestamp.
    If not present or invalid, fallback to time.time() + default_ttl_hours * 3600.
    """
    now = time.time()
    default_expiration = now + (default_ttl_hours * 3600)
    if not url:
        return default_expiration

    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        oe_list = params.get("oe")
        oe = oe_list[0] if oe_list else None

        if not oe:
            # Fallback regex search if query parsing missed it
            match = re.search(r"[?&]oe=([0-9a-fA-F]+)", url)
            if match:
                oe = match.group(1)

        if oe:
            timestamp = int(oe, 16)
            if timestamp > 0:
                return float(timestamp)
    except Exception:
        pass

    return default_expiration


def is_expired(expires_at: float | None, buffer_seconds: int = 300) -> bool:
    """
    Check if time.time() + buffer_seconds >= expires_at.
    Returns True if media is expired or about to expire within buffer_seconds.
    """
    if expires_at is None:
        return True
    return (time.time() + buffer_seconds) >= expires_at


def _parse_count(val: Any) -> int | None:
    """Parse count from integer, float, or string (supporting suffixes like K, M, B)."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        val = val.strip().replace(",", "")
        if not val:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)\s*([KkMmBb])?", val)
        if match:
            try:
                num = float(match.group(1))
                suffix = (match.group(2) or "").upper()
                if suffix == "K":
                    num *= 1_000
                elif suffix == "M":
                    num *= 1_000_000
                elif suffix == "B":
                    num *= 1_000_000_000
                return int(num)
            except (ValueError, TypeError):
                return None
    return None


_initialized_dbs: set[Path] = set()


async def init_db(db_path: str | Path | None = None) -> None:
    """Initialize the SQLite database schema, run migrations, and create indices."""
    target_path = get_db_path(db_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(target_path) as db:
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA synchronous = NORMAL;")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS media_cache (
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
                like_count INTEGER,
                comment_count INTEGER,
                view_count INTEGER,
                ip_address TEXT,
                request_timestamp REAL,
                created_at REAL,
                updated_at REAL
            );
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_cache_fb_path ON media_cache(fb_path);"
        )

        # Migration logic for existing databases: check existing columns in media_cache
        async with db.execute("PRAGMA table_info(media_cache);") as cursor:
            columns_info = await cursor.fetchall()
            existing_cols = {row[1] for row in columns_info}

        columns_to_add = [
            ("like_count", "INTEGER"),
            ("comment_count", "INTEGER"),
            ("view_count", "INTEGER"),
            ("ip_address", "TEXT"),
            ("request_timestamp", "REAL"),
        ]
        for col_name, col_type in columns_to_add:
            if col_name not in existing_cols:
                await db.execute(
                    f"ALTER TABLE media_cache ADD COLUMN {col_name} {col_type};"
                )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fb_path TEXT NOT NULL,
                media_id TEXT,
                ip_address TEXT,
                request_timestamp REAL,
                like_count INTEGER,
                comment_count INTEGER,
                view_count INTEGER
            );
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_fb_path ON request_logs(fb_path);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs(request_timestamp);"
        )
        await db.commit()

    _initialized_dbs.add(target_path)


async def ensure_db_initialized(target_path: Path) -> None:
    if target_path not in _initialized_dbs:
        await init_db(target_path)


async def log_request(
    fb_path: str,
    media_id: str | None = None,
    ip_address: str | None = None,
    request_timestamp: float | None = None,
    like_count: int | str | None = None,
    comment_count: int | str | None = None,
    view_count: int | str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Log an incoming Facebook content request with timestamp, IP, and metrics."""
    target_path = get_db_path(db_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    await ensure_db_initialized(target_path)
    cleaned_path = fb_path.strip("/")
    if request_timestamp is None:
        request_timestamp = time.time()

    parsed_like = _parse_count(like_count)
    parsed_comment = _parse_count(comment_count)
    parsed_view = _parse_count(view_count)

    async with aiosqlite.connect(target_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fb_path TEXT NOT NULL,
                media_id TEXT,
                ip_address TEXT,
                request_timestamp REAL,
                like_count INTEGER,
                comment_count INTEGER,
                view_count INTEGER
            );
            """
        )
        await db.execute(
            """
            INSERT INTO request_logs (
                fb_path, media_id, ip_address, request_timestamp,
                like_count, comment_count, view_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                cleaned_path,
                media_id,
                ip_address,
                request_timestamp,
                parsed_like,
                parsed_comment,
                parsed_view,
            ),
        )
        await db.commit()


async def get_request_logs(
    limit: int = 50, db_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Retrieve recent request logs ordered by newest first."""
    target_path = get_db_path(db_path)
    if not target_path.exists():
        return []

    async with aiosqlite.connect(target_path) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT * FROM request_logs ORDER BY request_timestamp DESC, id DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []


async def get_media(media_id: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """Retrieve cached media item by media_id."""
    target_path = get_db_path(db_path)
    if not target_path.exists():
        return None

    async with aiosqlite.connect(target_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM media_cache WHERE media_id = ?", (media_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


async def get_media_by_path(
    fb_path: str, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    """Retrieve cached media item by cleaned fb_path."""
    target_path = get_db_path(db_path)
    if not target_path.exists():
        return None

    cleaned = fb_path.strip("/")
    async with aiosqlite.connect(target_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM media_cache WHERE fb_path = ? ORDER BY updated_at DESC LIMIT 1",
            (cleaned,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None


async def save_media(
    fb_path: str,
    media_type: str,
    hot_url: str,
    thumbnail_url: str | None = None,
    title: str | None = None,
    description: str | None = None,
    width: int | None = None,
    height: int | None = None,
    extra_data: str | dict | None = None,
    expires_at: float | None = None,
    like_count: int | str | None = None,
    comment_count: int | str | None = None,
    view_count: int | str | None = None,
    ip_address: str | None = None,
    request_timestamp: float | None = None,
    media_id: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Insert or update a media cache entry.
    If media_id is not provided, computes generate_media_id(fb_path).
    If expires_at is not provided, extracts it from hot_url.
    Persists metrics and logs the request into request_logs.
    """
    target_path = get_db_path(db_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    await ensure_db_initialized(target_path)

    cleaned_path = fb_path.strip("/")

    if not media_id:
        media_id = generate_media_id(cleaned_path)

    if expires_at is None:
        expires_at = extract_cdn_expiration(hot_url)

    now = time.time()
    if request_timestamp is None:
        request_timestamp = now

    parsed_like = _parse_count(like_count)
    parsed_comment = _parse_count(comment_count)
    parsed_view = _parse_count(view_count)

    if isinstance(extra_data, dict):
        if parsed_like is None:
            parsed_like = _parse_count(
                extra_data.get("like_count") or extra_data.get("reaction_count")
            )
        if parsed_comment is None:
            parsed_comment = _parse_count(extra_data.get("comment_count"))
        if parsed_view is None:
            parsed_view = _parse_count(extra_data.get("view_count"))
        extra_data = json.dumps(extra_data)

    result_dict: dict[str, Any] | None = None

    async with aiosqlite.connect(target_path) as db:
        db.row_factory = aiosqlite.Row
        query = """
        INSERT INTO media_cache (
            media_id, fb_path, media_type, hot_url, thumbnail_url,
            title, description, width, height, extra_data,
            expires_at, like_count, comment_count, view_count,
            ip_address, request_timestamp, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(media_id) DO UPDATE SET
            fb_path = excluded.fb_path,
            media_type = excluded.media_type,
            hot_url = excluded.hot_url,
            thumbnail_url = coalesce(excluded.thumbnail_url, media_cache.thumbnail_url),
            title = coalesce(excluded.title, media_cache.title),
            description = coalesce(excluded.description, media_cache.description),
            width = coalesce(excluded.width, media_cache.width),
            height = coalesce(excluded.height, media_cache.height),
            extra_data = coalesce(excluded.extra_data, media_cache.extra_data),
            expires_at = excluded.expires_at,
            like_count = coalesce(excluded.like_count, media_cache.like_count),
            comment_count = coalesce(excluded.comment_count, media_cache.comment_count),
            view_count = coalesce(excluded.view_count, media_cache.view_count),
            ip_address = coalesce(excluded.ip_address, media_cache.ip_address),
            request_timestamp = coalesce(excluded.request_timestamp, media_cache.request_timestamp),
            updated_at = excluded.updated_at
        RETURNING *;
        """
        async with db.execute(
            query,
            (
                media_id,
                cleaned_path,
                media_type,
                hot_url,
                thumbnail_url,
                title,
                description,
                width,
                height,
                extra_data,
                expires_at,
                parsed_like,
                parsed_comment,
                parsed_view,
                ip_address,
                request_timestamp,
                now,
                now,
            ),
        ) as cursor:
            row = await cursor.fetchone()
            await db.commit()
            if row:
                result_dict = dict(row)

    await log_request(
        fb_path=cleaned_path,
        media_id=media_id,
        ip_address=ip_address,
        request_timestamp=request_timestamp,
        like_count=parsed_like,
        comment_count=parsed_comment,
        view_count=parsed_view,
        db_path=target_path,
    )

    if result_dict is not None:
        return result_dict

    return {
        "media_id": media_id,
        "fb_path": cleaned_path,
        "media_type": media_type,
        "hot_url": hot_url,
        "thumbnail_url": thumbnail_url,
        "title": title,
        "description": description,
        "width": width,
        "height": height,
        "extra_data": extra_data,
        "expires_at": expires_at,
        "like_count": parsed_like,
        "comment_count": parsed_comment,
        "view_count": parsed_view,
        "ip_address": ip_address,
        "request_timestamp": request_timestamp,
        "created_at": now,
        "updated_at": now,
    }
