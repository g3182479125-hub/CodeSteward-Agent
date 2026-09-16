"""Signed media helpers for the WebUI HTTP surface."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.config.paths import get_media_dir
from nanobot.utils.helpers import safe_filename
from nanobot.webui.http_utils import (
    case_insensitive_header as _case_insensitive_header,
)
from nanobot.webui.http_utils import (
    http_error as _http_error,
)
from nanobot.webui.http_utils import (
    http_response as _http_response,
)

MediaDirProvider = Callable[[str | None], Path]
SignedMediaPath = Callable[[Path], dict[str, str] | None]


def b64url_encode(data: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """Reverse of :func:`b64url_encode`; caller handles decode errors."""
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _default_media_dir(channel: str | None = None) -> Path:
    return get_media_dir(channel)


# Allowed MIME types we actually serve from the media endpoint. Anything
# outside this set is degraded to ``application/octet-stream`` so an
# attacker who somehow gets a signed URL for an unexpected file type can't
# trick the browser into sniffing executable content.
_MEDIA_ALLOWED_MIMES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/svg+xml",
    "video/mp4",
    "video/webm",
    "video/quicktime",
})
MEDIA_URL_TTL_SECONDS = 300
_SVG_MEDIA_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
    ),
)

_BYTE_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _verify_opened_path(file_fd: int, root: Path) -> None:
    """Reject a Windows handle whose final path escaped the validated root."""
    if os.name != "nt":
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final_path(
        wintypes.HANDLE(msvcrt.get_osfhandle(file_fd)),
        buffer,
        len(buffer),
        0,
    )
    if not length or length >= len(buffer):
        raise OSError("could not resolve the opened Windows file handle")
    final_name = buffer.value
    if final_name.startswith("\\\\?\\UNC\\"):
        final_name = "\\" + final_name[7:]
    elif final_name.startswith("\\\\?\\"):
        final_name = final_name[4:]
    try:
        common = os.path.commonpath(
            (
                os.path.normcase(os.path.abspath(str(root))),
                os.path.normcase(os.path.abspath(final_name)),
            )
        )
    except ValueError as exc:
        raise OSError("opened Windows file handle is on another volume") from exc
    if common != os.path.normcase(os.path.abspath(str(root))):
        raise OSError("opened Windows file handle escaped the media root")


def _open_media_file(media_root: Path, relative_path: str, fallback: Path) -> int:
    """Open one media file and return a stable read handle.

    POSIX callers get descriptor-relative, no-follow traversal for every path
    component. Windows does not expose the equivalent through Python's stdlib,
    so its fallback opens the validated absolute path and then verifies the
    operating system's final handle path remains under the media root.
    """
    parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise FileNotFoundError(relative_path)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    non_blocking = getattr(os, "O_NONBLOCK", 0)
    if os.name == "posix" and no_follow and os.open in os.supports_dir_fd:
        root_fd = os.open(media_root, os.O_RDONLY | directory_flag | no_follow)
        current_fd = root_fd
        file_fd = -1
        try:
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | directory_flag | no_follow,
                    dir_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            file_fd = os.open(
                parts[-1], os.O_RDONLY | no_follow | non_blocking, dir_fd=current_fd
            )
            _verify_opened_path(file_fd, media_root)
        except BaseException:
            if file_fd >= 0:
                os.close(file_fd)
            os.close(current_fd)
            raise
        os.close(current_fd)
        return file_fd
    file_fd = os.open(fallback, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        _verify_opened_path(file_fd, media_root)
    except BaseException:
        os.close(file_fd)
        raise
    return file_fd


def _parse_single_byte_range(range_header: str, size: int) -> tuple[int, int]:
    """Parse a single HTTP byte range for signed media responses."""
    if size <= 0 or "," in range_header:
        raise ValueError("invalid byte range")
    m = _BYTE_RANGE_RE.fullmatch(range_header.strip())
    if m is None:
        raise ValueError("invalid byte range")
    start_text, end_text = m.groups()
    if not start_text and not end_text:
        raise ValueError("invalid byte range")
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid byte range")
        start = max(size - suffix_length, 0)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
        if start >= size or start > end:
            raise ValueError("invalid byte range")
        end = min(end, size - 1)
    return start, end


def sign_media_path(
    abs_path: Path,
    *,
    secret: bytes,
    media_dir: MediaDirProvider = _default_media_dir,
    expires_in_s: int = MEDIA_URL_TTL_SECONDS,
) -> str | None:
    """Return a signed ``/api/media/<sig>/<payload>`` URL for a media-root path."""
    if isinstance(expires_in_s, bool) or not isinstance(expires_in_s, int) or expires_in_s <= 0:
        return None
    try:
        media_root = media_dir(None).resolve()
        rel = abs_path.resolve().relative_to(media_root)
    except (OSError, ValueError):
        return None
    payload_data = json.dumps(
        {"expires_at": int(time.time()) + expires_in_s, "path": rel.as_posix()},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload = b64url_encode(payload_data)
    mac = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
    return f"/api/media/{b64url_encode(mac)}/{payload}"


def sign_or_stage_media_path(
    path: Path,
    *,
    secret: bytes,
    media_dir: MediaDirProvider = _default_media_dir,
    logger: Any | None = None,
) -> dict[str, str] | None:
    """Sign an existing media-root path, or stage an arbitrary file before signing."""
    signed = sign_media_path(path, secret=secret, media_dir=media_dir)
    if signed is not None:
        return {"url": signed, "name": path.name}
    staged_tmp: Path | None = None
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return None
        source_stat = resolved.stat()
        target_dir = media_dir("websocket")
        safe_name = safe_filename(path.name) or "attachment"
        source_version = "\0".join((
            os.path.normcase(str(resolved)),
            str(source_stat.st_size),
            str(source_stat.st_mtime_ns),
            str(source_stat.st_ctime_ns),
        ))
        source_digest = hashlib.sha256(source_version.encode("utf-8")).hexdigest()[:20]
        staged = target_dir / f"{source_digest}-{safe_name}"
        if not staged.is_file() or staged.stat().st_size != source_stat.st_size:
            staged_tmp = target_dir / f".{source_digest}-{uuid.uuid4().hex}.tmp"
            shutil.copyfile(resolved, staged_tmp)
            staged_tmp.replace(staged)
    except OSError as exc:
        if logger is not None:
            logger.warning("failed to stage outbound media {}: {}", path, exc)
        return None
    finally:
        if staged_tmp is not None:
            staged_tmp.unlink(missing_ok=True)
    signed = sign_media_path(staged, secret=secret, media_dir=media_dir)
    if signed is None:
        return None
    return {"url": signed, "name": path.name}


def media_attachment_kind(name: str) -> str:
    """Infer the WebUI media attachment kind from a filename."""
    mime, _ = mimetypes.guess_type(name)
    if mime and mime.startswith("video/"):
        return "video"
    if mime and mime.startswith("image/"):
        return "image"
    return "file"


def signed_media_attachments(
    paths: list[str],
    *,
    sign_path: SignedMediaPath,
) -> list[dict[str, Any]]:
    """Map persisted media paths to WebUI attachment dicts with fresh signed URLs."""
    out: list[dict[str, Any]] = []
    for pstr in paths:
        path = Path(pstr)
        att = sign_path(path)
        if att is None:
            continue
        url = att.get("url")
        if not url:
            continue
        name = att.get("name") or path.name
        out.append({"kind": media_attachment_kind(name), "url": url, "name": name})
    return out


def serve_signed_media(
    sig: str,
    payload: str,
    *,
    secret: bytes,
    request: WsRequest | None = None,
    media_dir: MediaDirProvider = _default_media_dir,
) -> Response:
    """Serve a signed media URL, including browser-friendly byte ranges."""
    try:
        provided_mac = b64url_decode(sig)
    except (ValueError, binascii.Error):
        return _http_error(401, "invalid signature")
    expected_mac = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(expected_mac, provided_mac):
        return _http_error(401, "invalid signature")
    try:
        payload_data = json.loads(b64url_decode(payload).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return _http_error(400, "invalid payload")
    if not isinstance(payload_data, dict):
        return _http_error(400, "invalid payload")
    rel_str = payload_data.get("path")
    expires_at = payload_data.get("expires_at")
    if (
        not isinstance(rel_str, str)
        or not rel_str
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
    ):
        return _http_error(400, "invalid payload")
    now = int(time.time())
    if expires_at <= now:
        return _http_error(401, "expired signature")
    try:
        media_root = media_dir(None).resolve()
        candidate = (media_root / rel_str).resolve()
        candidate.relative_to(media_root)
    except (OSError, ValueError):
        return _http_error(404, "not found")
    if not candidate.is_file():
        return _http_error(404, "not found")

    mime, _ = mimetypes.guess_type(candidate.name)
    if mime not in _MEDIA_ALLOWED_MIMES:
        mime = "application/octet-stream"
    cache_seconds = min(MEDIA_URL_TTL_SECONDS, max(0, expires_at - now))
    common_headers = [
        ("Accept-Ranges", "bytes"),
        ("Cache-Control", f"private, max-age={cache_seconds}"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    ]
    if mime == "image/svg+xml":
        common_headers.extend(_SVG_MEDIA_HEADERS)
    try:
        file_fd = _open_media_file(media_root, rel_str, candidate)
        try:
            file_handle = os.fdopen(file_fd, "rb")
        except BaseException:
            os.close(file_fd)
            raise
        try:
            file_stat = os.fstat(file_handle.fileno())
        except BaseException:
            file_handle.close()
            raise
    except (OSError, ValueError):
        return _http_error(500, "read error")
    if not stat.S_ISREG(file_stat.st_mode):
        file_handle.close()
        return _http_error(404, "not found")
    size = file_stat.st_size

    range_header = _case_insensitive_header(request.headers, "Range") if request else ""
    if range_header:
        try:
            start, end = _parse_single_byte_range(range_header, size)
        except ValueError:
            file_handle.close()
            return _http_response(
                b"range not satisfiable",
                status=416,
                extra_headers=[
                    ("Accept-Ranges", "bytes"),
                    ("Content-Range", f"bytes */{size}"),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )
        try:
            length = end - start + 1
            file_handle.seek(start)
            body = file_handle.read(length)
        except OSError:
            file_handle.close()
            return _http_error(500, "read error")
        file_handle.close()
        return _http_response(
            body,
            status=206,
            content_type=mime,
            extra_headers=[
                *common_headers,
                ("Content-Range", f"bytes {start}-{end}/{size}"),
            ],
        )

    try:
        body = file_handle.read()
    except OSError:
        file_handle.close()
        return _http_error(500, "read error")
    file_handle.close()
    return _http_response(body, content_type=mime, extra_headers=common_headers)
