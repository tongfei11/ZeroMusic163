"""Fetch bounded NetEase Cloud Music artwork for the native UI."""


from __future__ import annotations
from http.client import HTTPException
from io import BytesIO
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, ImageOps

_CDN_DOMAINS = ("music.126.net", "music.163.com")
_MAX_BYTES = 512 * 1024
_MAX_PIXELS = 4_000_000
_TIMEOUT = 3


def _cdn_url(url, size=None):
    if not url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("Invalid artwork URL")
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if not any(hostname == domain or hostname.endswith("." + domain) for domain in _CDN_DOMAINS):
        raise ValueError("Unsupported artwork host")
    default_port = 80 if parts.scheme == "http" else 443
    if (parts.scheme not in ("http", "https") or parts.username is not None
            or parts.password is not None or parts.port not in (None, default_port)):
        raise ValueError("Invalid artwork origin")
    # A CDN signature can cover the whole query. Keep a supplied query intact.
    query = parts.query or (f"param={size}y{size}" if size else "")
    return urlunsplit(("https", hostname, parts.path, query, ""))


class _CDNRedirectHandler(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _cdn_url(newurl))


def download_thumbnail(url: str, size: int) -> Image.Image | None:
    """Return a square RGB thumbnail or None if the asset cannot be shown."""
    if not isinstance(url, str) or not isinstance(size, int) or not 1 <= size <= 256:
        return None
    try:
        request = Request(_cdn_url(url, size), headers={"Accept": "image/*"})
        deadline = monotonic() + _TIMEOUT
        with build_opener(_CDNRedirectHandler()).open(request, timeout=_TIMEOUT) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > _MAX_BYTES:
                return None
            data = bytearray()
            while monotonic() < deadline:
                chunk = response.read1(min(64 * 1024, _MAX_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _MAX_BYTES:
                    return None
            else:
                return None

        with Image.open(BytesIO(data), formats=("JPEG", "PNG", "WEBP", "GIF")) as source:
            if source.width * source.height > _MAX_PIXELS:
                return None
            return ImageOps.fit(source.convert("RGB"), (size, size), Image.Resampling.LANCZOS)
    except (OSError, HTTPException, ValueError, SyntaxError, Image.DecompressionBombError):
        return None
