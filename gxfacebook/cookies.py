"""Cookie management for Facebook authentication and burner accounts.

Provides utilities for discovering Netscape format cookie files and
parsing them into dictionaries suitable for httpx.AsyncClient.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Search candidates in prioritized order
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_COOKIE_FILENAMES = ("cookies.txt", "facebook_cookies.txt")


def get_cookie_file(custom_path: str | Path | None = None) -> str | None:
    """Return the path to a non-empty cookie file, or None if not found.

    Checks custom_path if provided, otherwise checks default locations
    in the project root and current working directory:
      - /home/ohncal/gxfacebook/cookies.txt
      - /home/ohncal/gxfacebook/facebook_cookies.txt
    """
    if custom_path:
        p = Path(custom_path)
        if p.is_file() and p.stat().st_size > 0:
            return str(p)
        return None

    candidates: list[Path] = []
    # Primary configured locations
    fixed_base = Path("/home/ohncal/gxfacebook")
    for name in DEFAULT_COOKIE_FILENAMES:
        candidates.append(fixed_base / name)
        candidates.append(PROJECT_DIR / name)
        candidates.append(Path.cwd() / name)

    # Return the first existing non-empty file
    seen: set[str] = set()
    for candidate in candidates:
        cand_str = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if cand_str in seen:
            continue
        seen.add(cand_str)
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return str(candidate.resolve())
        except OSError:
            continue

    return None


def parse_netscape_cookies(content: str) -> dict[str, str]:
    """Parse Netscape format cookies text into a key-value dictionary.

    Netscape format specifies tab-separated values:
    domain, include_subdomains, path, secure, expires, name, value
    Lines starting with '#HttpOnly_' have that prefix stripped before parsing.
    Other lines starting with '#' or empty lines are ignored.
    """
    cookies: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Handle HttpOnly cookies
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) >= 7:
            name = parts[5].strip()
            val = parts[6].strip()
            if name:
                cookies[name] = val
        elif len(parts) == 6:
            name = parts[5].strip()
            if name:
                cookies[name] = ""

    return cookies


def get_httpx_cookies(cookie_file: str | Path | None = None) -> dict[str, str]:
    """Parse cookies from cookie_file or the default discovered cookie file.

    Returns a dictionary suitable for passing to httpx.AsyncClient(cookies=...).
    """
    path_str = get_cookie_file(cookie_file) if cookie_file else get_cookie_file()
    if not path_str:
        return {}

    try:
        content = Path(path_str).read_text(encoding="utf-8", errors="ignore")
        return parse_netscape_cookies(content)
    except Exception as e:
        logger.warning(f"Failed to read or parse cookie file {path_str}: {e}")
        return {}


def has_valid_cookies(cookie_file: str | Path | None = None) -> bool:
    """Return True if a valid non-empty cookie file exists and contains cookies."""
    cookies = get_httpx_cookies(cookie_file)
    return len(cookies) > 0
