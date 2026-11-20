"""Read-only Bilibili source adapter (`up` / `popular` / `recommend` + subtitle/visual/danmaku)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from http.cookies import SimpleCookie
from typing import Any

from bilibili_api import (
    Credential,
    homepage,
    hot,
    rank,
    user,
    video,
)
from bilibili_api import (
    exceptions as bili_exc,
)

from .ssrf import (
    AdapterHTTPError,
    AdapterRateLimitError,
    SSRFBlockedError,
    clean_external_text,
    clean_inline_field,
    is_masked_secret,
    normalize_candidate,
    redact_error_text,
    safe_http_get,
    validate_url_sync,
)
from .video_media import (
    acquire_video_media,
    build_empty_media_bundle,
    format_timestamp,
    normalize_media_options,
)

BILI_MODES = ("up", "popular", "recommend")
POPULAR_KINDS = ("hot", "weekly", "rank")
RISK_CONTROL_CODES = frozenset({-412, -352, -509, -799})
PERMANENT_UNAVAILABLE_CODES = frozenset({-404, -403, -10403, 62002, 62004, 62012, 87007, 87008})
BILI_RCMD_URL = "https://api.bilibili.com/x/web-interface/index/top/feed/rcmd"

_BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}
_BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")


class BilibiliRiskControlError(RuntimeError):
    """Raised internally when Bilibili triggers -412/-352/Geetest risk control; never bypassed."""

    def __init__(
        self, reason: str, *, code: int = -412, cooldown_seconds: float = 1800.0
    ):
        super().__init__(reason)
        self.code = int(code)
        self.cooldown_seconds = float(cooldown_seconds)


class _BilibiliStageError(RuntimeError):
    """Keep completed UP results when a later request fails, without retrying it."""

    def __init__(
        self, stage: str, cause: Exception, *,
        items: list[dict[str, Any]] | None = None, completed: bool = False,
    ) -> None:
        super().__init__(stage)
        self.stage = stage
        self.cause = cause
        self.items = list(items or [])
        self.completed = completed


def _extract_cookie_parts(
    config: Mapping[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """Extract read-only cookie tokens (`SESSDATA`, `buvid3`, `DedeUserID`). Never extracts `bili_jct`."""
    raw_cookie = str(config.get("cookie") or "").strip()
    raw_sessdata = str(config.get("sessdata") or "").strip()
    secrets: list[str] = []
    parsed: dict[str, str] = {}

    if raw_cookie and not is_masked_secret(raw_cookie):
        secrets.append(raw_cookie)
        if "=" in raw_cookie:
            jar = SimpleCookie()
            try:
                jar.load(raw_cookie)
                for k, morsel in jar.items():
                    val = morsel.value.strip()
                    if val:
                        parsed[k] = val
                        secrets.append(val)
            except Exception:
                for seg in raw_cookie.split(";"):
                    if "=" in seg:
                        k, v = seg.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k and v:
                            parsed[k] = v
                            secrets.append(v)
        else:
            parsed["SESSDATA"] = raw_cookie

    if raw_sessdata and not is_masked_secret(raw_sessdata):
        parsed["SESSDATA"] = raw_sessdata
        secrets.append(raw_sessdata)

    # Strictly strip CSRF write token (`bili_jct`) so write APIs can never be invoked
    parsed.pop("bili_jct", None)
    return parsed, secrets


def _build_readonly_credential(
    config: Mapping[str, Any],
    *,
    shared_cookie: str = "",
    fallback_cookies: Sequence[str] | None = None,
) -> tuple[Credential | None, str, list[str]]:
    """Build strictly read-only Bilibili credentials (zero `bili_jct`) with optional cookie fallbacks."""
    parts, secrets = _extract_cookie_parts(config)
    candidates: list[dict[str, str]] = [parts]

    raw_fallbacks: list[str] = []
    if isinstance(shared_cookie, str) and shared_cookie.strip():
        raw_fallbacks.append(shared_cookie.strip())
    if fallback_cookies:
        for item in fallback_cookies:
            if isinstance(item, str) and item.strip():
                raw_fallbacks.append(item.strip())

    for fb_raw in raw_fallbacks:
        fb_parts, fb_secrets = _extract_cookie_parts({"cookie": fb_raw})
        secrets.extend(fb_secrets)
        if fb_parts:
            candidates.append(fb_parts)

    # Prefer the first candidate carrying SESSDATA; otherwise keep first with buvid3
    chosen: dict[str, str] = {}
    for cand in candidates:
        if cand.get("SESSDATA") or cand.get("sessdata"):
            chosen = cand
            break
    if not chosen:
        for cand in candidates:
            if cand.get("buvid3"):
                chosen = cand
                break

    # Defensive: ensure bili_jct is never kept
    chosen.pop("bili_jct", None)
    sessdata = chosen.get("SESSDATA") or chosen.get("sessdata") or ""
    buvid3 = chosen.get("buvid3") or ""
    dedeuserid = chosen.get("DedeUserID") or chosen.get("dedeuserid") or ""

    if not (sessdata or buvid3):
        return None, "", secrets

    cred = Credential(
        sessdata=sessdata or None,
        bili_jct=None,  # Enforce read-only at SDK level
        buvid3=buvid3 or None,
        dedeuserid=dedeuserid or None,
    )
    readonly_pairs = []
    if sessdata:
        readonly_pairs.append(f"SESSDATA={sessdata}")
    if buvid3:
        readonly_pairs.append(f"buvid3={buvid3}")
    if dedeuserid:
        readonly_pairs.append(f"DedeUserID={dedeuserid}")
    return cred, "; ".join(readonly_pairs), secrets


def _classify_permanent_unavailable(exc: BaseException) -> str:
    """Return a non-empty permanent skip reason when Bilibili indicates deleted/private/VIP/paid."""
    code = 0
    if isinstance(exc, bili_exc.ResponseCodeException):
        code = int(getattr(exc, "code", 0) or 0)
    elif hasattr(exc, "code"):
        try:
            code = int(getattr(exc, "code", 0) or 0)
        except (TypeError, ValueError):
            code = 0
    if code in {-404, 62002, 62004, 62012}:
        return f"video_deleted_or_hidden:{code}"
    if code in {-10403, 87007, 87008}:
        return f"vip_or_paid:{code}"
    if code == -403:
        return "access_denied:-403"
    return ""


def _parse_duration_seconds(raw: Any) -> int:
    """Parse Bilibili duration (int seconds or `MM:SS` / `HH:MM:SS` string) into seconds."""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return max(0, int(round(raw)))
    text = str(raw).strip()
    if not text:
        return 0
    if text.isdigit():
        return max(0, int(text))
    parts = text.split(":")
    if len(parts) in {2, 3} and all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def _classify_bili_exception(
    exc: BaseException, secrets: list[str]
) -> BilibiliRiskControlError | None:
    if isinstance(exc, BilibiliRiskControlError):
        return exc
    if isinstance(
        exc,
        (
            bili_exc.GeetestException,
            bili_exc.ExClimbWuzhiException,
            bili_exc.WbiRetryTimesExceedException,
        ),
    ):
        return BilibiliRiskControlError(
            f"risk_control:{type(exc).__name__}", code=-352, cooldown_seconds=2400.0
        )
    if isinstance(exc, bili_exc.ResponseCodeException):
        code = int(getattr(exc, "code", 0) or 0)
        msg_low = str(getattr(exc, "msg", "") or "").lower()
        if (
            code in RISK_CONTROL_CODES
            or "风控" in msg_low
            or "验证码" in msg_low
            or "voucher" in msg_low
        ):
            return BilibiliRiskControlError(
                f"risk_control:code={code}", code=code or -412, cooldown_seconds=1800.0
            )
    http_status = None
    if isinstance(exc, AdapterHTTPError):
        http_status = exc.status_code
    elif isinstance(exc, bili_exc.NetworkException):
        http_status = exc.status
    elif getattr(exc, "status_code", None) is not None:
        try:
            http_status = int(getattr(exc, "status_code"))
        except (TypeError, ValueError):
            http_status = None
    if http_status in {412, 403}:
        return BilibiliRiskControlError(
            f"risk_control:http={http_status}",
            code=-http_status,
            cooldown_seconds=1800.0,
        )
    if isinstance(exc, AdapterRateLimitError):
        return BilibiliRiskControlError(
            f"rate_limited:http={exc.status_code}",
            code=-429,
            cooldown_seconds=exc.retry_after,
        )
    if http_status == 429:
        return BilibiliRiskControlError(
            "rate_limited:http=429", code=-429, cooldown_seconds=300.0
        )
    redacted = redact_error_text(exc, secrets).lower()
    if any(
        tok in redacted
        for tok in (
            "-412",
            "-352",
            "-509",
            "-799",
            "risk_control:",
            "geetest",
            "v_voucher",
            "security control",
        )
    ):
        detected_code = -412
        for rc_code in (-412, -352, -509, -799):
            if str(rc_code) in redacted:
                detected_code = rc_code
                break
        return BilibiliRiskControlError(
            f"risk_control:code={detected_code}",
            code=detected_code,
            cooldown_seconds=1800.0,
        )
    return None


def validate_bilibili_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise ValueError("B站源配置必须是对象")
    mode = str(config.get("mode") or "popular").strip().lower()
    if mode not in BILI_MODES:
        raise ValueError(
            f"不支持的 B站源模式：{mode}（可选：up / popular / recommend）"
        )

    try:
        limit = int(config.get("limit", config.get("max_items", 10)))
    except (TypeError, ValueError) as exc:
        raise ValueError("拉取条数 limit 必须是整数") from exc
    limit = max(1, min(limit, 30))

    out: dict[str, Any] = {
        "mode": mode,
        "limit": limit,
    }

    if mode == "up":
        raw_uid = str(config.get("uid") or "").strip()
        if not raw_uid or not raw_uid.isdigit() or int(raw_uid) <= 0:
            raise ValueError("B站 UP 主模式必须填写有效的正整数 UID")
        out["uid"] = str(int(raw_uid))
        out["include_videos"] = bool(config.get("include_videos", True))
        out["include_dynamics"] = bool(config.get("include_dynamics", True))
        if not out["include_videos"] and not out["include_dynamics"]:
            raise ValueError("UP 主模式至少启用投稿视频或图文动态之一")
    elif mode == "popular":
        pop_kind = str(config.get("popular_kind") or "hot").strip().lower()
        if pop_kind not in POPULAR_KINDS:
            raise ValueError(
                f"不支持的榜单类型：{pop_kind}（可选：hot / weekly / rank）"
            )
        out["popular_kind"] = pop_kind

    cookie = str(config.get("cookie") or "").strip()
    sessdata = str(config.get("sessdata") or "").strip()
    if cookie:
        out["cookie"] = cookie
    if sessdata:
        out["sessdata"] = sessdata
    return out


def _video_row_to_candidate(
    row: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any] | None:
    bvid = str(row.get("bvid") or "").strip()
    if not bvid:
        uri = str(row.get("uri") or row.get("short_link_v2") or "")
        m = _BVID_RE.search(uri)
        if m:
            bvid = m.group(1)
    if not bvid:
        return None

    owner = row.get("owner") if isinstance(row.get("owner"), Mapping) else {}
    creator = str(row.get("author") or owner.get("name") or "").strip()
    up_mid = row.get("mid") or owner.get("mid")
    aid = row.get("aid") or row.get("id")
    cid = row.get("cid")
    stamp = row.get("pubdate") or row.get("created") or row.get("ctime") or time.time()

    rcmd_reason = ""
    if isinstance(row.get("rcmd_reason"), Mapping):
        rcmd_reason = str(row["rcmd_reason"].get("content") or "").strip()
    elif isinstance(row.get("rcmd_reason"), str):
        rcmd_reason = row["rcmd_reason"].strip()

    desc = str(row.get("description") or row.get("desc") or rcmd_reason or "").strip()
    title = str(row.get("title") or bvid).strip()

    meta: dict[str, Any] = {"kind": "video"}
    if aid:
        try:
            meta["aid"] = int(aid)
        except (TypeError, ValueError):
            pass
    if cid:
        try:
            meta["cid"] = int(cid)
        except (TypeError, ValueError):
            pass
    if up_mid:
        try:
            meta["up_mid"] = int(up_mid)
        except (TypeError, ValueError):
            pass
    if rcmd_reason:
        meta["rcmd_reason"] = clean_inline_field(rcmd_reason, max_chars=80)
    dur_s = _parse_duration_seconds(row.get("duration") or row.get("length"))
    if dur_s > 0:
        meta["duration_sec"] = dur_s

    return normalize_candidate(
        external_id=f"bili:bv:{bvid}",
        source=source,
        title=title,
        creator=creator or "B站UP主",
        bvid=bvid,
        summary=desc or title,
        raw_url=f"https://www.bilibili.com/video/{bvid}",
        published_at=stamp,
        content_ready=False,
        full_text="",
        meta=meta,
    )


def _dynamic_row_to_candidate(
    raw: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    skip_video_archives: bool = True,
) -> dict[str, Any] | None:
    modules = raw.get("modules") if isinstance(raw.get("modules"), Mapping) else {}
    author = (
        modules.get("module_author")
        if isinstance(modules.get("module_author"), Mapping)
        else {}
    )
    body = (
        modules.get("module_dynamic")
        if isinstance(modules.get("module_dynamic"), Mapping)
        else {}
    )
    major = body.get("major") if isinstance(body.get("major"), Mapping) else {}

    if skip_video_archives and (
        raw.get("type") == "DYNAMIC_TYPE_AV" or major.get("archive")
    ):
        return None

    item_id = str(raw.get("id_str") or "").strip()
    stamp = author.get("pub_ts") or time.time()
    if not item_id:
        return None

    opus = major.get("opus") if isinstance(major.get("opus"), Mapping) else {}
    desc_obj = body.get("desc") if isinstance(body.get("desc"), Mapping) else {}
    opus_sum = opus.get("summary") if isinstance(opus.get("summary"), Mapping) else {}

    text = str(desc_obj.get("text") or opus_sum.get("text") or "").strip()
    title = str(opus.get("title") or "").strip()

    if raw.get("type") == "DYNAMIC_TYPE_FORWARD" and isinstance(
        raw.get("orig"), Mapping
    ):
        orig_mods = (
            raw["orig"].get("modules")
            if isinstance(raw["orig"].get("modules"), Mapping)
            else {}
        )
        orig_body = (
            orig_mods.get("module_dynamic")
            if isinstance(orig_mods.get("module_dynamic"), Mapping)
            else {}
        )
        orig_major = (
            orig_body.get("major")
            if isinstance(orig_body.get("major"), Mapping)
            else {}
        )
        orig_arch = (
            orig_major.get("archive")
            if isinstance(orig_major.get("archive"), Mapping)
            else {}
        )
        orig_desc = (
            orig_body.get("desc") if isinstance(orig_body.get("desc"), Mapping) else {}
        )
        fwd_piece = str(orig_desc.get("text") or orig_arch.get("title") or "").strip()
        if fwd_piece:
            text = f"{text}\n转发动态：{fwd_piece}".strip()

    if not title and not text:
        return None

    creator = str(author.get("name") or "B站UP主").strip()
    derived_title = title or text.split("\n", 1)[0][:80]

    return normalize_candidate(
        external_id=f"bili:dyn:{item_id}",
        source=source,
        title=derived_title,
        creator=creator,
        bvid="",
        summary=text or derived_title,
        raw_url=f"https://t.bilibili.com/{item_id}",
        published_at=stamp,
        content_ready=True,
        full_text=text or derived_title,
        meta={"kind": "dynamic", "dynamic_id": item_id},
    )


async def _call_with_timeout(awaitable: Any, timeout: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout=max(1.0, float(timeout)))


async def _poll_up_mode(
    cfg: Mapping[str, Any],
    source: Mapping[str, Any],
    cred: Credential | None,
    timeout: float,
) -> list[dict[str, Any]]:
    uid = int(cfg["uid"])
    up = user.User(uid, credential=cred)
    limit = int(cfg.get("limit", 10))
    candidates: list[dict[str, Any]] = []

    if cfg.get("include_videos", True):
        try:
            vdata = await _call_with_timeout(
                up.get_videos(pn=1, ps=min(limit, 30)), timeout
            )
        except SSRFBlockedError:
            raise
        except Exception as exc:
            # A rejected first request stops this poll; do not probe other APIs.
            raise _BilibiliStageError("videos", exc) from exc
        vlist = ((vdata or {}).get("list") or {}).get("vlist") or []
        if isinstance(vlist, list):
            for row in vlist:
                if isinstance(row, Mapping):
                    cand = _video_row_to_candidate(row, source)
                    if cand:
                        candidates.append(cand)

    if cfg.get("include_dynamics", True):
        if candidates:
            await asyncio.sleep(0.2)
        try:
            ddata = await _call_with_timeout(up.get_dynamics_new(offset=""), timeout)
        except SSRFBlockedError:
            raise
        except Exception as exc:
            raise _BilibiliStageError(
                "dynamics", exc, items=candidates,
                completed=bool(cfg.get("include_videos", True)),
            ) from exc
        dlist = (ddata or {}).get("items") or []
        if isinstance(dlist, list):
            for row in dlist:
                if isinstance(row, Mapping):
                    cand = _dynamic_row_to_candidate(
                        row,
                        source,
                        skip_video_archives=bool(cfg.get("include_videos", True)),
                    )
                    if cand:
                        candidates.append(cand)

    return candidates


async def _poll_popular_mode(
    cfg: Mapping[str, Any],
    source: Mapping[str, Any],
    timeout: float,
) -> list[dict[str, Any]]:
    pop_kind = str(cfg.get("popular_kind") or "hot")
    limit = int(cfg.get("limit", 10))

    if pop_kind == "weekly":
        data = await _call_with_timeout(hot.get_weekly_hot_videos(), timeout)
    elif pop_kind == "rank":
        data = await _call_with_timeout(rank.get_rank(), timeout)
    else:
        data = await _call_with_timeout(
            hot.get_hot_videos(pn=1, ps=min(limit, 20)), timeout
        )

    rows = (data or {}).get("list") or []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            cand = _video_row_to_candidate(row, source)
            if cand:
                out.append(cand)
    return out


async def _poll_recommend_mode(
    cfg: Mapping[str, Any],
    source: Mapping[str, Any],
    cred: Credential | None,
    cookie_header: str,
    secrets: list[str],
    timeout: float,
) -> list[dict[str, Any]]:
    limit = int(cfg.get("limit", 10))
    headers = dict(_BILI_HEADERS)
    if cookie_header:
        headers["Cookie"] = cookie_header

    resp = await safe_http_get(
        BILI_RCMD_URL,
        params={"fresh_type": "3", "version": "1", "ps": str(min(limit, 20))},
        headers=headers,
        timeout=timeout,
        max_bytes=1_572_864,
        allow_private=False,
        secrets_to_redact=secrets,
    )
    payload = json.loads(resp.text())
    if not isinstance(payload, Mapping):
        raise ValueError("B站推荐流返回格式异常")

    code = int(payload.get("code", 0) or 0)
    data_obj = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    if code in RISK_CONTROL_CODES or data_obj.get("v_voucher"):
        raise BilibiliRiskControlError(
            f"risk_control:code={code or -352}",
            code=code or -352,
            cooldown_seconds=1800.0,
        )
    if code != 0:
        # Optional fallback to bilibili_api.homepage.get_videos when direct endpoint declines non-WBI query
        if cred is not None or code in {-400, -404}:
            hdata = await _call_with_timeout(
                homepage.get_videos(credential=cred), timeout
            )
            items = (hdata or {}).get("item") or []
        else:
            raise RuntimeError(f"B站推荐接口返回错误码 {code}")
    else:
        items = data_obj.get("item") or []

    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for row in items:
        if isinstance(row, Mapping):
            # Skip ad/live cards without bvid
            if row.get("goto") and str(row.get("goto")) not in {"av", "archive", ""}:
                continue
            cand = _video_row_to_candidate(row, source)
            if cand:
                out.append(cand)
    return out


async def poll_bilibili_source(
    source: Mapping[str, Any],
    cursor: Mapping[str, Any] | None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    cfg = validate_bilibili_config(
        source.get("config") if isinstance(source.get("config"), Mapping) else {}
    )
    cur = dict(cursor or {})
    now = time.time()
    cred, cookie_header, secrets = _build_readonly_credential(cfg)
    result = {"cooldown_until": 0.0, "status": "ok"}
    next_cur = dict(cur)

    try:
        mode = cfg["mode"]
        if mode == "up":
            raw_items = await _poll_up_mode(cfg, source, cred, timeout)
        elif mode == "popular":
            raw_items = await _poll_popular_mode(cfg, source, timeout)
        else:
            raw_items = await _poll_recommend_mode(
                cfg, source, cred, cookie_header, secrets, timeout
            )
    except SSRFBlockedError:
        raise
    except Exception as exc:
        stage_error = exc if isinstance(exc, _BilibiliStageError) else None
        cause = stage_error.cause if stage_error else exc
        partial = bool(stage_error and stage_error.completed)
        stage = f"{stage_error.stage}:" if stage_error else ""
        rc = _classify_bili_exception(cause, secrets)
        step = int(cur.get("backoff_step") or 0) + 1
        next_cur["backoff_step"] = step
        if rc is not None:
            wait_s = min(7200.0, rc.cooldown_seconds * (1.5 ** min(step - 1, 3)))
            result = {
                "cooldown_until": now + wait_s,
                "status": f"{'partial_' if partial else ''}risk_control_backoff: {stage}{redact_error_text(rc, secrets)}",
            }
        else:
            wait_s = min(1800.0, 90.0 * (2 ** min(step - 1, 4)))
            result = {
                "cooldown_until": now + wait_s,
                "status": f"{'partial_' if partial else ''}error: {stage}{redact_error_text(cause, secrets)}",
            }
        if not partial:
            return {**result, "items": [], "next_cursor": next_cur}
        raw_items = stage_error.items
    else:
        # A successful first stage must not clear a later stage's backoff.
        next_cur["backoff_step"] = 0

    seen_ids = list(next_cur.get("seen_ids") or [])
    seen_set = set(seen_ids)

    fresh: list[dict[str, Any]] = []
    for item in raw_items:
        eid = item["external_id"]
        if eid not in seen_set:
            fresh.append(item)

    fresh.sort(key=lambda x: float(x.get("published_at") or 0.0), reverse=True)
    selected = fresh[: cfg["limit"]]
    for item in selected:
        if item["external_id"] not in seen_set:
            seen_ids.append(item["external_id"])
            seen_set.add(item["external_id"])
    next_cur["seen_ids"] = seen_ids[-250:]

    return {
        **result,
        "items": selected,
        "next_cursor": next_cur,
    }


def _format_ai_conclusion(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    # Unwrap `data` if raw HTTP payload was returned
    root = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    code = root.get("code")
    if code is not None and int(code) not in {0}:
        return ""

    model_res = root.get("model_result")
    if not isinstance(model_res, Mapping):
        return ""

    pieces: list[str] = []
    summary = str(model_res.get("summary") or "").strip()
    if summary:
        pieces.append(summary)

    outline = model_res.get("outline")
    if isinstance(outline, list):
        for sec in outline:
            if not isinstance(sec, Mapping):
                continue
            sec_title = str(sec.get("title") or "").strip()
            bullets: list[str] = []
            part_list = sec.get("part_outline")
            if isinstance(part_list, list):
                for pt in part_list:
                    if isinstance(pt, Mapping):
                        c = str(pt.get("content") or "").strip()
                        if c:
                            bullets.append(f"- {c}")
            if sec_title and bullets:
                pieces.append(f"【{sec_title}】\n" + "\n".join(bullets))
            elif sec_title:
                pieces.append(f"【{sec_title}】")
            elif bullets:
                pieces.append("\n".join(bullets))

    if not pieces and isinstance(model_res.get("subtitle"), list):
        sub_lines: list[str] = []
        for sec in model_res["subtitle"]:
            if isinstance(sec, Mapping) and isinstance(sec.get("part_subtitle"), list):
                for item in sec["part_subtitle"]:
                    if isinstance(item, Mapping):
                        c = str(item.get("content") or "").strip()
                        if c:
                            sub_lines.append(c)
        if sub_lines:
            pieces.append("\n".join(sub_lines))

    return clean_external_text("\n\n".join(pieces), max_chars=4000)


def _pick_subtitle_track(sub_info: Any) -> tuple[str, str]:
    """Select the best subtitle URL and classify its kind (`cc` vs `ai_subtitle`)."""
    if not isinstance(sub_info, Mapping):
        return "", ""
    tracks = sub_info.get("subtitles") or sub_info.get("list") or []
    if not isinstance(tracks, list) or not tracks:
        return "", ""

    valid_tracks: list[Mapping[str, Any]] = [
        t
        for t in tracks
        if isinstance(t, Mapping) and str(t.get("subtitle_url") or "").strip()
    ]
    if not valid_tracks:
        return "", ""

    def _is_ai(track: Mapping[str, Any]) -> bool:
        lan = str(track.get("lan") or "").lower()
        return (
            lan.startswith("ai-")
            or bool(track.get("ai_type"))
            or bool(track.get("ai_status"))
        )

    def _score(track: Mapping[str, Any]) -> tuple[int, int]:
        lan = str(track.get("lan") or "").lower()
        ai_rank = 1 if _is_ai(track) else 0
        if lan in {"zh-cn", "zh-hans"}:
            return (0, ai_rank)
        if lan in {"ai-zh", "zh-hk", "zh-tw"} or lan.startswith("zh") or "zh" in lan:
            return (1, ai_rank)
        return (2, ai_rank)

    valid_tracks.sort(key=_score)
    best = valid_tracks[0]
    raw_url = str(best["subtitle_url"]).strip()
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    kind = "ai_subtitle" if _is_ai(best) else "cc"
    return raw_url, kind


def _pick_subtitle_url(sub_info: Any) -> str:
    url, _ = _pick_subtitle_track(sub_info)
    return url


def _condense_timestamped_lines(
    lines: Sequence[str], *, max_chars: int = 4500
) -> str:
    """Keep full timeline coverage (opening, middle samples, ending) when long subtitles exceed budget."""
    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined
    if len(lines) <= 6:
        return joined[:max_chars]

    head_budget = int(max_chars * 0.42)
    tail_budget = int(max_chars * 0.28)
    mid_budget = max(200, max_chars - head_budget - tail_budget - 60)

    head_lines: list[str] = []
    acc = 0
    head_end = 0
    for idx, ln in enumerate(lines):
        if acc + len(ln) + 1 > head_budget and head_lines:
            break
        head_lines.append(ln)
        acc += len(ln) + 1
        head_end = idx + 1

    tail_lines: list[str] = []
    acc = 0
    tail_start = len(lines)
    for idx in range(len(lines) - 1, head_end - 1, -1):
        ln = lines[idx]
        if acc + len(ln) + 1 > tail_budget and tail_lines:
            break
        tail_lines.append(ln)
        acc += len(ln) + 1
        tail_start = idx
    tail_lines.reverse()

    middle_pool = lines[head_end:tail_start]
    mid_lines: list[str] = []
    if middle_pool:
        approx_slots = max(3, min(len(middle_pool), mid_budget // 32))
        step = len(middle_pool) / approx_slots
        acc = 0
        for s in range(approx_slots):
            ln = middle_pool[int(s * step)]
            if acc + len(ln) + 1 > mid_budget:
                break
            mid_lines.append(ln)
            acc += len(ln) + 1

    sections = ["\n".join(head_lines)]
    if mid_lines:
        sections.append("... [中段节选] ...\n" + "\n".join(mid_lines))
    if tail_lines:
        sections.append("... [后段] ...\n" + "\n".join(tail_lines))
    return "\n".join(sections)[:max_chars]


async def _download_subtitle_text(
    sub_url: str,
    *,
    timeout: float,
    secrets: list[str],
    max_chars: int = 4500,
) -> str:
    clean_url = validate_url_sync(sub_url, allow_private=False, require_dns=False)
    resp = await safe_http_get(
        clean_url,
        headers=_BILI_HEADERS,
        timeout=timeout,
        max_bytes=1_048_576,
        allow_private=False,
        secrets_to_redact=secrets,
    )
    data = json.loads(resp.text())
    body = (data or {}).get("body") if isinstance(data, Mapping) else None
    if not isinstance(body, list):
        return ""

    lines: list[str] = []
    prev = ""
    for row in body:
        if not isinstance(row, Mapping):
            continue
        line = str(row.get("content") or "").strip()
        if not line or line == prev:
            continue
        prev = line
        raw_from = row.get("from")
        if raw_from is not None:
            try:
                ts_label = format_timestamp(float(raw_from))
                lines.append(f"[{ts_label}] {line}")
            except (TypeError, ValueError):
                lines.append(line)
        else:
            lines.append(line)
    condensed = _condense_timestamped_lines(lines, max_chars=max_chars)
    return clean_external_text(condensed, max_chars=max_chars)


def _compose_video_full_text(
    *,
    subtitle_text: str,
    audio_transcript: str,
    danmaku_sample: Sequence[str],
    ai_text: str,
    best_desc: str,
    fallback_title: str,
    has_visual: bool,
    legacy_mode: bool,
    max_chars: int,
) -> str:
    parts: list[str] = []
    if subtitle_text:
        parts.append(subtitle_text)
    elif audio_transcript:
        parts.append(f"【音频转写】\n{audio_transcript}")

    if danmaku_sample:
        parts.append("【高能弹幕采样】\n" + "\n".join(danmaku_sample))

    if ai_text:
        if parts or has_visual:
            parts.append(f"【B站AI参考提纲（辅助）】\n{ai_text[:1200]}")
        else:
            parts.append(ai_text)

    if best_desc:
        if parts or has_visual:
            parts.append(f"【视频简介】\n{best_desc[:600]}")
        else:
            parts.append(best_desc)

    if not parts:
        parts.append(fallback_title)

    # Preserve clean legacy formatting when only subtitle or only ai_conclusion exists without extras
    if legacy_mode and not danmaku_sample and not has_visual:
        if subtitle_text:
            return clean_external_text(subtitle_text, max_chars=max_chars)
        if ai_text:
            return clean_external_text(ai_text, max_chars=max_chars)
        return clean_external_text(best_desc or fallback_title, max_chars=max_chars)

    return clean_external_text("\n\n".join(parts), max_chars=max_chars)


async def fetch_bilibili_content(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    timeout: float = 25.0,
    shared_cookie: str = "",
    fallback_cookies: Sequence[str] | None = None,
    media_options: Mapping[str, Any] | None = None,
    audio_transcriber: Callable[[str, int], Awaitable[str]] | None = None,
) -> dict[str, Any]:
    """Fetch Stage-2 content for a Bilibili item with subtitle-first priority and dual-channel visuals.

    - Strictly read-only (strips `bili_jct` from source & fallback cookies).
    - Subtitle priority: official CC / AI subtitle (with `[mm:ss]`) BEFORE `ai_conclusion`.
    - Visual acquisition (when `media_options` enables it): short native video / ffmpeg keyframes /
      videoshot sprite fallback + optional lightweight high-energy danmaku & audio-STT fallback.
    - Never bypasses CAPTCHA or risk control (-412/-352/-509/-799).
    """
    meta = candidate.get("meta") if isinstance(candidate.get("meta"), Mapping) else {}
    if meta.get("kind") == "dynamic" or (
        not candidate.get("bvid") and candidate.get("full_text")
    ):
        dyn_bundle = build_empty_media_bundle(
            watch_mode="dynamic",
            content_origin="bilibili_dynamic",
        )
        return {
            "full_text": clean_external_text(
                candidate.get("full_text") or candidate.get("summary") or "",
                max_chars=4000,
            ),
            "content_origin": "bilibili_dynamic",
            "chapter_label": "",
            "total_chapters": None,
            "media_bundle": dyn_bundle,
        }

    bvid = str(candidate.get("bvid") or "").strip()
    if not bvid:
        m = _BVID_RE.search(
            str(candidate.get("raw_url") or candidate.get("external_id") or "")
        )
        if m:
            bvid = m.group(1)

    fallback_title = clean_inline_field(
        candidate.get("title") or bvid or "B站视频", max_chars=160
    )
    fallback_desc = clean_external_text(
        candidate.get("full_text")
        or candidate.get("summary")
        or candidate.get("title")
        or "",
        max_chars=4000,
    )
    legacy_mode = media_options is None
    eff_opts = normalize_media_options(
        media_options, default_visual_enabled=(not legacy_mode)
    )
    max_text_chars = 4000 if legacy_mode else 6000

    if not bvid:
        empty_bundle = build_empty_media_bundle(
            watch_mode="unavailable" if not legacy_mode else "metadata_only",
            content_origin="bilibili_description"
            if legacy_mode
            else "bilibili_unavailable_permanent",
            skip_reason="missing_bvid",
            is_permanent_unavailable=(not legacy_mode),
        )
        return {
            "full_text": fallback_desc,
            "content_origin": empty_bundle["content_origin"],
            "chapter_label": "",
            "total_chapters": None,
            "media_bundle": empty_bundle,
        }

    cfg = source.get("config") if isinstance(source.get("config"), Mapping) else {}
    cred, cookie_header, secrets = _build_readonly_credential(
        cfg, shared_cookie=shared_cookie, fallback_cookies=fallback_cookies
    )
    v = video.Video(bvid=bvid, credential=cred)

    cid = meta.get("cid")
    up_mid = meta.get("up_mid")
    duration_sec = float(_parse_duration_seconds(meta.get("duration_sec") or meta.get("duration")))
    info_desc = ""
    info_subtitle: Any = None
    is_paid_or_vip_hint = False
    info_transient_err = ""

    need_info = (
        not cid
        or not up_mid
        or not fallback_desc
        or (eff_opts["visual_enabled"] and duration_sec <= 0)
    )
    if need_info:
        try:
            info = await _call_with_timeout(v.get_info(), timeout)
            if isinstance(info, Mapping):
                if not cid:
                    cid = info.get("cid")
                    if (
                        not cid
                        and isinstance(info.get("pages"), list)
                        and info["pages"]
                    ):
                        p0 = info["pages"][0]
                        if isinstance(p0, Mapping):
                            cid = p0.get("cid")
                if not up_mid and isinstance(info.get("owner"), Mapping):
                    up_mid = info["owner"].get("mid")
                if duration_sec <= 0:
                    duration_sec = float(_parse_duration_seconds(info.get("duration")))
                info_desc = str(info.get("desc") or "").strip()
                info_subtitle = info.get("subtitle")
                rights = info.get("rights") if isinstance(info.get("rights"), Mapping) else {}
                if (
                    rights.get("pay")
                    or info.get("is_upower_exclusive")
                    or info.get("is_upower_preview")
                ):
                    is_paid_or_vip_hint = True
        except Exception as exc:
            rc = _classify_bili_exception(exc, secrets)
            if rc is not None:
                raise BilibiliRiskControlError(
                    redact_error_text(rc, secrets),
                    code=rc.code,
                    cooldown_seconds=rc.cooldown_seconds,
                ) from None
            perm_reason = _classify_permanent_unavailable(exc)
            if perm_reason:
                perm_bundle = build_empty_media_bundle(
                    watch_mode="unavailable",
                    content_origin="bilibili_unavailable_permanent",
                    duration_sec=int(round(duration_sec)),
                    skip_reason=perm_reason,
                    is_permanent_unavailable=True,
                    error_detail=redact_error_text(exc, secrets),
                )
                return {
                    "full_text": clean_external_text(
                        fallback_desc or fallback_title, max_chars=max_text_chars
                    ),
                    "content_origin": "bilibili_unavailable_permanent",
                    "chapter_label": "",
                    "total_chapters": None,
                    "media_bundle": perm_bundle,
                }
            info_transient_err = redact_error_text(exc, secrets)

    best_desc = clean_external_text(info_desc or fallback_desc, max_chars=4000)

    # (a) Official CC / AI subtitle JSON FIRST (before ai_conclusion) with compact [mm:ss]
    subtitle_text = ""
    subtitle_source = ""
    subtitle_transient_err = ""
    if cid:
        try:
            sub_meta = await _call_with_timeout(v.get_subtitle(cid=int(cid)), timeout)
            sub_url, sub_kind = _pick_subtitle_track(sub_meta)
            if not sub_url:
                sub_url, sub_kind = _pick_subtitle_track(info_subtitle)
            if sub_url:
                subtitle_text = await _download_subtitle_text(
                    sub_url,
                    timeout=timeout,
                    secrets=secrets,
                    max_chars=4500 if not legacy_mode else 4000,
                )
                if subtitle_text:
                    subtitle_source = sub_kind or "ai_subtitle"
        except (SSRFBlockedError, BilibiliRiskControlError, AdapterRateLimitError):
            raise
        except Exception as exc:
            rc = _classify_bili_exception(exc, secrets)
            if rc is not None:
                raise BilibiliRiskControlError(
                    redact_error_text(rc, secrets),
                    code=rc.code,
                    cooldown_seconds=rc.cooldown_seconds,
                ) from None
            subtitle_transient_err = redact_error_text(exc, secrets)

    has_subtitle = bool(subtitle_text)

    # (b) Dual-channel visual + optional danmaku + optional audio STT acquisition
    audio_transcript = ""
    want_media_pipeline = bool(
        cid
        and (
            eff_opts["visual_enabled"]
            or eff_opts["danmaku_enabled"]
            or callable(audio_transcriber)
        )
    )
    if want_media_pipeline:
        media_bundle = await acquire_video_media(
            video_obj=v,
            bvid=bvid,
            cid=int(cid),
            duration_sec=duration_sec,
            has_subtitle=has_subtitle,
            subtitle_source=subtitle_source,
            media_options=eff_opts,
            cookie_header=cookie_header,
            secrets=secrets,
            audio_transcriber=audio_transcriber,
            timeout=max(timeout, 40.0),
            classify_error=lambda e: _classify_bili_exception(e, secrets),
        )
        audio_transcript = str(media_bundle.pop("_audio_transcript", "") or "")
    else:
        media_bundle = build_empty_media_bundle(
            watch_mode="subtitle_only" if has_subtitle else "metadata_only",
            content_origin="bilibili_subtitle"
            if has_subtitle
            else ("bilibili_description" if legacy_mode else "bilibili_metadata_only"),
            duration_sec=int(round(duration_sec)),
            has_subtitle=has_subtitle,
            subtitle_source=subtitle_source,
            is_transient_error=bool(
                not has_subtitle and (info_transient_err or subtitle_transient_err)
            ),
            skip_reason=(
                "transient_fetch_error"
                if (not has_subtitle and (info_transient_err or subtitle_transient_err))
                else ("" if has_subtitle else "no_subtitle_or_visual")
            ),
            error_detail=info_transient_err or subtitle_transient_err,
        )

    # Promote VIP/paid hint to permanent unavailable if neither visual nor subtitle succeeded
    if (
        is_paid_or_vip_hint
        and not media_bundle["has_visual"]
        and not has_subtitle
        and not media_bundle["has_audio_transcript"]
    ):
        media_bundle["watch_mode"] = "unavailable"
        media_bundle["content_origin"] = "bilibili_unavailable_permanent"
        media_bundle["is_permanent_unavailable"] = True
        media_bundle["is_transient_error"] = False
        media_bundle["skip_reason"] = "vip_or_paid"

    # (c) Supplementary official AI conclusion (never replaces subtitles/visuals)
    ai_text = ""
    should_fetch_ai = bool(
        cid
        and not media_bundle["is_permanent_unavailable"]
        and not media_bundle["is_transient_error"]
        and (
            (legacy_mode and not has_subtitle)
            or (
                not legacy_mode
                and not has_subtitle
                and media_bundle["has_visual"]
            )
            or (not legacy_mode and duration_sec > 600 and media_bundle["has_visual"])
        )
    )
    if should_fetch_ai:
        try:
            ai_raw = await _call_with_timeout(
                v.get_ai_conclusion(
                    cid=int(cid), up_mid=int(up_mid) if up_mid else None
                ),
                timeout,
            )
            ai_text = _format_ai_conclusion(ai_raw)
        except Exception as exc:
            rc = _classify_bili_exception(exc, secrets)
            if rc is not None:
                # Only raise risk control if we have no usable subtitle/visual content yet
                if not has_subtitle and not media_bundle["has_visual"]:
                    media_bundle["cleanup"]()
                    raise BilibiliRiskControlError(
                        redact_error_text(rc, secrets),
                        code=rc.code,
                        cooldown_seconds=rc.cooldown_seconds,
                    ) from None

    # Resolve final content_origin & watch_mode consistently
    if media_bundle["has_visual"]:
        content_origin = str(media_bundle["content_origin"])
    elif has_subtitle:
        content_origin = "bilibili_subtitle"
        media_bundle["watch_mode"] = "subtitle_only"
        media_bundle["content_origin"] = content_origin
    elif media_bundle["has_audio_transcript"]:
        content_origin = "bilibili_audio_transcript"
        media_bundle["watch_mode"] = "audio_transcript"
        media_bundle["content_origin"] = content_origin
    elif media_bundle["is_permanent_unavailable"]:
        content_origin = "bilibili_unavailable_permanent"
        media_bundle["watch_mode"] = "unavailable"
        media_bundle["content_origin"] = content_origin
    elif legacy_mode:
        content_origin = "bilibili_ai_conclusion" if ai_text else "bilibili_description"
        media_bundle["watch_mode"] = "metadata_only"
        media_bundle["content_origin"] = content_origin
    else:
        content_origin = "bilibili_metadata_only"
        media_bundle["watch_mode"] = "metadata_only"
        media_bundle["content_origin"] = content_origin

    full_text = _compose_video_full_text(
        subtitle_text=subtitle_text,
        audio_transcript=audio_transcript,
        danmaku_sample=media_bundle.get("danmaku_sample") or (),
        ai_text=ai_text,
        best_desc=best_desc,
        fallback_title=fallback_title,
        has_visual=bool(media_bundle["has_visual"]),
        legacy_mode=legacy_mode,
        max_chars=max_text_chars,
    )

    return {
        "full_text": full_text,
        "content_origin": content_origin,
        "chapter_label": "",
        "total_chapters": None,
        "media_bundle": media_bundle,
    }
