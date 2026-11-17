"""RSS 2.0, Atom 1.0, and JSON Feed 1.1 adapter with ETag/Last-Modified 304 & HTTP 429 Retry-After."""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

from .manual import extract_article_from_html
from .ssrf import (
    AdapterRateLimitError,
    SSRFBlockedError,
    clean_external_text,
    clean_inline_field,
    is_masked_secret,
    normalize_candidate,
    redact_error_text,
    safe_http_get,
    safe_public_url,
    strip_html_to_text,
    validate_url_sync,
)

_XML_ENTITY_RE = re.compile(r"<!ENTITY\b", re.IGNORECASE)
_EMAIL_PAREN_RE = re.compile(r"^[^\s@]+@[^\s@]+\s*\(([^)]+)\)\s*$")


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    if ":" in tag:
        return tag.rsplit(":", 1)[1].lower()
    return tag.lower()


def _parse_feed_timestamp(raw: Any) -> float:
    s = str(raw or "").strip()
    if not s:
        return time.time()
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return time.time()


def _clean_author(raw: Any) -> str:
    s = str(raw or "").strip()
    m = _EMAIL_PAREN_RE.match(s)
    if m:
        s = m.group(1)
    return clean_inline_field(s, max_chars=80)


def validate_feed_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise ValueError("Feed 配置必须是对象")
    raw_url = str(config.get("url") or config.get("feed_url") or "").strip()
    if not raw_url:
        raise ValueError("请填写订阅源 URL")
    # Standard feeds NEVER allow private/LAN/loopback addresses
    clean_url = validate_url_sync(raw_url, allow_private=False, require_dns=False)

    try:
        limit = int(config.get("limit", 15))
    except (TypeError, ValueError) as exc:
        raise ValueError("订阅每次拉取条数必须是整数") from exc
    limit = max(1, min(limit, 50))

    fetch_full = bool(config.get("fetch_full_article", True))
    auth_token = str(config.get("auth_token") or "").strip()
    cookie = str(config.get("cookie") or "").strip()

    out: dict[str, Any] = {
        "url": clean_url,
        "limit": limit,
        "fetch_full_article": fetch_full,
    }
    if auth_token:
        out["auth_token"] = auth_token
    if cookie:
        out["cookie"] = cookie
    return out


def _parse_json_feed(
    data: Mapping[str, Any],
    *,
    base_url: str,
    source: Mapping[str, Any],
    fetch_full_article: bool,
) -> list[dict[str, Any]]:
    version = str(data.get("version") or "").strip().lower()
    if not (
        version.startswith("https://jsonfeed.org/version/")
        or version.startswith("http://jsonfeed.org/version/")
    ):
        raise ValueError("JSON Feed version 字段不合法")

    feed_title = clean_inline_field(
        data.get("title") or source.get("name") or "JSON Feed", max_chars=80
    )
    feed_author = ""
    if isinstance(data.get("authors"), list) and data["authors"]:
        first_a = data["authors"][0]
        if isinstance(first_a, Mapping):
            feed_author = _clean_author(first_a.get("name"))
    elif isinstance(data.get("author"), Mapping):
        feed_author = _clean_author(data["author"].get("name"))

    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return []

    results: list[dict[str, Any]] = []
    for entry in raw_items:
        if not isinstance(entry, Mapping):
            continue
        item_url_raw = str(entry.get("url") or entry.get("external_url") or "").strip()
        resolved_url = (
            safe_public_url(urljoin(base_url, item_url_raw), allow_private=False)
            if item_url_raw
            else ""
        )

        content_text = str(entry.get("content_text") or "").strip()
        content_html = str(entry.get("content_html") or "").strip()
        summary_raw = str(entry.get("summary") or "").strip()

        body = ""
        if content_text:
            body = clean_external_text(content_text, max_chars=4000, strip_html=False)
        elif content_html:
            body = clean_external_text(content_html, max_chars=4000, strip_html=True)

        summary_clean = clean_inline_field(summary_raw or body, max_chars=300)
        title = clean_inline_field(entry.get("title") or summary_clean, max_chars=160)
        if not title and not body and not summary_clean:
            continue

        item_author = ""
        if isinstance(entry.get("authors"), list) and entry["authors"]:
            a0 = entry["authors"][0]
            if isinstance(a0, Mapping):
                item_author = _clean_author(a0.get("name"))
        elif isinstance(entry.get("author"), Mapping):
            item_author = _clean_author(entry["author"].get("name"))
        creator = item_author or feed_author or feed_title

        raw_id = str(entry.get("id") or resolved_url or "").strip()
        if not raw_id:
            raw_id = hashlib.sha256(
                f"{title}:{summary_clean}".encode("utf-8")
            ).hexdigest()[:16]

        pub_ts = _parse_feed_timestamp(
            entry.get("date_published") or entry.get("date_modified")
        )
        has_rich_body = len(body) >= 220 or not resolved_url or not fetch_full_article
        full_text = body or (
            clean_external_text(summary_raw, max_chars=4000, strip_html=True)
            if has_rich_body
            else ""
        )

        results.append(
            normalize_candidate(
                external_id=f"feed:{raw_id}",
                source=source,
                title=title or "订阅条目",
                creator=creator,
                summary=summary_clean or full_text,
                raw_url=resolved_url,
                published_at=pub_ts,
                content_ready=bool(full_text and has_rich_body),
                full_text=full_text,
                meta={"feed_format": "jsonfeed", "feed_title": feed_title},
            )
        )
    return results


def _child_text(elem: ET.Element, names: set[str]) -> str:
    for ch in elem:
        if _local_tag(ch.tag) in names:
            txt = "".join(ch.itertext()).strip()
            if txt:
                return txt
    return ""


def _parse_xml_feed(
    xml_text: str,
    *,
    base_url: str,
    source: Mapping[str, Any],
    fetch_full_article: bool,
) -> list[dict[str, Any]]:
    if _XML_ENTITY_RE.search(xml_text):
        raise ValueError("拒绝包含 XML ENTITY 声明的不可信订阅文档")
    cleaned_xml = xml_text.lstrip("\ufeff \t\r\n")
    try:
        root = ET.fromstring(cleaned_xml)
    except ET.ParseError as exc:
        raise ValueError(f"订阅 XML 解析失败：{exc}") from exc

    root_name = _local_tag(root.tag)
    results: list[dict[str, Any]] = []

    if root_name == "feed":
        # Atom 1.0
        feed_title = clean_inline_field(
            _child_text(root, {"title"}) or source.get("name") or "Atom Feed",
            max_chars=80,
        )
        feed_author = ""
        for ch in root:
            if _local_tag(ch.tag) == "author":
                feed_author = _clean_author(
                    _child_text(ch, {"name"}) or "".join(ch.itertext())
                )
                if feed_author:
                    break

        for entry in root:
            if _local_tag(entry.tag) != "entry":
                continue
            title = clean_inline_field(_child_text(entry, {"title"}), max_chars=160)
            entry_author = ""
            alt_href = ""
            fallback_href = ""
            content_raw = ""
            summary_raw = ""
            raw_id = ""
            pub_raw = ""

            for ch in entry:
                lname = _local_tag(ch.tag)
                if lname == "id" and not raw_id:
                    raw_id = "".join(ch.itertext()).strip()
                elif lname == "author" and not entry_author:
                    entry_author = _clean_author(
                        _child_text(ch, {"name"}) or "".join(ch.itertext())
                    )
                elif lname == "link":
                    href = (ch.attrib.get("href") or "").strip()
                    rel = (ch.attrib.get("rel") or "alternate").strip().lower()
                    if href:
                        if rel == "alternate" and not alt_href:
                            alt_href = href
                        elif not fallback_href and rel not in {
                            "self",
                            "Enclosure",
                            "enclosure",
                        }:
                            fallback_href = href
                elif lname == "content" and not content_raw:
                    content_raw = "".join(ch.itertext()).strip()
                elif lname == "summary" and not summary_raw:
                    summary_raw = "".join(ch.itertext()).strip()
                elif lname == "published" and not pub_raw:
                    pub_raw = "".join(ch.itertext()).strip()
                elif lname == "updated" and not pub_raw:
                    pub_raw = "".join(ch.itertext()).strip()

            chosen_href = alt_href or fallback_href
            resolved_url = (
                safe_public_url(urljoin(base_url, chosen_href), allow_private=False)
                if chosen_href
                else ""
            )
            body = (
                clean_external_text(content_raw, max_chars=4000, strip_html=True)
                if content_raw
                else ""
            )
            summary_clean = clean_inline_field(summary_raw or body, max_chars=300)
            if not title and not body and not summary_clean:
                continue
            if not raw_id:
                raw_id = (
                    resolved_url
                    or hashlib.sha256(
                        f"{title}:{summary_clean}".encode("utf-8")
                    ).hexdigest()[:16]
                )

            has_rich_body = (
                len(body) >= 220 or not resolved_url or not fetch_full_article
            )
            full_text = body or (
                clean_external_text(summary_raw, max_chars=4000, strip_html=True)
                if has_rich_body
                else ""
            )

            results.append(
                normalize_candidate(
                    external_id=f"feed:{raw_id}",
                    source=source,
                    title=title or summary_clean or "Atom 条目",
                    creator=entry_author or feed_author or feed_title,
                    summary=summary_clean or full_text,
                    raw_url=resolved_url,
                    published_at=_parse_feed_timestamp(pub_raw),
                    content_ready=bool(full_text and has_rich_body),
                    full_text=full_text,
                    meta={"feed_format": "atom", "feed_title": feed_title},
                )
            )
        return results

    # RSS 2.0 or RDF/RSS 1.0
    channel = next((ch for ch in root if _local_tag(ch.tag) == "channel"), root)
    feed_title = clean_inline_field(
        _child_text(channel, {"title"}) or source.get("name") or "RSS Feed",
        max_chars=80,
    )
    item_elems = [ch for ch in channel if _local_tag(ch.tag) == "item"]
    if not item_elems and root is not channel:
        item_elems = [ch for ch in root if _local_tag(ch.tag) == "item"]

    for item in item_elems:
        title = clean_inline_field(_child_text(item, {"title"}), max_chars=160)
        guid = _child_text(item, {"guid", "id"})
        link_raw = _child_text(item, {"link"})
        if not link_raw:
            for ch in item:
                if _local_tag(ch.tag) == "link" and ch.attrib.get("href"):
                    link_raw = ch.attrib["href"].strip()
                    break
        resolved_url = (
            safe_public_url(urljoin(base_url, link_raw), allow_private=False)
            if link_raw
            else ""
        )

        creator = _clean_author(_child_text(item, {"creator", "author"})) or feed_title
        desc_raw = _child_text(item, {"description", "summary"})
        encoded_raw = _child_text(item, {"encoded", "content"})
        pub_raw = _child_text(item, {"pubdate", "date", "published", "updated"})

        body_source = encoded_raw or desc_raw
        body = (
            clean_external_text(body_source, max_chars=4000, strip_html=True)
            if body_source
            else ""
        )
        summary_clean = clean_inline_field(
            strip_html_to_text(desc_raw) if desc_raw else body,
            max_chars=300,
        )
        if not title and not body and not summary_clean:
            continue

        raw_id = (
            guid
            or resolved_url
            or hashlib.sha256(
                f"{title}:{pub_raw}:{summary_clean}".encode("utf-8")
            ).hexdigest()[:16]
        )
        has_rich_body = len(body) >= 220 or not resolved_url or not fetch_full_article

        results.append(
            normalize_candidate(
                external_id=f"feed:{raw_id}",
                source=source,
                title=title or summary_clean or "RSS 条目",
                creator=creator,
                summary=summary_clean or body,
                raw_url=resolved_url,
                published_at=_parse_feed_timestamp(pub_raw),
                content_ready=bool(body and has_rich_body),
                full_text=body if has_rich_body else "",
                meta={
                    "feed_format": "rss",
                    "feed_title": feed_title,
                    "inline_excerpt": body[:1200] if body else "",
                },
            )
        )
    return results


async def poll_feed_source(
    source: Mapping[str, Any],
    cursor: Mapping[str, Any] | None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    cfg = validate_feed_config(
        source.get("config") if isinstance(source.get("config"), Mapping) else {}
    )
    cur = dict(cursor or {})
    now = time.time()

    headers: dict[str, str] = {
        "Accept": "application/feed+json, application/atom+xml, application/rss+xml, application/xml, text/xml, application/json;q=0.9, */*;q=0.5"
    }
    secrets: list[str] = []
    auth_token = str(cfg.get("auth_token") or "").strip()
    if auth_token and not is_masked_secret(auth_token):
        headers["Authorization"] = f"Bearer {auth_token}"
        secrets.append(auth_token)
    cookie = str(cfg.get("cookie") or "").strip()
    if cookie and not is_masked_secret(cookie):
        headers["Cookie"] = cookie
        secrets.append(cookie)

    pending = list(cur.get("pending_items") or [])
    if pending:
        selected = pending[: cfg["limit"]]
        next_cur = dict(cur, pending_items=pending[cfg["limit"] :])
        next_cur["seen_ids"] = list(
            dict.fromkeys(
                [*(cur.get("seen_ids") or []), *[i["external_id"] for i in selected]]
            )
        )[-200:]
        return {
            "items": selected,
            "next_cursor": next_cur,
            "cooldown_until": 0.0,
            "status": "draining_backlog",
        }
    etag = str(cur.get("etag") or "").strip()
    last_modified = str(cur.get("last_modified") or "").strip()

    try:
        resp = await safe_http_get(
            cfg["url"],
            headers=headers,
            timeout=timeout,
            max_bytes=2 * 1024 * 1024,
            allow_private=False,
            etag=etag,
            last_modified=last_modified,
            secrets_to_redact=secrets,
        )
    except SSRFBlockedError:
        raise
    except AdapterRateLimitError as exc:
        step = int(cur.get("backoff_step") or 0) + 1
        # Respect server Retry-After header; apply exponential floor when repeated
        wait_s = (
            max(exc.retry_after, min(3600.0, 60.0 * (2 ** min(step - 1, 5))))
            if exc.retry_after == 300.0
            else exc.retry_after
        )
        next_cur = dict(cur)
        next_cur["backoff_step"] = step
        return {
            "items": [],
            "next_cursor": next_cur,
            "cooldown_until": now + wait_s,
            "status": f"rate_limited: retry_after={int(wait_s)}s",
        }
    except Exception as exc:
        step = int(cur.get("backoff_step") or 0) + 1
        wait_s = min(1800.0, 60.0 * (2 ** min(step - 1, 4)))
        next_cur = dict(cur)
        next_cur["backoff_step"] = step
        return {
            "items": [],
            "next_cursor": next_cur,
            "cooldown_until": now + wait_s,
            "status": f"error: {redact_error_text(exc, secrets)}",
        }

    next_cur = dict(cur)
    next_cur["backoff_step"] = 0
    if resp.header("etag"):
        next_cur["etag"] = resp.header("etag").strip()
    if resp.header("last-modified"):
        next_cur["last_modified"] = resp.header("last-modified").strip()

    if resp.status == 304:
        return {
            "items": [],
            "next_cursor": next_cur,
            "cooldown_until": 0.0,
            "status": "not_modified",
        }

    raw_text = resp.text()
    ctype = resp.header("content-type", "").lower()
    stripped = raw_text.lstrip()

    try:
        if (
            "feed+json" in ctype
            or "application/json" in ctype
            or stripped.startswith("{")
        ):
            parsed_json = json.loads(stripped)
            if not isinstance(parsed_json, Mapping):
                raise ValueError("JSON Feed 根节点必须是对象")
            parsed_items = _parse_json_feed(
                parsed_json,
                base_url=resp.url or cfg["url"],
                source=source,
                fetch_full_article=cfg["fetch_full_article"],
            )
        else:
            parsed_items = _parse_xml_feed(
                raw_text,
                base_url=resp.url or cfg["url"],
                source=source,
                fetch_full_article=cfg["fetch_full_article"],
            )
    except Exception as exc:
        # Keep the validators for the last parsed feed so the next request
        # cannot accept a 304 for content this poll never consumed.
        failed_cursor = dict(cur, backoff_step=int(cur.get("backoff_step") or 0) + 1)
        return {
            "items": [],
            "next_cursor": failed_cursor,
            "cooldown_until": now + 300.0,
            "status": f"parse_error: {redact_error_text(exc, secrets)}",
        }

    seen_ids = list(next_cur.get("seen_ids") or [])
    seen_set = set(seen_ids)
    fresh_items = [it for it in parsed_items if it["external_id"] not in seen_set]
    fresh_items.sort(key=lambda x: float(x.get("published_at") or 0.0), reverse=True)
    selected = fresh_items[: cfg["limit"]]
    next_cur["pending_items"] = fresh_items[cfg["limit"] :]

    for it in selected:
        if it["external_id"] not in seen_set:
            seen_ids.append(it["external_id"])
            seen_set.add(it["external_id"])
    next_cur["seen_ids"] = seen_ids[-200:]

    return {
        "items": selected,
        "next_cursor": next_cur,
        "cooldown_until": 0.0,
        "status": "ok",
    }


async def fetch_feed_content(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    timeout: float = 25.0,
) -> dict[str, Any]:
    existing = clean_external_text(candidate.get("full_text") or "", max_chars=4000)
    if candidate.get("content_ready") and existing:
        return {
            "full_text": existing,
            "content_origin": "feed_inline",
            "chapter_label": "",
            "total_chapters": None,
        }

    meta = candidate.get("meta") if isinstance(candidate.get("meta"), Mapping) else {}
    inline_excerpt = clean_external_text(
        meta.get("inline_excerpt") or existing or candidate.get("summary") or "",
        max_chars=4000,
    )
    raw_url = str(candidate.get("raw_url") or "").strip()
    cfg = source.get("config") if isinstance(source.get("config"), Mapping) else {}

    if raw_url and bool(cfg.get("fetch_full_article", True)):
        try:
            resp = await safe_http_get(
                raw_url,
                timeout=timeout,
                max_bytes=2 * 1024 * 1024,
                allow_private=False,
                headers={
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"
                },
            )
            extracted = extract_article_from_html(resp.text(), max_chars=4000)
            article_text = extracted["text"]
            if len(article_text) >= max(80, len(inline_excerpt)):
                return {
                    "full_text": article_text,
                    "content_origin": "feed_article",
                    "chapter_label": "",
                    "total_chapters": None,
                }
        except SSRFBlockedError:
            raise
        except Exception:
            pass

    return {
        "full_text": inline_excerpt,
        "content_origin": "feed_summary",
        "chapter_label": "",
        "total_chapters": None,
    }
