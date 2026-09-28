from datetime import datetime
from pathlib import Path
from typing import Optional
from sanic.request import Request

DEFAULT_IP_LOG_PATH = Path("ips.log")


def get_client_ip(request: Request) -> str:
    """
    Extract the real client IP address from request headers.
    When behind Cloudflare proxy -> Nginx proxy -> Sanic,
    the real IP is provided in headers:
      - 'CF-Connecting-IP' (Cloudflare visitor IP)
      - 'X-Forwarded-For' (comma-separated list; first IP is the client)
      - 'X-Real-IP' (set by Nginx)
    """
    headers = request.headers

    # 1. Cloudflare header
    cf_ip = headers.get("cf-connecting-ip")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()

    # 2. X-Forwarded-For header
    xff = headers.get("x-forwarded-for")
    if xff and xff.strip():
        client_ip = xff.split(",")[0].strip()
        if client_ip:
            return client_ip

    # 3. X-Real-IP header
    x_real_ip = headers.get("x-real-ip")
    if x_real_ip and x_real_ip.strip():
        return x_real_ip.strip()

    # 4. Fallback to direct connection remote address
    return request.remote_addr or getattr(request, "ip", "") or "unknown"


def log_ip_address(ip: str, path: str = "", method: str = "", log_file: Path = DEFAULT_IP_LOG_PATH) -> None:
    """
    Append an IP address to the log file using the append flag ('a').
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{timestamp} - {ip}"
    if method or path:
        entry += f" - {method} {path}"
    entry += "\n"

    # Open with the append flag 'a'
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(entry)


def log_request_ip(request: Request, log_file: Path = DEFAULT_IP_LOG_PATH) -> str:
    """
    Helper to extract IP from request and append it to the log file.
    """
    ip = get_client_ip(request)
    log_ip_address(ip=ip, path=request.path, method=request.method, log_file=log_file)
    return ip
