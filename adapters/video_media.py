"""Duration-tiered Bilibili visual, audio-STT fallback, and danmaku acquisition layer.

Strictly read-only, SSRF-guarded, bounded by explicit duration/size/frame budgets,
and guaranteed to keep all temporary artifacts inside a dedicated per-fetch temp dir.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import io
import math
import os
import re
import shutil
import tempfile
import threading
import time
import weakref
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from astrbot.api import logger

from . import ssrf as ssrf_mod
from .ssrf import (
    AdapterHTTPError,
    AdapterRateLimitError,
    SSRFBlockedError,
    clean_external_text,
    clean_inline_field,
    redact_error_text,
    safe_http_get,
    validate_url_async,
)

BILI_VIDEOSHOT_URL = "https://api.bilibili.com/x/player/videoshot"
_TEMP_PREFIX = "horizons_bili_"
_RANGE_CHUNK_BYTES = 4 * 1024 * 1024
_MIN_FREE_DISK_BYTES = 512 * 1024 * 1024  # 512 MB minimum free space before download
_STALE_TEMP_TTL_SECONDS = 3600.0  # 1 hour stale temp dir cleanup threshold
_MEDIA_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()
_MEDIA_SEMAPHORE_LOCK = threading.Lock()
_IN_MEDIA_SEMAPHORE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "horizons_in_media_semaphore", default=False
)
_PTS_TIME_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)
_REPEATED_CHAR_RE = re.compile(r"^(.)\1{2,}$")
_LOW_SIGNAL_DANMAKU_RE = re.compile(
    r"^(?:[623]+|h+|w+|？+|\?+|！+|!+|\.+|。+|…+|啊+|哈+|卧槽|牛逼|nb|awsl|草+|好家伙|确实|笑死)+$",
    re.IGNORECASE,
)

BILI_MEDIA_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "*/*",
}

DEFAULT_MEDIA_OPTIONS: dict[str, Any] = {
    "visual_enabled": True,
    "prefer_native_video": False,
    "native_max_duration_sec": 480,
    "native_max_mb": 15.0,
    "medium_max_duration_sec": 2700,
    "max_frames": 12,
    "subtitle_max_frames": 6,
    "frame_max_width": 960,
    "subtitle_frame_max_width": 640,
    "frame_jpeg_quality": 80,
    "max_download_mb": 60.0,
    "danmaku_enabled": False,
    "danmaku_max_items": 15,
    "audio_fallback_enabled": True,
    "audio_max_duration_sec": 1800,
    "audio_max_mb": 20.0,
}

# Bilibili DASH quality preference order: 480P (readable hard-subs, small) -> 360P -> 720P -> 1080P
_VIDEO_QUALITY_RANK = {
    32: 0,  # 480P
    16: 1,  # 360P
    64: 2,  # 720P
    80: 3,  # 1080P
    74: 4,  # 720P60
    112: 5,  # 1080P+
    116: 6,  # 1080P60
}
_VIDEO_CODEC_RANK = {
    7: 0,  # AVC / H.264 (fastest ffmpeg decode)
    12: 1,  # HEVC / H.265
    13: 2,  # AV1
}
_AUDIO_QUALITY_RANK = {
    30216: 0,  # 64K
    30232: 1,  # 132K
    30280: 2,  # 192K
}


class MediaSizeLimitExceeded(RuntimeError):
    """Raised when a remote or muxed media artifact exceeds configured size budget."""


class MediaDownloadError(RuntimeError):
    """Raised when media download fails after bounded retries."""

    def __init__(
        self,
        message: str,
        *,
        is_transient: bool = True,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.is_transient = bool(is_transient)
        self.status_code = status_code


class MediaUnavailableError(RuntimeError):
    """Raised when a video stream is permanently unavailable (VIP/paid/deleted/missing)."""

    def __init__(self, reason: str, *, code: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = int(code)


def normalize_media_options(
    options: Mapping[str, Any] | None,
    *,
    default_visual_enabled: bool = True,
) -> dict[str, Any]:
    """Return normalized, bounded media acquisition options."""
    opts = dict(DEFAULT_MEDIA_OPTIONS)
    opts["visual_enabled"] = bool(default_visual_enabled)
    if isinstance(options, Mapping):
        for key in DEFAULT_MEDIA_OPTIONS:
            if key in options and options[key] is not None:
                opts[key] = options[key]

    def _clamp_int(val: Any, lo: int, hi: int, fallback: int) -> int:
        try:
            return max(lo, min(hi, int(val)))
        except (TypeError, ValueError):
            return fallback

    def _clamp_float(val: Any, lo: float, hi: float, fallback: float) -> float:
        try:
            return max(lo, min(hi, float(val)))
        except (TypeError, ValueError):
            return fallback

    opts["visual_enabled"] = bool(opts["visual_enabled"])
    opts["prefer_native_video"] = bool(opts["prefer_native_video"])
    opts["native_max_duration_sec"] = _clamp_int(
        opts["native_max_duration_sec"], 30, 1800, 480
    )
    opts["native_max_mb"] = _clamp_float(opts["native_max_mb"], 2.0, 50.0, 15.0)
    opts["medium_max_duration_sec"] = _clamp_int(
        opts["medium_max_duration_sec"],
        opts["native_max_duration_sec"],
        7200,
        2700,
    )
    opts["max_frames"] = _clamp_int(opts["max_frames"], 1, 20, 12)
    opts["subtitle_max_frames"] = _clamp_int(
        opts["subtitle_max_frames"], 1, opts["max_frames"], min(6, opts["max_frames"])
    )
    opts["frame_max_width"] = _clamp_int(opts["frame_max_width"], 320, 1280, 960)
    opts["subtitle_frame_max_width"] = _clamp_int(
        opts["subtitle_frame_max_width"],
        320,
        opts["frame_max_width"],
        min(640, opts["frame_max_width"]),
    )
    opts["frame_jpeg_quality"] = _clamp_int(opts["frame_jpeg_quality"], 50, 95, 80)
    opts["max_download_mb"] = _clamp_float(opts["max_download_mb"], 5.0, 200.0, 60.0)
    opts["danmaku_enabled"] = bool(opts["danmaku_enabled"])
    opts["danmaku_max_items"] = _clamp_int(opts["danmaku_max_items"], 1, 30, 15)
    opts["audio_fallback_enabled"] = bool(opts["audio_fallback_enabled"])
    opts["audio_max_duration_sec"] = _clamp_int(
        opts["audio_max_duration_sec"], 60, 3600, 1800
    )
    opts["audio_max_mb"] = _clamp_float(opts["audio_max_mb"], 2.0, 60.0, 20.0)
    return opts


def resolve_keyframe_budget(
    opts: Mapping[str, Any],
    *,
    has_subtitle: bool,
    duration_sec: float = 0.0,
) -> tuple[int, int]:
    """Return adaptive ``(max_frames, frame_max_width)`` based on subtitle availability.

    Subtitle-backed videos use a compact budget (4-6 frames at <=640px) to cut visual
    token cost while keeping scene grounding; subtitle-less videos keep the full budget.
    """
    full_frames = max(1, min(20, int(opts.get("max_frames", 12) or 12)))
    full_width = max(320, min(1280, int(opts.get("frame_max_width", 960) or 960)))
    if not has_subtitle:
        return full_frames, full_width
    sub_cap = max(
        1,
        min(
            full_frames,
            int(opts.get("subtitle_max_frames", min(6, full_frames)) or 6),
        ),
    )
    sub_width = max(
        320,
        min(
            full_width,
            int(opts.get("subtitle_frame_max_width", min(640, full_width)) or 640),
        ),
    )
    dur = float(duration_sec or 0.0)
    if 0 < dur <= 180.0:
        eff_frames = min(sub_cap, 4)
    elif 0 < dur <= 600.0:
        eff_frames = min(sub_cap, 5)
    else:
        eff_frames = sub_cap
    return max(1, eff_frames), sub_width


def format_timestamp(seconds: float) -> str:
    """Format seconds into compact `mm:ss` (or `h:mm:ss` for >= 1 hour)."""
    total = max(0, int(round(float(seconds or 0.0))))
    hrs, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def make_temp_dir(bvid: str = "") -> Path:
    """Create a dedicated temporary directory for a single video fetch."""
    safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "", str(bvid or ""))[:20]
    prefix = f"{_TEMP_PREFIX}{safe_tag}_" if safe_tag else _TEMP_PREFIX
    return Path(tempfile.mkdtemp(prefix=prefix))


def cleanup_temp_dir(path: str | Path | None) -> None:
    """Idempotently remove a horizons-owned temp directory inside the system temp root."""
    if not path:
        return
    try:
        target = Path(path).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if target == temp_root or not target.name.startswith(_TEMP_PREFIX):
            return
        if temp_root not in target.parents:
            return
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    except Exception:
        return


def get_media_pipeline_semaphore() -> asyncio.Semaphore:
    """Return a per-event-loop Semaphore(1) serializing heavy media download/transcode."""
    loop = asyncio.get_running_loop()
    with _MEDIA_SEMAPHORE_LOCK:
        sem = _MEDIA_SEMAPHORES.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(1)
            _MEDIA_SEMAPHORES[loop] = sem
        return sem


@contextlib.asynccontextmanager
async def _media_pipeline_slot():
    if _IN_MEDIA_SEMAPHORE.get():
        yield
        return
    sem = get_media_pipeline_semaphore()
    async with sem:
        token = _IN_MEDIA_SEMAPHORE.set(True)
        try:
            yield
        finally:
            _IN_MEDIA_SEMAPHORE.reset(token)


def _ensure_free_disk_space(
    target_dir: Path,
    *,
    min_free_bytes: int = _MIN_FREE_DISK_BYTES,
) -> None:
    """Verify sufficient free disk space before downloading media artifacts."""
    probe = target_dir
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    if usage.free < min_free_bytes:
        free_mb = usage.free / (1024 * 1024)
        need_mb = min_free_bytes / (1024 * 1024)
        logger.warning(
            "[DynamicsHorizons] Skipping media download due to low disk space: free=%.1fMB < required=%.1fMB",
            free_mb,
            need_mb,
        )
        raise MediaDownloadError(
            f"磁盘可用空间不足 ({free_mb:.1f}MB < {need_mb:.0f}MB)",
            is_transient=True,
        )


def cleanup_stale_temp_dirs(
    *,
    max_age_seconds: float = _STALE_TEMP_TTL_SECONDS,
    now: float | None = None,
    temp_root: str | Path | None = None,
    protected_dirs: Iterable[str | Path] | None = None,
) -> int:
    """Remove expired `horizons_bili_*` temp dirs whose mtime exceeds `max_age_seconds`."""
    root = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    try:
        resolved_root = root.resolve()
    except OSError:
        return 0
    if not resolved_root.is_dir():
        return 0
    protected: set[Path] = set()
    for p in protected_dirs or ():
        if not p:
            continue
        try:
            protected.add(Path(p).resolve())
        except OSError:
            continue
    cutoff = (float(now) if now is not None else time.time()) - max(
        0.0, float(max_age_seconds)
    )
    removed = 0
    try:
        entries = list(resolved_root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(_TEMP_PREFIX):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            resolved = entry.resolve()
            if resolved in protected or resolved_root not in resolved.parents:
                continue
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(resolved, ignore_errors=True)
                if not resolved.exists():
                    removed += 1
        except OSError:
            continue
    if removed > 0:
        logger.info(
            "[DynamicsHorizons] Cleaned up %d stale temp directories under %s",
            removed,
            resolved_root,
        )
    return removed


def cleanup_media_bundle(bundle: Mapping[str, Any] | None) -> None:
    """Safely clean up temp files referenced by a `media_bundle` dict."""
    if not isinstance(bundle, Mapping):
        return
    cleanup_fn = bundle.get("cleanup")
    if callable(cleanup_fn):
        try:
            cleanup_fn()
            return
        except Exception:
            pass
    cleanup_temp_dir(bundle.get("temp_dir"))


def build_empty_media_bundle(
    *,
    watch_mode: str = "metadata_only",
    content_origin: str = "bilibili_metadata_only",
    duration_sec: int = 0,
    has_subtitle: bool = False,
    subtitle_source: str = "",
    temp_dir: str = "",
    skip_reason: str = "",
    is_transient_error: bool = False,
    is_permanent_unavailable: bool = False,
    error_detail: str = "",
) -> dict[str, Any]:
    """Construct a normalized `media_bundle` dict with idempotent cleanup & fallback hooks."""
    cleaned_dir = str(temp_dir or "")

    def _cleanup() -> None:
        cleanup_temp_dir(cleaned_dir)

    async def _noop_fallback_frames() -> list[dict[str, Any]]:
        return []

    return {
        "watch_mode": watch_mode,
        "content_origin": content_origin,
        "has_visual": False,
        "has_subtitle": bool(has_subtitle),
        "has_audio_transcript": False,
        "has_danmaku": False,
        "duration_sec": max(0, int(duration_sec or 0)),
        "native_video_path": "",
        "native_video_size_bytes": 0,
        "frames": [],
        "frame_paths": [],
        "videoshot_paths": [],
        "frame_count": 0,
        "subtitle_source": subtitle_source,
        "danmaku_sample": [],
        "danmaku_spikes_sec": [],
        "temp_dir": cleaned_dir,
        "cleanup": _cleanup,
        "ensure_fallback_frames": _noop_fallback_frames,
        "skip_reason": skip_reason,
        "is_transient_error": bool(is_transient_error),
        "is_permanent_unavailable": bool(is_permanent_unavailable),
        "error_detail": error_detail,
    }


def _is_bili_cookie_host(url: str) -> bool:
    """Only attach read-only Bilibili Cookie header to first-party `bilibili.com` hosts."""
    host = (urlsplit(url).hostname or "").lower()
    return host == "bilibili.com" or host.endswith(".bilibili.com")


def _parse_content_range(
    header_val: str, offset: int, requested_end: int, known_size: int | None
) -> tuple[int, int]:
    match = _CONTENT_RANGE_RE.fullmatch(str(header_val or "").strip())
    if not match:
        raise MediaDownloadError("分段响应缺少有效的 Content-Range", is_transient=True)
    start, end, total = map(int, match.groups())
    if start != offset or not (start <= end < total) or end > requested_end:
        raise MediaDownloadError("媒体分段位置不符", is_transient=True)
    if known_size is not None and total != known_size:
        raise MediaDownloadError("媒体总长度发生变化", is_transient=True)
    return end + 1, total


def _validate_media_content_type(content_type: str, content_encoding: str) -> None:
    media_type = str(content_type or "").partition(";")[0].lower().strip()
    if media_type.startswith("text/") or media_type in {
        "application/json",
        "application/xml",
    }:
        raise MediaDownloadError(f"节点返回非媒体类型：{media_type}", is_transient=True)
    enc = str(content_encoding or "identity").lower().strip()
    if enc not in {"", "identity"}:
        raise MediaDownloadError(f"节点返回不支持的内容编码：{enc}", is_transient=True)


async def _stream_single_url_to_file(
    url: str,
    part_path: Path,
    *,
    max_bytes: int,
    timeout: float,
    cookie_header: str,
    secrets: Sequence[str],
    max_redirects: int = 5,
) -> int:
    """SSRF-guarded range-chunked download of a single media URL into `part_path`."""
    proxy_url = ssrf_mod._resolve_proxy_url(None)
    current_url = await validate_url_async(
        url, allow_private=False, require_dns=(proxy_url is None)
    )
    base_headers = dict(BILI_MEDIA_HEADERS)
    base_headers["Accept-Encoding"] = "identity"
    if cookie_header and _is_bili_cookie_host(current_url):
        base_headers["Cookie"] = cookie_header

    # Offline test transport path: delegates through ssrf.safe_http_get
    if ssrf_mod._http_transport is not None:
        resp = await safe_http_get(
            current_url,
            headers=base_headers,
            timeout=timeout,
            max_bytes=max_bytes,
            allow_private=False,
            secrets_to_redact=secrets,
        )
        _validate_media_content_type(
            resp.header("content-type"), resp.header("content-encoding")
        )
        if not resp.body:
            raise MediaDownloadError("媒体内容为空", is_transient=True)
        if len(resp.body) > max_bytes:
            raise MediaSizeLimitExceeded(f"超过下载大小限制 ({max_bytes} bytes)")
        await asyncio.to_thread(part_path.write_bytes, resp.body)
        return len(resp.body)

    client_timeout = aiohttp.ClientTimeout(
        total=max(5.0, float(timeout)),
        connect=min(10.0, max(3.0, float(timeout) / 3.0)),
    )
    connector = aiohttp.TCPConnector(
        resolver=ssrf_mod._CheckedResolver(allow_private=False),
        use_dns_cache=False,
    )
    async with aiohttp.ClientSession(
        timeout=client_timeout, connector=connector, trust_env=False
    ) as session:
        # Resolve any redirects manually with SSRF hop validation on the first range request
        offset = 0
        total_size: int | None = None
        active_url = current_url
        active_headers = dict(base_headers)

        with part_path.open("wb") as fh:
            while total_size is None or offset < total_size:
                end = offset + _RANGE_CHUNK_BYTES - 1
                if total_size is not None:
                    end = min(end, total_size - 1)

                req_headers = dict(active_headers)
                req_headers["Range"] = f"bytes={offset}-{end}"

                # Follow redirects safely on hop 0 (or any chunk if CDN redirects)
                hop_url = active_url
                for hop in range(max_redirects + 1):
                    hop_url = await validate_url_async(
                        hop_url, allow_private=False, require_dns=(proxy_url is None)
                    )
                    if not _is_bili_cookie_host(hop_url):
                        req_headers.pop("Cookie", None)
                    async with session.get(
                        hop_url,
                        headers=req_headers,
                        allow_redirects=False,
                        timeout=client_timeout,
                        proxy=proxy_url,
                    ) as resp:
                        if resp.status in {301, 302, 303, 307, 308}:
                            loc = resp.headers.get("Location", "").strip()
                            if not loc or hop >= max_redirects:
                                raise MediaDownloadError(
                                    "媒体重定向次数过多或缺少 Location",
                                    is_transient=True,
                                )
                            next_url = urljoin(hop_url, loc)
                            if ssrf_mod._origin_tuple(next_url) != ssrf_mod._origin_tuple(
                                hop_url
                            ):
                                req_headers.pop("Cookie", None)
                                active_headers.pop("Cookie", None)
                            hop_url = next_url
                            active_url = next_url
                            continue

                        if resp.status == 429:
                            ra = ssrf_mod.parse_retry_after(
                                resp.headers.get("Retry-After")
                            )
                            raise AdapterRateLimitError(
                                "媒体下载触发 429 限流",
                                retry_after=ra,
                                status_code=429,
                            )
                        if resp.status >= 400:
                            is_trans = resp.status >= 500 or resp.status in {
                                403,
                                408,
                                429,
                            }
                            raise MediaDownloadError(
                                f"HTTP {resp.status}",
                                is_transient=is_trans,
                                status_code=resp.status,
                            )

                        _validate_media_content_type(
                            resp.headers.get("Content-Type", ""),
                            resp.headers.get("Content-Encoding", "identity"),
                        )

                        next_offset: int | None
                        if resp.status == 206:
                            next_offset, total_size = _parse_content_range(
                                resp.headers.get("Content-Range", ""),
                                offset,
                                end,
                                total_size,
                            )
                        elif resp.status == 200 and offset == 0:
                            cl = resp.headers.get("Content-Length", "").strip()
                            if cl and not cl.isdecimal():
                                raise MediaDownloadError(
                                    "媒体 Content-Length 无效", is_transient=True
                                )
                            total_size = next_offset = int(cl) if cl else None
                        else:
                            raise MediaDownloadError(
                                f"节点返回非预期状态码 {resp.status}", is_transient=True
                            )

                        if (
                            total_size is not None
                            and total_size > max_bytes
                        ):
                            raise MediaSizeLimitExceeded(
                                f"媒体大小 {total_size} 超过上限 {max_bytes}"
                            )

                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            offset += len(chunk)
                            if offset > max_bytes:
                                raise MediaSizeLimitExceeded(
                                    f"下载已超过大小上限 {max_bytes}"
                                )
                            if next_offset is not None and offset > next_offset:
                                raise MediaDownloadError(
                                    "收到的媒体分段过长", is_transient=True
                                )
                            await asyncio.to_thread(fh.write, chunk)

                        if offset == 0 or (
                            next_offset is not None and offset != next_offset
                        ):
                            raise MediaDownloadError("媒体数据不完整", is_transient=True)

                        if resp.status == 200:
                            return offset
                        break
        return offset


async def download_media_to_file(
    urls: Sequence[str],
    output_path: Path,
    *,
    max_bytes: int,
    timeout: float = 60.0,
    total_timeout: float | None = None,
    cookie_header: str = "",
    secrets: Sequence[str] = (),
) -> int:
    """Download a Bilibili media track trying primary + backup CDN URLs with SSRF protection.

    Enforces a single total deadline across all ranged chunks, backup URLs, and retry
    attempts so slow streams cannot stack timeouts and starve the global Semaphore(1).
    """
    deduped = list(dict.fromkeys(str(u).strip() for u in urls if str(u or "").strip()))
    if not deduped:
        raise MediaDownloadError("缺少可用媒体下载地址", is_transient=False)

    await asyncio.to_thread(_ensure_free_disk_space, output_path.parent)

    per_req_timeout = max(1.0, float(timeout))
    effective_total = (
        max(0.05, float(total_timeout))
        if total_timeout is not None
        else max(per_req_timeout, min(per_req_timeout * 1.5, 180.0))
    )
    part_path = output_path.with_suffix(output_path.suffix + ".part")
    last_err: Exception | None = None
    candidates = deduped
    async with _media_pipeline_slot():
        deadline = time.monotonic() + effective_total
        try:
            for attempt in range(2):
                retry_urls: list[str] = []
                for raw_url in candidates:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        last_err = MediaDownloadError(
                            f"媒体下载超过总超时上限（{effective_total:.1f}s）",
                            is_transient=True,
                        )
                        retry_urls.clear()
                        break
                    url = ("https:" + raw_url) if raw_url.startswith("//") else raw_url
                    try:
                        async with asyncio.timeout(remaining):
                            written = await _stream_single_url_to_file(
                                url,
                                part_path,
                                max_bytes=max_bytes,
                                timeout=min(per_req_timeout, remaining),
                                cookie_header=cookie_header,
                                secrets=secrets,
                            )
                        part_path.replace(output_path)
                        return written
                    except (
                        SSRFBlockedError,
                        MediaSizeLimitExceeded,
                        AdapterRateLimitError,
                    ):
                        raise
                    except MediaDownloadError as exc:
                        last_err = exc
                        if exc.status_code == 412:
                            raise
                        if exc.is_transient:
                            retry_urls.append(url)
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                        if deadline - time.monotonic() <= 0.0:
                            last_err = MediaDownloadError(
                                f"媒体下载超过总超时上限（{effective_total:.1f}s）",
                                is_transient=True,
                            )
                            retry_urls.clear()
                            break
                        last_err = MediaDownloadError(
                            redact_error_text(exc, secrets), is_transient=True
                        )
                        retry_urls.append(url)
                    except AdapterHTTPError as exc:
                        is_trans = exc.status_code >= 500 or exc.status_code in {
                            403,
                            408,
                            429,
                        }
                        last_err = MediaDownloadError(
                            f"HTTP {exc.status_code}",
                            is_transient=is_trans,
                            status_code=exc.status_code,
                        )
                        if exc.status_code == 412:
                            raise last_err from None
                        if is_trans:
                            retry_urls.append(url)
                    finally:
                        if part_path.exists():
                            try:
                                part_path.unlink()
                            except OSError:
                                pass
                remaining = deadline - time.monotonic()
                if not retry_urls or attempt == 1 or remaining <= 0.0:
                    break
                candidates = retry_urls
                await asyncio.sleep(min(1.0, remaining))
        finally:
            if part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass

    if isinstance(last_err, MediaDownloadError):
        raise last_err
    raise MediaDownloadError(
        redact_error_text(last_err or "媒体下载失败", secrets), is_transient=True
    )


def _extract_stream_urls(entry: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for k in ("baseUrl", "base_url", "url"):
        val = str(entry.get(k) or "").strip()
        if val:
            urls.append(val)
    for k in ("backupUrl", "backup_url"):
        raw = entry.get(k)
        if isinstance(raw, str) and raw.strip():
            urls.append(raw.strip())
        elif isinstance(raw, list):
            for item in raw:
                s = str(item or "").strip()
                if s:
                    urls.append(s)
    return list(dict.fromkeys(urls))


def select_dash_streams(
    download_url_data: Mapping[str, Any] | None,
    *,
    max_download_mb: float = 60.0,
    need_video: bool = True,
    need_audio: bool = True,
) -> dict[str, Any]:
    """Select compact DASH video/audio (or progressive `durl`) tracks within `max_download_mb`."""
    if not isinstance(download_url_data, Mapping):
        raise MediaUnavailableError("播放地址数据为空")

    root = (
        download_url_data.get("data")
        if isinstance(download_url_data.get("data"), Mapping)
        else download_url_data
    )
    timelength_ms = float(root.get("timelength") or 0.0)
    timelength_sec = timelength_ms / 1000.0 if timelength_ms > 0 else 0.0
    max_bytes = int(max_download_mb * 1024 * 1024)

    dash = root.get("dash") if isinstance(root.get("dash"), Mapping) else None
    if dash:
        dash_dur = float(dash.get("duration") or 0.0)
        eff_dur = timelength_sec or dash_dur

        raw_videos = [
            v
            for v in (dash.get("video") or [])
            if isinstance(v, Mapping) and _extract_stream_urls(v)
        ]
        raw_audios = [
            a
            for a in (dash.get("audio") or [])
            if isinstance(a, Mapping) and _extract_stream_urls(a)
        ]

        chosen_audio: Mapping[str, Any] | None = None
        est_audio_bytes = 0
        if need_audio and raw_audios:
            raw_audios.sort(
                key=lambda a: (
                    _AUDIO_QUALITY_RANK.get(int(a.get("id") or 0), 99),
                    int(a.get("bandwidth") or 0),
                )
            )
            chosen_audio = raw_audios[0]
            bw = int(chosen_audio.get("bandwidth") or 0)
            if bw > 0 and eff_dur > 0:
                est_audio_bytes = int(bw * eff_dur / 8.0)

        chosen_video: Mapping[str, Any] | None = None
        est_video_bytes = 0
        if need_video and raw_videos:
            def _video_sort_key(v: Mapping[str, Any]) -> tuple[int, int, int]:
                qid = int(v.get("id") or 0)
                cid = int(v.get("codecid") or 0)
                bw = int(v.get("bandwidth") or 0)
                return (
                    _VIDEO_QUALITY_RANK.get(qid, 50 + qid),
                    _VIDEO_CODEC_RANK.get(cid, 10),
                    bw,
                )

            ranked_videos = sorted(raw_videos, key=_video_sort_key)
            # First prefer highest-ranked candidate whose estimated total size fits max_download_mb
            for cand in ranked_videos:
                bw = int(cand.get("bandwidth") or 0)
                est_v = int(bw * eff_dur / 8.0) if (bw > 0 and eff_dur > 0) else 0
                if est_v == 0 or (est_v + est_audio_bytes) <= max_bytes:
                    chosen_video = cand
                    est_video_bytes = est_v
                    break
            # Fallback to smallest bandwidth candidate even if estimate exceeds cap
            if chosen_video is None:
                smallest = min(
                    raw_videos,
                    key=lambda v: (
                        int(v.get("bandwidth") or 0),
                        _VIDEO_CODEC_RANK.get(int(v.get("codecid") or 0), 10),
                    ),
                )
                chosen_video = smallest
                bw = int(smallest.get("bandwidth") or 0)
                est_video_bytes = int(bw * eff_dur / 8.0) if (bw > 0 and eff_dur > 0) else 0

        if (need_video and chosen_video is not None) or (
            not need_video and chosen_audio is not None
        ):
            return {
                "is_progressive": False,
                "video_urls": _extract_stream_urls(chosen_video) if chosen_video else [],
                "audio_urls": _extract_stream_urls(chosen_audio) if chosen_audio else [],
                "video_quality_id": int(chosen_video.get("id") or 0) if chosen_video else 0,
                "video_codecid": int(chosen_video.get("codecid") or 0) if chosen_video else 0,
                "estimated_video_bytes": est_video_bytes,
                "estimated_audio_bytes": est_audio_bytes,
                "estimated_total_bytes": est_video_bytes + est_audio_bytes,
                "duration_sec": eff_dur,
                "is_preview": bool(root.get("is_preview")),
            }

    durl = root.get("durl")
    if isinstance(durl, list) and durl and isinstance(durl[0], Mapping):
        urls = _extract_stream_urls(durl[0])
        if urls:
            est_size = int(durl[0].get("size") or 0)
            return {
                "is_progressive": True,
                "video_urls": urls,
                "audio_urls": [],
                "video_quality_id": int(root.get("quality") or 0),
                "video_codecid": 7,
                "estimated_video_bytes": est_size,
                "estimated_audio_bytes": 0,
                "estimated_total_bytes": est_size,
                "duration_sec": timelength_sec,
                "is_preview": bool(root.get("is_preview")),
            }

    raise MediaUnavailableError("未找到可用的视频或音频播放流")


async def _run_subprocess(
    args: Sequence[str], *, timeout: float
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        try:
            if getattr(proc, "returncode", None) is None:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            pass
        try:
            await proc.wait()
        except BaseException:
            pass
        raise
    return int(proc.returncode or 0), stdout, stderr


async def probe_media_duration(media_path: Path, *, timeout: float = 15.0) -> float:
    """Probe media duration in seconds via `ffprobe`; returns 0.0 on failure."""
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin or not media_path.exists():
        return 0.0
    try:
        rc, stdout, _ = await _run_subprocess(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            timeout=timeout,
        )
        if rc == 0:
            val = float(stdout.decode("utf-8", errors="ignore").strip() or "0")
            return max(0.0, val) if math.isfinite(val) else 0.0
    except Exception:
        return 0.0
    return 0.0


async def mux_and_compress_native_video(
    video_path: Path,
    audio_path: Path | None,
    output_path: Path,
    *,
    duration_sec: float,
    max_mb: float = 15.0,
    timeout: float = 120.0,
) -> int:
    """Mux video (+ optional audio) and compress into an inline-ready MP4 `<= max_mb`."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg 未安装")

    eff_dur = max(1.0, float(duration_sec or 0.0))
    if eff_dur <= 1.0:
        probed = await probe_media_duration(video_path)
        if probed > 0:
            eff_dur = probed

    max_bytes = int(max_mb * 1024 * 1024)
    target_bytes = int(max_bytes * 0.88)

    # First pass settings based on duration
    total_kbps = int((target_bytes * 8) / eff_dur / 1000)
    audio_kbps = 48 if total_kbps >= 320 else 32
    video_kbps = max(90, min(560, total_kbps - audio_kbps))
    if eff_dur <= 90:
        height, fps = 480, 4
    elif eff_dur <= 240:
        height, fps = 360, 2
    else:
        height, fps = 360, 1

    passes = [
        (height, fps, video_kbps, audio_kbps, 29),
        (320, 1, max(60, int(video_kbps * 0.62)), 24, 34),
    ]

    async with _media_pipeline_slot():
        for idx, (h, target_fps, v_kbps, a_kbps, crf) in enumerate(passes):
            cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path)]
            has_separate_audio = audio_path is not None and audio_path.exists()
            if has_separate_audio:
                cmd.extend(["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0?"])
            else:
                cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

            vf = f"fps={target_fps},scale=-2:'min({h},ih)'"
            cmd.extend(
                [
                    "-vf",
                    vf,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    str(crf),
                    "-maxrate",
                    f"{v_kbps}k",
                    "-bufsize",
                    f"{v_kbps * 2}k",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    "-b:a",
                    f"{a_kbps}k",
                    "-movflags",
                    "+faststart",
                    "-shortest",
                    str(output_path),
                ]
            )
            rc, _, stderr = await _run_subprocess(cmd, timeout=timeout)
            if rc != 0 or not output_path.exists():
                err_msg = stderr.decode("utf-8", errors="ignore").strip()[:240]
                if idx == len(passes) - 1:
                    output_path.unlink(missing_ok=True)
                    raise RuntimeError(f"ffmpeg 压缩封装失败: {err_msg or rc}")
                continue

            size = output_path.stat().st_size
            if 0 < size <= max_bytes:
                return size

    output_path.unlink(missing_ok=True)
    raise MediaSizeLimitExceeded(f"压缩后 MP4 仍超过上限 {max_mb:.1f}MB")


def plan_frame_timestamps(
    duration_sec: float,
    *,
    max_frames: int = 12,
    scene_timestamps: Sequence[float] = (),
    danmaku_spikes_sec: Sequence[float] = (),
) -> list[float]:
    """Combine danmaku spikes, scene-change timestamps, and uniform coverage into `<= max_frames` sorted points."""
    count = max(1, min(int(max_frames or 12), 20))
    dur = max(0.0, float(duration_sec or 0.0))
    if dur <= 2.0:
        return [round(max(0.0, dur * 0.5), 2)]

    lo = min(dur * 0.04, 3.0)
    hi = max(lo, dur - min(dur * 0.04, 3.0))
    min_gap = max(1.5, (hi - lo) / max(1.0, count * 1.7))

    chosen: list[float] = []

    def _can_add(ts: float) -> bool:
        return all(abs(ts - existing) >= min_gap for existing in chosen)

    def _try_add(ts: float) -> bool:
        clamped = round(max(lo, min(hi, float(ts))), 2)
        if _can_add(clamped):
            chosen.append(clamped)
            return True
        return False

    # 1) High-energy danmaku spikes (up to ~1/3 of budget)
    danmaku_budget = max(1, count // 3)
    added_dm = 0
    for ts in danmaku_spikes_sec:
        if added_dm >= danmaku_budget or len(chosen) >= count:
            break
        if _try_add(ts):
            added_dm += 1

    # 2) Scene changes (up to ~1/2 of budget)
    scene_budget = max(2, count // 2)
    added_scene = 0
    for ts in scene_timestamps:
        if added_scene >= scene_budget or len(chosen) >= count:
            break
        if _try_add(ts):
            added_scene += 1

    # 3) Uniform timeline anchors to guarantee start/middle/end arc coverage
    if count == 1:
        anchors = [(lo + hi) / 2.0]
    else:
        step = (hi - lo) / (count - 1)
        anchors = [lo + i * step for i in range(count)]

    for ts in anchors:
        if len(chosen) >= count:
            break
        _try_add(ts)

    # Relax spacing slightly if still below target count and video is long enough
    if len(chosen) < count and dur >= count * 2.0:
        relaxed_gap = min_gap * 0.45
        for ts in anchors:
            if len(chosen) >= count:
                break
            clamped = round(max(lo, min(hi, ts)), 2)
            if all(abs(clamped - existing) >= relaxed_gap for existing in chosen):
                chosen.append(clamped)

    chosen.sort()
    return chosen[:count]


async def detect_scene_timestamps(
    video_path: Path,
    *,
    duration_sec: float,
    max_candidates: int = 24,
    threshold: float = 0.32,
    timeout: float = 20.0,
) -> list[float]:
    """Detect scene-change timestamps via ffmpeg `select` + `showinfo` with strict timeout."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin or not video_path.exists() or duration_sec <= 4.0:
        return []
    try:
        rc, _, stderr = await _run_subprocess(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-i",
                str(video_path),
                "-vf",
                f"fps=1,select='gt(scene,{threshold:.2f})',showinfo",
                "-an",
                "-f",
                "null",
                "-",
            ],
            timeout=timeout,
        )
        if rc != 0:
            return []
        text = stderr.decode("utf-8", errors="ignore")
        pts = [float(m.group(1)) for m in _PTS_TIME_RE.finditer(text)]
        if len(pts) <= max_candidates:
            return pts
        step = len(pts) / max_candidates
        return [pts[int(i * step)] for i in range(max_candidates)]
    except Exception:
        return []


def stamp_timestamp_on_image(image_path: Path, label: str) -> None:
    """Stamp a compact `[mm:ss]` badge onto the top-left corner without obscuring bottom subtitles."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return
    try:
        with Image.open(image_path) as im:
            base = im.convert("RGBA")
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            text = f"[{label}]"
            font_size = max(14, min(28, int(base.width * 0.026)))
            font: Any = None
            for font_path in (
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "C:/Windows/Fonts/consolab.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
            ):
                if os.path.exists(font_path):
                    try:
                        font = ImageFont.truetype(font_path, font_size)
                        break
                    except Exception:
                        pass
            if font is None:
                font = ImageFont.load_default()

            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            pad_x, pad_y = 8, 5
            x0, y0 = 10, 10
            draw.rounded_rectangle(
                [x0, y0, x0 + tw + pad_x * 2, y0 + th + pad_y * 2],
                radius=6,
                fill=(0, 0, 0, 185),
            )
            draw.text(
                (x0 + pad_x, y0 + pad_y - bbox[1]),
                text,
                fill=(255, 255, 255, 255),
                font=font,
            )
            merged = Image.alpha_composite(base, overlay).convert("RGB")
            merged.save(image_path, format="JPEG", quality=84)
    except Exception:
        return


async def extract_keyframes_from_video(
    video_path: Path,
    output_dir: Path,
    *,
    duration_sec: float,
    max_frames: int = 12,
    max_width: int = 960,
    jpeg_quality: int = 80,
    danmaku_spikes_sec: Sequence[float] = (),
    use_scene_detect: bool = True,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """Extract timestamped, stamped JPEG keyframes from a local video file."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin or not video_path.exists():
        return []

    async with _media_pipeline_slot():
        eff_dur = float(duration_sec or 0.0)
        if eff_dur <= 0.0:
            eff_dur = await probe_media_duration(video_path)
        if eff_dur <= 0.0:
            eff_dur = 10.0

        scene_ts: list[float] = []
        if use_scene_detect and eff_dur <= 1800:
            scene_ts = await detect_scene_timestamps(
                video_path,
                duration_sec=eff_dur,
                timeout=min(20.0, max(8.0, timeout * 0.35)),
            )

        timestamps = plan_frame_timestamps(
            eff_dur,
            max_frames=max_frames,
            scene_timestamps=scene_ts,
            danmaku_spikes_sec=danmaku_spikes_sec,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        # Map JPEG quality 50..95 -> ffmpeg mjpeg q:v 10..2
        qscale = max(2, min(12, int(round((100 - max(50, min(95, jpeg_quality))) / 5.0))))
        per_frame_timeout = max(8.0, min(20.0, timeout / max(1, len(timestamps)) * 2.0))
        sem = asyncio.Semaphore(3)

        async def _grab_one(idx: int, ts: float) -> dict[str, Any] | None:
            label = format_timestamp(ts)
            out_file = output_dir / f"frame_{idx:02d}_{int(ts):05d}s.jpg"
            async with sem:
                try:
                    rc, _, _ = await _run_subprocess(
                        [
                            ffmpeg_bin,
                            "-y",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-ss",
                            f"{ts:.2f}",
                            "-i",
                            str(video_path),
                            "-frames:v",
                            "1",
                            "-vf",
                            f"scale='min({int(max_width)},iw)':-2",
                            "-q:v",
                            str(qscale),
                            str(out_file),
                        ],
                        timeout=per_frame_timeout,
                    )
                except Exception:
                    return None
                if rc != 0 or not out_file.exists() or out_file.stat().st_size == 0:
                    out_file.unlink(missing_ok=True)
                    return None
                await asyncio.to_thread(stamp_timestamp_on_image, out_file, label)
                return {
                    "path": str(out_file),
                    "timestamp_sec": round(ts, 2),
                    "label": label,
                    "source": "ffmpeg",
                }

        results = await asyncio.gather(
            *(_grab_one(i + 1, ts) for i, ts in enumerate(timestamps))
        )
        frames = [r for r in results if r is not None]
        frames.sort(key=lambda item: float(item["timestamp_sec"]))
        return frames


def _pick_videoshot_tile_indices(
    index_timestamps: Sequence[float],
    *,
    max_tiles: int,
    danmaku_spikes_sec: Sequence[float] = (),
) -> list[int]:
    total = len(index_timestamps)
    if total == 0:
        return []
    target = max(1, min(max_tiles, total))
    if total <= target:
        return list(range(total))

    chosen: set[int] = set()
    # Map spikes to nearest videoshot tile index
    spike_budget = max(1, target // 3)
    for sp in danmaku_spikes_sec:
        if len(chosen) >= spike_budget:
            break
        best_idx = min(range(total), key=lambda i: abs(index_timestamps[i] - sp))
        chosen.add(best_idx)

    # Fill remaining slots evenly across the timeline
    step = (total - 1) / max(1, target - 1)
    for k in range(target):
        if len(chosen) >= target:
            break
        idx = int(round(k * step))
        chosen.add(max(0, min(total - 1, idx)))

    return sorted(chosen)[:target]


def _compose_videoshot_images(
    sheets: dict[int, bytes],
    selected_indices: Sequence[int],
    index_timestamps: Sequence[float],
    *,
    x_len: int,
    y_len: int,
    x_size: int,
    y_size: int,
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Crop selected tiles from Bilibili sprite sheets into crisp 2x2 timestamped contact sheets."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return []

    per_sheet = max(1, x_len * y_len)
    opened_sheets: dict[int, Any] = {}
    cropped_tiles: list[tuple[float, str, Any]] = []

    font: Any = None
    for font_path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/consolab.ttf",
    ):
        if os.path.exists(font_path):
            try:
                font = ImageFont.truetype(font_path, 16)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    try:
        for tile_idx in selected_indices:
            sheet_idx, local_idx = divmod(tile_idx, per_sheet)
            raw_sheet = sheets.get(sheet_idx)
            if not raw_sheet:
                continue
            if sheet_idx not in opened_sheets:
                opened_sheets[sheet_idx] = Image.open(io.BytesIO(raw_sheet)).convert("RGB")
            sheet_img = opened_sheets[sheet_idx]
            row, col = divmod(local_idx, x_len)
            left = col * x_size
            top = row * y_size
            right = left + x_size
            bottom = top + y_size
            if right > sheet_img.width or bottom > sheet_img.height:
                continue
            tile = sheet_img.crop((left, top, right, bottom))
            # Upscale 2x so 160x90 tiles become crisp 320x180 panels
            scale = 2 if x_size <= 200 else 1
            if scale > 1:
                tile = tile.resize(
                    (x_size * scale, y_size * scale), Image.Resampling.LANCZOS
                )
            ts = float(index_timestamps[tile_idx]) if tile_idx < len(index_timestamps) else 0.0
            label = format_timestamp(ts)
            draw = ImageDraw.Draw(tile)
            text = f"[{label}]"
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.rectangle([4, 4, 4 + tw + 10, 4 + th + 6], fill=(0, 0, 0))
            draw.text((9, 7 - bbox[1]), text, fill=(255, 255, 255), font=font)
            cropped_tiles.append((ts, label, tile))

        if not cropped_tiles:
            return []

        output_dir.mkdir(parents=True, exist_ok=True)
        out_frames: list[dict[str, Any]] = []
        # Pack into 2x2 grids (4 tiles -> 640x360) for compact VLM viewing
        tile_w, tile_h = cropped_tiles[0][2].size
        gap = 4
        for grid_idx, start in enumerate(range(0, len(cropped_tiles), 4), start=1):
            batch = cropped_tiles[start : start + 4]
            cols = 2 if len(batch) > 1 else 1
            rows = 2 if len(batch) > 2 else 1
            canvas = Image.new(
                "RGB",
                (cols * tile_w + (cols - 1) * gap, rows * tile_h + (rows - 1) * gap),
                (18, 18, 18),
            )
            for pos, (_, _, t_img) in enumerate(batch):
                r, c = divmod(pos, cols)
                canvas.paste(t_img, (c * (tile_w + gap), r * (tile_h + gap)))
            first_ts, first_label = batch[0][0], batch[0][1]
            last_label = batch[-1][1]
            grid_label = (
                f"{first_label}-{last_label}" if len(batch) > 1 else first_label
            )
            out_path = output_dir / f"videoshot_{grid_idx:02d}.jpg"
            canvas.save(out_path, format="JPEG", quality=84)
            out_frames.append(
                {
                    "path": str(out_path),
                    "timestamp_sec": round(first_ts, 2),
                    "label": grid_label,
                    "source": "videoshot",
                }
            )
        return out_frames
    finally:
        for img in opened_sheets.values():
            try:
                img.close()
            except Exception:
                pass


async def fetch_videoshot_frames(
    bvid: str,
    cid: int,
    output_dir: Path,
    *,
    duration_sec: float,
    max_frames: int = 12,
    danmaku_spikes_sec: Sequence[float] = (),
    cookie_header: str = "",
    secrets: Sequence[str] = (),
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Fetch Bilibili official `x/player/videoshot` preview sprites and crop into timestamped grids."""
    headers = dict(BILI_MEDIA_HEADERS)
    if cookie_header:
        headers["Cookie"] = cookie_header

    resp = await safe_http_get(
        BILI_VIDEOSHOT_URL,
        params={"bvid": bvid, "cid": str(int(cid)), "index": "1"},
        headers=headers,
        timeout=timeout,
        max_bytes=1_048_576,
        allow_private=False,
        secrets_to_redact=secrets,
    )
    import json

    payload = json.loads(resp.text())
    if not isinstance(payload, Mapping):
        return []
    code = int(payload.get("code", -1) if payload.get("code") is not None else -1)
    if code in {-412, -352, -509, -799}:
        raise MediaDownloadError(
            f"risk_control:code={code}", is_transient=True, status_code=None
        )
    if code != 0:
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []

    raw_images = data.get("image") or []
    if not isinstance(raw_images, list) or not raw_images:
        return []

    x_len = max(1, int(data.get("img_x_len") or 10))
    y_len = max(1, int(data.get("img_y_len") or 10))
    x_size = max(32, int(data.get("img_x_size") or 160))
    y_size = max(18, int(data.get("img_y_size") or 90))
    raw_index = data.get("index") or []

    per_sheet = x_len * y_len
    total_capacity = len(raw_images) * per_sheet
    index_ts: list[float] = []
    if isinstance(raw_index, list) and raw_index:
        for item in raw_index[:total_capacity]:
            try:
                index_ts.append(float(item))
            except (TypeError, ValueError):
                index_ts.append(0.0)
    else:
        approx_count = min(total_capacity, max(4, min(16, int(max_frames or 12))))
        eff_dur = max(1.0, float(duration_sec or 60.0))
        index_ts = [eff_dur * i / max(1, approx_count) for i in range(approx_count)]

    if not index_ts:
        return []

    # Up to 16 tiles packed into up to 4 crisp 2x2 grids (bounded by max_frames * 4)
    tile_budget = max(4, min(16, int(max_frames or 12) * 2))
    selected = _pick_videoshot_tile_indices(
        index_ts, max_tiles=tile_budget, danmaku_spikes_sec=danmaku_spikes_sec
    )
    needed_sheets = sorted({idx // per_sheet for idx in selected})[:4]
    sheet_bytes: dict[int, bytes] = {}

    for sheet_idx in needed_sheets:
        if sheet_idx >= len(raw_images):
            continue
        raw_url = str(raw_images[sheet_idx] or "").strip()
        if not raw_url:
            continue
        url = ("https:" + raw_url) if raw_url.startswith("//") else raw_url
        img_resp = await safe_http_get(
            url,
            headers=BILI_MEDIA_HEADERS,
            timeout=timeout,
            max_bytes=5_242_880,
            allow_private=False,
            secrets_to_redact=secrets,
        )
        if img_resp.body:
            sheet_bytes[sheet_idx] = img_resp.body

    if not sheet_bytes:
        return []

    return await asyncio.to_thread(
        _compose_videoshot_images,
        sheet_bytes,
        selected,
        index_ts,
        x_len=x_len,
        y_len=y_len,
        x_size=x_size,
        y_size=y_size,
        output_dir=output_dir,
    )


def sample_danmakus_from_list(
    raw_danmakus: Sequence[Any],
    *,
    duration_sec: float = 0.0,
    max_items: int = 15,
) -> dict[str, Any]:
    """Bucket danmakus to locate high-energy spikes and pick a capped, sanitized sample."""
    cap = max(1, min(int(max_items or 15), 30))
    parsed: list[tuple[float, str]] = []
    for dm in raw_danmakus or ():
        if isinstance(dm, Mapping):
            raw_text = dm.get("text") or dm.get("content") or ""
            raw_time = dm.get("dm_time", dm.get("time", 0.0))
        else:
            raw_text = getattr(dm, "text", "")
            raw_time = getattr(dm, "dm_time", 0.0)
        try:
            t_sec = max(0.0, float(raw_time or 0.0))
        except (TypeError, ValueError):
            t_sec = 0.0
        raw_str = str(raw_text or "")
        # Drop any danmaku line containing a prompt-injection pattern in its entirety
        if any(pat.search(raw_str) for pat in ssrf_mod._INJECTION_PATTERNS):
            continue
        cleaned = clean_inline_field(raw_str, max_chars=60)
        if not cleaned or "[已过滤]" in cleaned or len(cleaned) < 2:
            continue
        if _REPEATED_CHAR_RE.match(cleaned) or _LOW_SIGNAL_DANMAKU_RE.match(cleaned):
            continue
        parsed.append((t_sec, cleaned))

    if not parsed:
        return {"sample": [], "spikes_sec": []}

    eff_dur = max(
        float(duration_sec or 0.0),
        max(t for t, _ in parsed),
        10.0,
    )
    bucket_sec = max(5.0, min(30.0, eff_dur / 18.0))
    bucket_counts: Counter[int] = Counter()
    bucket_items: dict[int, list[tuple[float, str]]] = {}
    for t_sec, text in parsed:
        b = int(t_sec // bucket_sec)
        bucket_counts[b] += 1
        bucket_items.setdefault(b, []).append((t_sec, text))

    # Top spike buckets separated by at least 2 buckets
    spikes_sec: list[float] = []
    ranked_buckets = [b for b, _ in bucket_counts.most_common()]
    for b in ranked_buckets:
        center = round(min(eff_dur, (b + 0.5) * bucket_sec), 1)
        if all(abs(center - s) >= bucket_sec * 1.5 for s in spikes_sec):
            spikes_sec.append(center)
        if len(spikes_sec) >= 6:
            break

    # Pick unique short lines prioritizing high-energy buckets round-robin
    seen_norm: set[str] = set()
    selected: list[tuple[float, str]] = []
    per_bucket_cap = max(2, math.ceil(cap / max(1, min(len(ranked_buckets), 6))))

    for b in ranked_buckets:
        if len(selected) >= cap:
            break
        added_here = 0
        for t_sec, text in bucket_items.get(b, ()):
            norm = re.sub(r"\s+", "", text).lower()
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            selected.append((t_sec, text))
            added_here += 1
            if added_here >= per_bucket_cap or len(selected) >= cap:
                break

    selected.sort(key=lambda item: item[0])
    spikes_sec.sort()
    return {
        "sample": [f"[{format_timestamp(t)}] {txt}" for t, txt in selected],
        "spikes_sec": spikes_sec,
    }


async def fetch_danmaku_sample(
    video_obj: Any,
    *,
    cid: int,
    duration_sec: float = 0.0,
    max_items: int = 15,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch and sample high-energy danmakus via `video_obj.get_danmakus` (never comments)."""
    getter = getattr(video_obj, "get_danmakus", None)
    if not callable(getter):
        return {"sample": [], "spikes_sec": []}
    raw = await asyncio.wait_for(getter(cid=int(cid)), timeout=max(2.0, float(timeout)))
    return sample_danmakus_from_list(
        raw if isinstance(raw, Sequence) else (),
        duration_sec=duration_sec,
        max_items=max_items,
    )


async def acquire_video_media(
    *,
    video_obj: Any,
    bvid: str,
    cid: int,
    duration_sec: float,
    has_subtitle: bool,
    subtitle_source: str,
    media_options: Mapping[str, Any] | None,
    cookie_header: str = "",
    secrets: Sequence[str] = (),
    audio_transcriber: Callable[[str, int], Awaitable[str]] | None = None,
    timeout: float = 45.0,
    classify_error: Callable[[BaseException], Any] | None = None,
) -> dict[str, Any]:
    """Acquire visual/audio/danmaku media for a Bilibili video using duration-tiered budgeting.

    Always places temporary files under a dedicated `horizons_bili_*` temp dir and attaches
    an idempotent `cleanup()` callable plus `ensure_fallback_frames()` on the returned bundle.
    """
    opts = normalize_media_options(media_options, default_visual_enabled=True)
    temp_dir = make_temp_dir(bvid)
    try:
        return await _acquire_video_media_in_temp(
            temp_dir=temp_dir,
            opts=opts,
            video_obj=video_obj,
            bvid=bvid,
            cid=cid,
            duration_sec=duration_sec,
            has_subtitle=has_subtitle,
            subtitle_source=subtitle_source,
            cookie_header=cookie_header,
            secrets=secrets,
            audio_transcriber=audio_transcriber,
            timeout=timeout,
            classify_error=classify_error,
        )
    except BaseException:
        cleanup_temp_dir(temp_dir)
        raise


async def _acquire_video_media_in_temp(
    *,
    temp_dir: Path,
    opts: Mapping[str, Any],
    video_obj: Any,
    bvid: str,
    cid: int,
    duration_sec: float,
    has_subtitle: bool,
    subtitle_source: str,
    cookie_header: str,
    secrets: Sequence[str],
    audio_transcriber: Callable[[str, int], Awaitable[str]] | None,
    timeout: float,
    classify_error: Callable[[BaseException], Any] | None,
) -> dict[str, Any]:
    bundle = build_empty_media_bundle(
        watch_mode="subtitle_only" if has_subtitle else "metadata_only",
        content_origin="bilibili_subtitle" if has_subtitle else "bilibili_metadata_only",
        duration_sec=int(round(duration_sec or 0.0)),
        has_subtitle=has_subtitle,
        subtitle_source=subtitle_source,
        temp_dir=str(temp_dir),
    )

    danmaku_spikes: list[float] = []
    if opts["danmaku_enabled"] and cid:
        try:
            dm_res = await fetch_danmaku_sample(
                video_obj,
                cid=cid,
                duration_sec=duration_sec,
                max_items=opts["danmaku_max_items"],
                timeout=min(15.0, timeout),
            )
            if dm_res["sample"]:
                bundle["danmaku_sample"] = dm_res["sample"]
                bundle["has_danmaku"] = True
            danmaku_spikes = list(dm_res["spikes_sec"] or [])
            bundle["danmaku_spikes_sec"] = danmaku_spikes
        except Exception as exc:
            # Never silently swallow risk control; propagate to caller if classified
            if classify_error is not None:
                rc = classify_error(exc)
                if rc is not None:
                    raise rc from None

    def _compute_tier_flags(dur: float) -> tuple[bool, bool, bool]:
        w_native = (
            opts["visual_enabled"]
            and opts["prefer_native_video"]
            and 0 < dur <= opts["native_max_duration_sec"]
        )
        a_dash = opts["visual_enabled"] and (
            dur <= 0 or dur <= opts["medium_max_duration_sec"]
        )
        n_stt = (
            callable(audio_transcriber)
            and opts["audio_fallback_enabled"]
            and not has_subtitle
            and not w_native
            and (dur <= 0 or dur <= opts["audio_max_duration_sec"])
        )
        return w_native, a_dash, n_stt

    eff_dur = float(duration_sec or 0.0)
    want_native, allow_dash_video, need_audio_stt = _compute_tier_flags(eff_dur)

    download_url_data: Mapping[str, Any] | None = None
    stream_info: dict[str, Any] | None = None
    raw_video_path: Path | None = None
    raw_audio_path: Path | None = None
    transient_reason = ""
    permanent_reason = ""

    # Step 1: Fetch DASH/progressive stream metadata when within duration cap or audio STT needed
    if (allow_dash_video or need_audio_stt) and cid:
        get_dl = getattr(video_obj, "get_download_url", None)
        if callable(get_dl):
            try:
                download_url_data = await asyncio.wait_for(
                    get_dl(cid=int(cid)), timeout=min(25.0, timeout)
                )
                stream_info = select_dash_streams(
                    download_url_data,
                    max_download_mb=opts["max_download_mb"],
                    need_video=allow_dash_video,
                    need_audio=want_native
                    or (opts["visual_enabled"] and opts["prefer_native_video"] and eff_dur <= 0)
                    or need_audio_stt,
                )
                if eff_dur <= 0 and stream_info.get("duration_sec"):
                    eff_dur = float(stream_info["duration_sec"])
                    bundle["duration_sec"] = int(round(eff_dur))
                    want_native, allow_dash_video, need_audio_stt = _compute_tier_flags(
                        eff_dur
                    )
            except MediaUnavailableError as exc:
                permanent_reason = exc.reason
            except Exception as exc:
                if classify_error is not None:
                    rc = classify_error(exc)
                    if rc is not None:
                        raise rc from None
                # Distinguish permanent Bilibili codes vs transient errors
                code = int(getattr(exc, "code", 0) or 0)
                if code in {-404, -403, -10403, 62002, 62004, 62012, 87007, 87008}:
                    permanent_reason = f"bili_code:{code}"
                else:
                    transient_reason = redact_error_text(exc, secrets)

    # Step 2 + 3A + 3B: Serialize heavy download and transcode under global Semaphore(1)
    max_dl_bytes = int(opts["max_download_mb"] * 1024 * 1024)
    async with _media_pipeline_slot():
        if (
            opts["visual_enabled"]
            and allow_dash_video
            and stream_info is not None
            and stream_info.get("video_urls")
            and (
                stream_info["estimated_video_bytes"] == 0
                or stream_info["estimated_video_bytes"] <= max_dl_bytes
            )
        ):
            cand_video = temp_dir / "raw_video.mp4"
            try:
                await download_media_to_file(
                    stream_info["video_urls"],
                    cand_video,
                    max_bytes=max_dl_bytes,
                    timeout=max(40.0, timeout),
                    cookie_header=cookie_header,
                    secrets=secrets,
                )
                raw_video_path = cand_video
            except (SSRFBlockedError, AdapterRateLimitError):
                cleanup_temp_dir(temp_dir)
                raise
            except MediaSizeLimitExceeded as exc:
                transient_reason = redact_error_text(exc, secrets)
            except Exception as exc:
                if classify_error is not None:
                    rc = classify_error(exc)
                    if rc is not None:
                        cleanup_temp_dir(temp_dir)
                        raise rc from None
                transient_reason = redact_error_text(exc, secrets)

        # Download audio track if needed for native mux or STT fallback
        want_audio_dl = (
            stream_info is not None
            and not stream_info.get("is_progressive")
            and stream_info.get("audio_urls")
            and ((want_native and raw_video_path is not None) or need_audio_stt)
        )
        if want_audio_dl and stream_info is not None:
            audio_cap_bytes = int(
                min(opts["max_download_mb"], opts["audio_max_mb"]) * 1024 * 1024
            )
            if (
                stream_info["estimated_audio_bytes"] == 0
                or stream_info["estimated_audio_bytes"] <= audio_cap_bytes
            ):
                cand_audio = temp_dir / "raw_audio.m4a"
                try:
                    await download_media_to_file(
                        stream_info["audio_urls"],
                        cand_audio,
                        max_bytes=audio_cap_bytes,
                        timeout=max(30.0, timeout),
                        cookie_header=cookie_header,
                        secrets=secrets,
                    )
                    raw_audio_path = cand_audio
                except (SSRFBlockedError, AdapterRateLimitError):
                    cleanup_temp_dir(temp_dir)
                    raise
                except Exception as exc:
                    if classify_error is not None:
                        rc = classify_error(exc)
                        if rc is not None:
                            cleanup_temp_dir(temp_dir)
                            raise rc from None
                    if not transient_reason:
                        transient_reason = redact_error_text(exc, secrets)

        # Step 3A: Channel A — Native compressed MP4 for short videos
        if want_native and raw_video_path is not None:
            native_out = temp_dir / "native_video.mp4"
            try:
                size_bytes = await mux_and_compress_native_video(
                    raw_video_path,
                    raw_audio_path,
                    native_out,
                    duration_sec=eff_dur,
                    max_mb=opts["native_max_mb"],
                    timeout=max(60.0, timeout * 2.0),
                )
                bundle["watch_mode"] = "native_video"
                bundle["content_origin"] = "bilibili_native_video"
                bundle["has_visual"] = True
                bundle["native_video_path"] = str(native_out)
                bundle["native_video_size_bytes"] = size_bytes

                # Remove raw audio immediately; keep native_out for Gemini AND lazy fallback frames
                if raw_audio_path is not None:
                    raw_audio_path.unlink(missing_ok=True)
                # Keep raw_video_path? No, native_out already has all video frames and is <= 15MB!
                raw_video_path.unlink(missing_ok=True)
                raw_video_path = None

                async def _ensure_fallback_frames() -> list[dict[str, Any]]:
                    if bundle["frames"]:
                        return list(bundle["frames"])
                    src_mp4 = Path(bundle["native_video_path"])
                    if not src_mp4.exists():
                        return []
                    fb_max_frames, fb_max_width = resolve_keyframe_budget(
                        opts, has_subtitle=has_subtitle, duration_sec=eff_dur
                    )
                    extracted = await extract_keyframes_from_video(
                        src_mp4,
                        temp_dir / "frames",
                        duration_sec=eff_dur,
                        max_frames=fb_max_frames,
                        max_width=fb_max_width,
                        jpeg_quality=opts["frame_jpeg_quality"],
                        danmaku_spikes_sec=danmaku_spikes,
                        use_scene_detect=True,
                    )
                    bundle["frames"] = extracted
                    bundle["frame_paths"] = [f["path"] for f in extracted]
                    bundle["frame_count"] = len(extracted)
                    return list(extracted)

                bundle["ensure_fallback_frames"] = _ensure_fallback_frames
            except Exception as exc:
                # Compression/mux failed -> fall straight into Channel B keyframe extraction using raw_video_path!
                transient_reason = redact_error_text(exc, secrets)

        # Step 3B: Channel B — Extract keyframes from downloaded video
        if (
            opts["visual_enabled"]
            and not bundle["has_visual"]
            and raw_video_path is not None
        ):
            kf_max_frames, kf_max_width = resolve_keyframe_budget(
                opts, has_subtitle=has_subtitle, duration_sec=eff_dur
            )
            frames = await extract_keyframes_from_video(
                raw_video_path,
                temp_dir / "frames",
                duration_sec=eff_dur,
                max_frames=kf_max_frames,
                max_width=kf_max_width,
                jpeg_quality=opts["frame_jpeg_quality"],
                danmaku_spikes_sec=danmaku_spikes,
                use_scene_detect=True,
                timeout=max(45.0, timeout),
            )
            # Release raw downloaded video promptly unless needed for progressive audio STT
            if not (
                need_audio_stt
                and raw_audio_path is None
                and stream_info
                and stream_info.get("is_progressive")
            ):
                raw_video_path.unlink(missing_ok=True)
                raw_video_path = None

            if frames:
                bundle["frames"] = frames
                bundle["frame_paths"] = [f["path"] for f in frames]
                bundle["frame_count"] = len(frames)
                bundle["has_visual"] = True
                bundle["watch_mode"] = (
                    "keyframes_subtitle" if has_subtitle else "keyframes_only"
                )
                bundle["content_origin"] = (
                    "bilibili_vision_subtitle"
                    if has_subtitle
                    else "bilibili_vision_frames"
                )

    # Step 3C: Videoshot sprite fallback (for super-long videos or when DASH failed/oversized)
    if (
        opts["visual_enabled"]
        and not bundle["has_visual"]
        and not permanent_reason
        and cid
    ):
        try:
            vs_max_frames, _ = resolve_keyframe_budget(
                opts, has_subtitle=has_subtitle, duration_sec=eff_dur
            )
            shot_frames = await fetch_videoshot_frames(
                bvid,
                cid,
                temp_dir / "videoshot",
                duration_sec=eff_dur,
                max_frames=vs_max_frames,
                danmaku_spikes_sec=danmaku_spikes,
                cookie_header=cookie_header,
                secrets=secrets,
                timeout=min(20.0, timeout),
            )
            if shot_frames:
                bundle["frames"] = shot_frames
                bundle["frame_paths"] = [f["path"] for f in shot_frames]
                bundle["videoshot_paths"] = [f["path"] for f in shot_frames]
                bundle["frame_count"] = len(shot_frames)
                bundle["has_visual"] = True
                bundle["watch_mode"] = (
                    "videoshot_subtitle" if has_subtitle else "videoshot_only"
                )
                bundle["content_origin"] = (
                    "bilibili_videoshot_subtitle"
                    if has_subtitle
                    else "bilibili_videoshot"
                )
        except (SSRFBlockedError, AdapterRateLimitError):
            cleanup_temp_dir(temp_dir)
            raise
        except Exception as exc:
            if classify_error is not None:
                rc = classify_error(exc)
                if rc is not None:
                    cleanup_temp_dir(temp_dir)
                    raise rc from None
            if not transient_reason:
                transient_reason = redact_error_text(exc, secrets)

    # Step 4: Optional Audio STT fallback when no official subtitle and not native_video
    stt_audio_source = raw_audio_path or (
        raw_video_path
        if (raw_video_path is not None and stream_info and stream_info.get("is_progressive"))
        else None
    )
    if (
        callable(audio_transcriber)
        and opts["audio_fallback_enabled"]
        and not has_subtitle
        and bundle["watch_mode"] != "native_video"
        and stt_audio_source is not None
        and stt_audio_source.exists()
    ):
        try:
            raw_transcript = await audio_transcriber(
                str(stt_audio_source), int(round(eff_dur))
            )
            cleaned_transcript = clean_external_text(
                str(raw_transcript or ""), max_chars=4500
            )
            if cleaned_transcript:
                bundle["has_audio_transcript"] = True
                bundle["subtitle_source"] = "audio_stt"
                bundle["_audio_transcript"] = cleaned_transcript
                if not bundle["has_visual"]:
                    bundle["watch_mode"] = "audio_transcript"
                    bundle["content_origin"] = "bilibili_audio_transcript"
                elif bundle["watch_mode"] == "keyframes_only":
                    bundle["watch_mode"] = "keyframes_subtitle"
                    bundle["content_origin"] = "bilibili_vision_subtitle"
                elif bundle["watch_mode"] == "videoshot_only":
                    bundle["watch_mode"] = "videoshot_subtitle"
                    bundle["content_origin"] = "bilibili_videoshot_subtitle"
        except Exception as exc:
            if not transient_reason:
                transient_reason = redact_error_text(exc, secrets)

    # Clean up remaining raw downloaded intermediate tracks
    if raw_video_path is not None:
        raw_video_path.unlink(missing_ok=True)
    if raw_audio_path is not None:
        raw_audio_path.unlink(missing_ok=True)

    # Final status signals for "宁缺毋滥"
    if not bundle["has_visual"] and not has_subtitle and not bundle["has_audio_transcript"]:
        if permanent_reason:
            bundle["watch_mode"] = "unavailable"
            bundle["content_origin"] = "bilibili_unavailable_permanent"
            bundle["is_permanent_unavailable"] = True
            bundle["skip_reason"] = permanent_reason
        elif transient_reason:
            bundle["watch_mode"] = "metadata_only"
            bundle["content_origin"] = "bilibili_metadata_only"
            bundle["is_transient_error"] = True
            bundle["skip_reason"] = "transient_media_error"
            bundle["error_detail"] = transient_reason
        else:
            bundle["watch_mode"] = "metadata_only"
            bundle["content_origin"] = "bilibili_metadata_only"
            bundle["skip_reason"] = "no_subtitle_or_visual"

    # If no media artifacts were retained (e.g. subtitle-only or failed visual), clean temp dir eagerly
    if not bundle["native_video_path"] and not bundle["frames"]:
        cleanup_temp_dir(temp_dir)
        bundle["temp_dir"] = ""

    return bundle
