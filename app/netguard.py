"""Host/Origin checks that protect the localhost dashboards from CSRF and DNS rebinding."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _hostname(netloc: str) -> str | None:
    try:
        return urlsplit(f"//{netloc}").hostname
    except ValueError:
        return None


def host_allowed(host_header: str | None, bind_host: str | None = None) -> bool:
    name = _hostname(host_header or "")
    if not name:
        return False
    return name in LOCAL_HOSTS or (bool(bind_host) and name == bind_host.lower())


def origin_allowed(origin: str | None, host_header: str | None) -> bool:
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return (parts.scheme.lower() in ("http", "https") and bool(host_header)
            and parts.netloc.lower() == host_header.lower())


def request_allowed(method: str, headers: Mapping[str, str], bind_host: str | None = None) -> bool:
    host = headers.get("host")
    if not host_allowed(host, bind_host):
        return False
    return method.upper() in SAFE_METHODS or origin_allowed(headers.get("origin"), host)


def install(app, bind_host: Callable[[], str | None]) -> None:
    """Reject HTTP requests with a foreign Host, or a cross-origin Origin on unsafe methods."""
    @app.middleware("http")
    async def _guard(request, call_next):
        if not request_allowed(request.method, request.headers, bind_host()):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)
