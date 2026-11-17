"""Unified registry and public contract entrypoints for Dynamics Horizons source adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .bilibili import (
    fetch_bilibili_content,
    poll_bilibili_source,
    validate_bilibili_config,
)
from .ebook import (
    fetch_ebook_content,
    parse_local_ebook,
    poll_ebook_source,
    search_public_ebooks,
    validate_ebook_config,
)
from .feed import fetch_feed_content, poll_feed_source, validate_feed_config
from .manual import (
    build_manual_candidate,
    fetch_manual_content,
    poll_manual_source,
    validate_manual_config,
)
from .ssrf import (
    MASK_TOKEN,
    SSRFBlockedError,
    clean_external_text,
    clean_inline_field,
    is_masked_secret,
    mask_secrets_in_dict,
    merge_secret_config,
)

SOURCE_KINDS = ("bilibili", "ebook", "feed", "manual")
POLL_FAILURE_OUTCOMES = {
    "error": "error",
    "parse_error": "error",
    "rate_limited": "cooldown",
    "risk_control_backoff": "cooldown",
    "partial_risk_control_backoff": "partial",
    "partial_error": "partial",
}


def validate_source_config(
    source_kind: str,
    config: dict[str, Any] | None = None,
    *,
    mask_secrets: bool = False,
    existing_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and normalize `config` for `source_kind`; optionally mask all secret fields."""
    kind = str(source_kind or "").strip().lower()
    if kind not in SOURCE_KINDS:
        raise ValueError(
            f"不支持的信息源类型：{source_kind}（可选：bilibili / ebook / feed / manual）"
        )
    if config is not None and not isinstance(config, Mapping):
        raise ValueError("信息源 config 必须是字典对象")

    merged = (
        merge_secret_config(config or {}, existing_config)
        if existing_config is not None
        else dict(config or {})
    )

    if kind == "bilibili":
        validated = validate_bilibili_config(merged)
    elif kind == "ebook":
        validated = validate_ebook_config(merged)
        if validated.get("provider") == "opds":
            from urllib.parse import urlsplit

            origins = merged.get("credential_origins", [])
            if not isinstance(origins, list):
                raise ValueError("credential_origins 必须为 HTTPS 来源列表")
            validated["credential_origins"] = []
            for value in origins:
                parsed = urlsplit(str(value))
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or parsed.path not in ("", "/")
                ):
                    raise ValueError("凭据来源必须为不含路径和账号的 HTTPS origin")
                validated["credential_origins"].append(str(value).rstrip("/"))
    elif kind == "feed":
        validated = validate_feed_config(merged)
    else:
        validated = validate_manual_config(merged)

    if mask_secrets:
        return mask_secrets_in_dict(validated)
    return validated


async def poll_source(
    source: dict[str, Any],
    cursor: dict[str, Any] | None = None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Poll an external source and distinguish successful results from failures."""
    if not isinstance(source, Mapping):
        raise ValueError("source 必须是字典对象")
    kind = str(source.get("source_kind") or "").strip().lower()
    if kind not in SOURCE_KINDS:
        raise ValueError(f"不支持的信息源类型：{kind}")

    cur = dict(cursor) if isinstance(cursor, Mapping) else {}
    effective_timeout = max(1.0, float(timeout or 20.0))

    if kind == "bilibili":
        res = await poll_bilibili_source(source, cur, timeout=effective_timeout)
    elif kind == "ebook":
        res = await poll_ebook_source(source, cur, timeout=effective_timeout)
    elif kind == "feed":
        res = await poll_feed_source(source, cur, timeout=effective_timeout)
    else:
        res = await poll_manual_source(source, cur, timeout=effective_timeout)

    items = list(res.get("items") or [])
    next_cursor = dict(res.get("next_cursor") or {})
    cooldown_until = float(res.get("cooldown_until") or 0.0)
    status = str(res.get("status") or "ok")
    status_code, _, detail = status.partition(":")
    outcome = POLL_FAILURE_OUTCOMES.get(status_code, "success")
    return {
        "items": items,
        "next_cursor": next_cursor,
        "cooldown_until": cooldown_until,
        "status": status,
        "outcome": outcome,
        "error": (detail.strip() or status_code) if outcome != "success" else "",
    }


async def fetch_item_content(
    source: dict[str, Any],
    candidate: dict[str, Any],
    *,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Stage-2 lazy content fetcher returning sanitized `{full_text, content_origin, chapter_label, total_chapters}`."""
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate 必须是字典对象")
    src = source if isinstance(source, Mapping) else {}
    kind = (
        str(src.get("source_kind") or candidate.get("source_kind") or "")
        .strip()
        .lower()
    )
    if kind not in SOURCE_KINDS:
        raise ValueError(f"不支持的信息源类型：{kind}")

    effective_timeout = max(1.0, float(timeout or 25.0))
    if kind == "bilibili":
        raw = await fetch_bilibili_content(src, candidate, timeout=effective_timeout)
    elif kind == "ebook":
        raw = await fetch_ebook_content(src, candidate, timeout=effective_timeout)
    elif kind == "feed":
        raw = await fetch_feed_content(src, candidate, timeout=effective_timeout)
    else:
        raw = await fetch_manual_content(src, candidate, timeout=effective_timeout)

    total_ch = raw.get("total_chapters")
    if total_ch is not None:
        try:
            total_ch = int(total_ch)
        except (TypeError, ValueError):
            total_ch = None

    return {
        "full_text": clean_external_text(
            raw.get("full_text") or "", max_chars=4000, strip_html=True
        ),
        "content_origin": clean_inline_field(
            raw.get("content_origin") or kind, max_chars=60
        ),
        "chapter_label": clean_inline_field(
            raw.get("chapter_label") or candidate.get("chapter_label") or "",
            max_chars=120,
        ),
        "total_chapters": total_ch,
    }


__all__ = [
    "MASK_TOKEN",
    "SOURCE_KINDS",
    "SSRFBlockedError",
    "build_manual_candidate",
    "fetch_item_content",
    "is_masked_secret",
    "mask_secrets_in_dict",
    "merge_secret_config",
    "parse_local_ebook",
    "poll_source",
    "search_public_ebooks",
    "validate_source_config",
]
