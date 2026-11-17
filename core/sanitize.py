"""Sandbox sanitization, prompt-injection stripping, spam filtering, and secret masking."""

from __future__ import annotations

import copy
import html
import re
from typing import Any
from urllib.parse import urlparse

from .contract import normalize_identity, sha256_hex

MASK_PLACEHOLDER = "******"

_SECRET_KEY_FRAGMENTS = (
    "cookie",
    "sessdata",
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "bili_jct",
)

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|iframe|svg)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_RAW_URL_RE = re.compile(
    r"(?:https?://[^\s<>'\"`)\]\uff09\u3011]+|(?<![a-zA-Z0-9])www\.[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s<>'\"`)\]\uff09\u3011]*)?)",
    re.IGNORECASE,
)
_INLINE_SECRET_RE = re.compile(
    r"(SESSDATA|bili_jct|DedeUserID__ckMd5)\s*=\s*[^;\s]+", re.IGNORECASE
)

_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"(ignore\s+(?:all\s+|previous\s+|above\s+|prior\s+)?instructions|disregard\s+(?:all\s+|previous\s+|above\s+)|forget\s+(?:all\s+|previous\s+)instructions)",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_override",
        re.compile(
            r"(system\s*prompt|developer\s*message|<\s*/?\s*untrusted|<\|im_start\||<\|im_end\||<\|endoftext\||\[system\]|###\s*(?:system|instruction))",
            re.IGNORECASE,
        ),
    ),
    (
        "zh_prompt_injection",
        re.compile(
            r"(忽略(?:之前|以上|上述|前面)(?:的)?(?:所有|全部)?(?:指令|设定|规则|提示)|无视(?:上述|以上|之前)(?:规则|设定|指令)|系统提示词|修改你的人设|篡改人设|你现在是一个|从现在起你是|退出角色扮演|作为AI(?:语言)?模型|越狱模式|jailbreak)",
            re.IGNORECASE,
        ),
    ),
    (
        "xss_or_exec",
        re.compile(
            r"(javascript\s*:|vbscript\s*:|data\s*:\s*text/html|eval\s*\(|onerror\s*=)",
            re.IGNORECASE,
        ),
    ),
)

_SPAM_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "contact_spam",
        re.compile(
            r"((?:加|添加|联系|私信)(?:我|客服|助理)?(?:微信|微信号|VX|vx|V信|威信|QQ群|Q群|企鹅群)\s*[:：]?\s*[a-zA-Z0-9_-]{4,}|(?:微信号|VX号|QQ群号)\s*[:：]\s*[a-zA-Z0-9_-]{5,})",
            re.IGNORECASE,
        ),
    ),
    (
        "promo_spam",
        re.compile(
            r"(点击(?:下方|置顶|评论区)(?:链接|蓝字)|领券购买|限时秒杀|内部优惠券|免费领资料|扫码进群|商务合作(?:请联系|V)|菠菜平台|正规博彩)",
            re.IGNORECASE,
        ),
    ),
)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", str(raw))
    cleaned = _HTML_COMMENT_RE.sub(" ", cleaned)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"</(p|div|li|h[1-6]|tr|blockquote)>", "\n", cleaned, flags=re.IGNORECASE
    )
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    return _CTRL_CHARS_RE.sub(" ", cleaned)


def strip_raw_urls(raw: str) -> str:
    if not raw:
        return ""
    cleaned = _RAW_URL_RE.sub("", str(raw))
    cleaned = _INLINE_SECRET_RE.sub(r"\1=******", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def contains_raw_url(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_RAW_URL_RE.search(value))
    if isinstance(value, dict):
        return any(contains_raw_url(k) or contains_raw_url(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(contains_raw_url(item) for item in value)
    return False


def detect_prompt_injection(raw: str) -> list[str]:
    text = str(raw or "")
    flags: list[str] = []
    for code, pattern in _INJECTION_RULES:
        if pattern.search(text):
            flags.append(code)
    return flags


def detect_ad_or_contact_spam(raw: str) -> list[str]:
    text = str(raw or "")
    flags: list[str] = []
    for code, pattern in _SPAM_RULES:
        if pattern.search(text):
            flags.append(code)
    return flags


def _normalize_whitespace(raw: str) -> str:
    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in raw.splitlines()]
    compact: list[str] = []
    for line in lines:
        if not line:
            if compact and compact[-1] != "":
                compact.append("")
            continue
        compact.append(line)
    return "\n".join(compact).strip()


def sanitize_untrusted_text(raw: str, *, max_chars: int = 4000) -> dict[str, Any]:
    """Strip HTML, raw promo URLs, prompt-injection directives, and ad/contact spam."""
    plain = strip_html(str(raw or ""))
    injection_flags = detect_prompt_injection(plain)
    spam_flags = detect_ad_or_contact_spam(plain)

    # Split on sentence/line boundaries and drop segments matching injection/spam rules.
    segments = re.split(r"(?<=[。！？.!?\n])", plain)
    kept: list[str] = []
    for seg in segments:
        if not seg.strip():
            continue
        if any(pat.search(seg) for _, pat in _INJECTION_RULES):
            continue
        if any(pat.search(seg) for _, pat in _SPAM_RULES):
            continue
        kept.append(seg)

    scrubbed = "".join(kept) if kept else plain
    for _, pat in _INJECTION_RULES:
        scrubbed = pat.sub("", scrubbed)
    for _, pat in _SPAM_RULES:
        scrubbed = pat.sub("", scrubbed)

    scrubbed = strip_raw_urls(scrubbed)
    scrubbed = scrubbed.replace("<", "‹").replace(">", "›")
    scrubbed = _normalize_whitespace(scrubbed)
    if len(scrubbed) > max_chars:
        scrubbed = scrubbed[:max_chars].rstrip()

    # Block outright if the item had nothing left after stripping injection/spam.
    blocked = bool((injection_flags or spam_flags) and len(scrubbed) < 6)
    return {
        "text": scrubbed,
        "injection_flags": injection_flags,
        "spam_flags": spam_flags,
        "blocked": blocked,
    }


def sanitize_admin_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()[:1000]
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def sanitize_citation(citation: dict[str, Any] | None) -> dict[str, str]:
    """Build a chat-safe citation object; NEVER includes raw_url or HTTP links."""
    c = citation if isinstance(citation, dict) else {}
    title = strip_raw_urls(strip_html(str(c.get("title") or "")))[:160]
    creator = strip_raw_urls(strip_html(str(c.get("creator") or "")))[:80]
    bvid_raw = str(c.get("bvid") or "").strip()
    bvid = (
        bvid_raw[:32]
        if re.fullmatch(r"(?:BV[0-9A-Za-z]{8,16}|av\d+)", bvid_raw, re.IGNORECASE)
        else ""
    )
    chapter_label = strip_raw_urls(strip_html(str(c.get("chapter_label") or "")))[:80]
    return {
        "title": title,
        "creator": creator,
        "bvid": bvid,
        "chapter_label": chapter_label,
    }


def sanitize_candidate_item(
    raw: dict[str, Any],
    *,
    source_id: str,
    source_kind: str,
    identity: str,
) -> dict[str, Any]:
    """Normalize and sandbox-sanitize a candidate item before entering SQLite."""
    norm_identity = normalize_identity(raw.get("identity") or identity)
    title_res = sanitize_untrusted_text(str(raw.get("title") or ""), max_chars=160)
    summary_res = sanitize_untrusted_text(str(raw.get("summary") or ""), max_chars=300)
    full_res = sanitize_untrusted_text(str(raw.get("full_text") or ""), max_chars=4000)
    creator_res = sanitize_untrusted_text(str(raw.get("creator") or ""), max_chars=80)
    chapter_res = sanitize_untrusted_text(
        str(raw.get("chapter_label") or ""), max_chars=80
    )

    title = title_res["text"].replace("\n", " ").strip()[:160]
    summary = summary_res["text"].strip()[:300]
    full_text = full_res["text"].strip()[:4000]
    if not summary and full_text:
        summary = full_text[:200]
    if not title:
        title = summary[:48] or "未命名条目"

    bvid_raw = str(raw.get("bvid") or "").strip()
    bvid = (
        bvid_raw[:32]
        if re.fullmatch(r"(?:BV[0-9A-Za-z]{8,16}|av\d+)", bvid_raw, re.IGNORECASE)
        else ""
    )

    ext_id = str(raw.get("external_id") or "").strip()[:256]
    if not ext_id:
        ext_id = sha256_hex([source_id, title, bvid, raw.get("raw_url") or ""])[:32]

    chapter_index = raw.get("chapter_index")
    if (
        not isinstance(chapter_index, int)
        or isinstance(chapter_index, bool)
        or chapter_index < 0
    ):
        chapter_index = None
    total_chapters = raw.get("total_chapters")
    if (
        not isinstance(total_chapters, int)
        or isinstance(total_chapters, bool)
        or total_chapters <= 0
    ):
        total_chapters = None

    published_at = raw.get("published_at")
    try:
        pub_ts = float(published_at) if published_at is not None else 0.0
    except (TypeError, ValueError):
        pub_ts = 0.0

    blocked = bool(
        title_res["blocked"]
        or (
            not summary
            and not full_text
            and (summary_res["blocked"] or full_res["blocked"])
        )
    )
    flags = sorted(
        set(
            title_res["injection_flags"]
            + summary_res["injection_flags"]
            + full_res["injection_flags"]
            + title_res["spam_flags"]
            + summary_res["spam_flags"]
            + full_res["spam_flags"]
        )
    )
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    safe_meta = mask_secrets(meta)
    if flags:
        safe_meta["sanitize_flags"] = flags

    fingerprint = sha256_hex(
        [
            norm_identity,
            source_kind,
            title.casefold(),
            bvid.casefold(),
            chapter_res["text"].casefold(),
            (summary or full_text)[:160].casefold(),
        ]
    )[:40]

    return {
        "external_id": ext_id,
        "source_id": str(source_id),
        "source_kind": str(source_kind),
        "identity": norm_identity,
        "book_id": str(raw.get("book_id") or "").strip()[:128],
        "chapter_index": chapter_index,
        "total_chapters": total_chapters,
        "title": title,
        "creator": creator_res["text"].replace("\n", " ").strip()[:80],
        "bvid": bvid,
        "chapter_label": chapter_res["text"].replace("\n", " ").strip()[:80],
        "summary": summary,
        "raw_url": sanitize_admin_url(str(raw.get("raw_url") or "")),
        "published_at": pub_ts,
        "content_ready": bool(raw.get("content_ready", bool(full_text))),
        "full_text": full_text,
        "fingerprint": fingerprint,
        "blocked": blocked,
        "meta": safe_meta,
    }


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").strip().lower()
    return any(frag in lowered for frag in _SECRET_KEY_FRAGMENTS)


def is_masked_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    return bool(s) and (s == "[REDACTED]" or all(ch in {"*", "•"} for ch in s))


def mask_secrets(config: Any) -> Any:
    """Recursively mask credentials so secrets never enter Web UI responses or prompts."""
    if isinstance(config, dict):
        out: dict[str, Any] = {}
        for k, v in config.items():
            if _is_secret_key(k):
                if isinstance(v, str) and v.strip():
                    out[k] = MASK_PLACEHOLDER
                elif v:
                    out[k] = MASK_PLACEHOLDER
                else:
                    out[k] = ""
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(config, list):
        return [mask_secrets(item) for item in config]
    if isinstance(config, str):
        return _INLINE_SECRET_RE.sub(r"\1=******", config)
    return copy.deepcopy(config)


def merge_masked_secrets(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Preserve stored credentials when an update submits masked placeholders."""
    merged = copy.deepcopy(incoming if isinstance(incoming, dict) else {})
    old = existing if isinstance(existing, dict) else {}
    for k, v in list(merged.items()):
        if _is_secret_key(k) and is_masked_placeholder(v):
            merged[k] = old.get(k, "")
        elif isinstance(v, dict) and isinstance(old.get(k), dict):
            merged[k] = merge_masked_secrets(old[k], v)
    return merged
