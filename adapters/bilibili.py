"""Read-only Bilibili source adapter (`up` / `popular` / `recommend` + AI conclusion/subtitle/desc)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import Any

from bilibili_api import (
    Credential,
    exceptions as bili_exc,
    homepage,
    hot,
    rank,
    user,
    video,
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

BILI_MODES = ("up", "popular", "recommend")
POPULAR_KINDS = ("hot", "weekly", "rank")
RISK_CONTROL_CODES = frozenset({-412, -352, -509, -799})
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
) -> tuple[Credential | None, str, list[str]]:
    parts, secrets = _extract_cookie_parts(config)
    sessdata = parts.get("SESSDATA") or parts.get("sessdata") or ""
    buvid3 = parts.get("buvid3") or ""
    dedeuserid = parts.get("DedeUserID") or parts.get("dedeuserid") or ""

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
        for tok in ("-412", "-352", "geetest", "v_voucher", "security control")
    ):
        return BilibiliRiskControlError(
            "risk_control:detected", code=-412, cooldown_seconds=1800.0
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


def _pick_subtitle_url(sub_info: Any) -> str:
    if not isinstance(sub_info, Mapping):
        return ""
    tracks = sub_info.get("subtitles") or sub_info.get("list") or []
    if not isinstance(tracks, list) or not tracks:
        return ""

    valid_tracks: list[Mapping[str, Any]] = [
        t
        for t in tracks
        if isinstance(t, Mapping) and str(t.get("subtitle_url") or "").strip()
    ]
    if not valid_tracks:
        return ""

    def _score(track: Mapping[str, Any]) -> int:
        lan = str(track.get("lan") or "").lower()
        if lan in {"zh-cn", "zh-hans"}:
            return 0
        if lan.startswith("zh") or "zh" in lan:
            return 1
        return 2

    valid_tracks.sort(key=_score)
    raw_url = str(valid_tracks[0]["subtitle_url"]).strip()
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    return raw_url


async def _download_subtitle_text(
    sub_url: str,
    *,
    timeout: float,
    secrets: list[str],
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
        if line and line != prev:
            lines.append(line)
            prev = line
    return clean_external_text("\n".join(lines), max_chars=4000)


async def fetch_bilibili_content(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Fetch Stage-2 content for a Bilibili item: AI conclusion -> CC/AI subtitle -> description.

    Never downloads video/audio media streams and never attempts CAPTCHA bypass.
    """
    meta = candidate.get("meta") if isinstance(candidate.get("meta"), Mapping) else {}
    if meta.get("kind") == "dynamic" or (
        not candidate.get("bvid") and candidate.get("full_text")
    ):
        return {
            "full_text": clean_external_text(
                candidate.get("full_text") or candidate.get("summary") or "",
                max_chars=4000,
            ),
            "content_origin": "bilibili_dynamic",
            "chapter_label": "",
            "total_chapters": None,
        }

    bvid = str(candidate.get("bvid") or "").strip()
    if not bvid:
        m = _BVID_RE.search(
            str(candidate.get("raw_url") or candidate.get("external_id") or "")
        )
        if m:
            bvid = m.group(1)

    fallback_desc = clean_external_text(
        candidate.get("full_text")
        or candidate.get("summary")
        or candidate.get("title")
        or "",
        max_chars=4000,
    )
    if not bvid:
        return {
            "full_text": fallback_desc,
            "content_origin": "bilibili_description",
            "chapter_label": "",
            "total_chapters": None,
        }

    cfg = source.get("config") if isinstance(source.get("config"), Mapping) else {}
    cred, _, secrets = _build_readonly_credential(cfg)
    v = video.Video(bvid=bvid, credential=cred)

    cid = meta.get("cid")
    up_mid = meta.get("up_mid")
    info_desc = ""
    info_subtitle: Any = None

    if not cid or not up_mid or not fallback_desc:
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
                info_desc = str(info.get("desc") or "").strip()
                info_subtitle = info.get("subtitle")
        except Exception as exc:
            rc = _classify_bili_exception(exc, secrets)
            if rc is not None:
                return {
                    "full_text": fallback_desc,
                    "content_origin": "bilibili_description",
                    "chapter_label": "",
                    "total_chapters": None,
                }

    best_desc = clean_external_text(info_desc or fallback_desc, max_chars=4000)

    # (a) Official AI conclusion (`x/web-interface/view/conclusion/get` via `get_ai_conclusion`)
    if cid:
        try:
            ai_raw = await _call_with_timeout(
                v.get_ai_conclusion(
                    cid=int(cid), up_mid=int(up_mid) if up_mid else None
                ),
                timeout,
            )
            ai_text = _format_ai_conclusion(ai_raw)
            if ai_text:
                return {
                    "full_text": ai_text,
                    "content_origin": "bilibili_ai_conclusion",
                    "chapter_label": "",
                    "total_chapters": None,
                }
        except Exception as exc:
            if _classify_bili_exception(exc, secrets) is not None:
                return {
                    "full_text": best_desc,
                    "content_origin": "bilibili_description",
                    "chapter_label": "",
                    "total_chapters": None,
                }

    # (b) CC / AI subtitle JSON (`get_subtitle` -> download subtitle body JSON)
    if cid:
        try:
            sub_meta = await _call_with_timeout(v.get_subtitle(cid=int(cid)), timeout)
            sub_url = _pick_subtitle_url(sub_meta) or _pick_subtitle_url(info_subtitle)
            if sub_url:
                sub_text = await _download_subtitle_text(
                    sub_url, timeout=timeout, secrets=secrets
                )
                if sub_text:
                    return {
                        "full_text": sub_text,
                        "content_origin": "bilibili_subtitle",
                        "chapter_label": "",
                        "total_chapters": None,
                    }
        except SSRFBlockedError:
            raise
        except Exception:
            pass

    # (c) Video description fallback
    return {
        "full_text": best_desc
        or clean_inline_field(candidate.get("title") or bvid, max_chars=160),
        "content_origin": "bilibili_description",
        "chapter_label": "",
        "total_chapters": None,
    }
