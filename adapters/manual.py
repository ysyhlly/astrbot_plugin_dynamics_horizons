"""Manual clipping & single-article URL extraction adapter (`source_kind == 'manual'`)."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from .ssrf import (
    AdapterRateLimitError,
    SSRFBlockedError,
    clean_external_text,
    clean_inline_field,
    normalize_candidate,
    redact_error_text,
    safe_http_get,
    validate_url_sync,
)

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_HARD_IGNORE_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "iframe",
        "object",
        "embed",
        "template",
        "form",
        "button",
        "select",
        "option",
        "textarea",
        "nav",
        "footer",
        "aside",
        "dialog",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "pre",
        "div",
        "section",
        "article",
        "main",
        "tr",
    }
)
_BOILERPLATE_ROLES = frozenset(
    {"navigation", "banner", "contentinfo", "complementary", "search", "menu", "dialog"}
)
_BOILERPLATE_TOKEN_RE = re.compile(
    r"(?:^|[-_\s])(nav|navbar|menubar|sidebar|footer|comment|comments|cookie|consent|advert|ads|promo|share|breadcrumb|pagination|related-posts|toc)(?:$|[-_\s])",
    re.IGNORECASE,
)


class _ArticleExtractorParser(HTMLParser):
    """Stdlib `HTMLParser` extracting `<article>` / `<main>` / `<p>` prose while stripping chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool]] = []  # (tag_name, ignored)
        self._in_title = False
        self._in_h1 = False
        self.doc_title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.meta_title = ""
        self.meta_author = ""
        self.meta_description = ""

        self._article_chunks: list[str] = []
        self._main_chunks: list[str] = []
        self._body_chunks: list[str] = []

    def _is_ignored(self) -> bool:
        return any(ign for _, ign in self._stack)

    def _inside_tag(self, name: str) -> bool:
        return any(t == name and not ign for t, ign in self._stack)

    def _inside_head(self) -> bool:
        return any(t == "head" for t, _ in self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs if k}

        if low == "meta":
            name_key = (
                (attr_map.get("property") or attr_map.get("name") or "").strip().lower()
            )
            content = attr_map.get("content", "").strip()
            if content:
                if name_key in {"og:title", "twitter:title"} and not self.meta_title:
                    self.meta_title = content
                elif (
                    name_key in {"author", "article:author", "dc.creator", "byl"}
                    and not self.meta_author
                ):
                    self.meta_author = content
                elif (
                    name_key in {"description", "og:description"}
                    and not self.meta_description
                ):
                    self.meta_description = content
            return

        if low == "br":
            self._append_break("\n")
            return
        if low in _VOID_TAGS:
            return

        if low == "title":
            self._in_title = True

        parent_ignored = self._is_ignored()
        this_ignored = parent_ignored or (low in _HARD_IGNORE_TAGS)
        if not this_ignored and low == "header" and not self._inside_tag("article"):
            this_ignored = True
        if not this_ignored:
            if (
                "hidden" in attr_map
                or attr_map.get("aria-hidden", "").lower() == "true"
            ):
                this_ignored = True
            elif attr_map.get("role", "").strip().lower() in _BOILERPLATE_ROLES:
                this_ignored = True
            elif low not in {"article", "main", "body", "html"}:
                ident_blob = f"{attr_map.get('class', '')} {attr_map.get('id', '')}"
                if ident_blob.strip() and _BOILERPLATE_TOKEN_RE.search(ident_blob):
                    this_ignored = True

        self._stack.append((low, this_ignored))
        if not this_ignored:
            if low == "h1" and not self.h1_parts:
                self._in_h1 = True
            if low in _BLOCK_TAGS:
                self._append_break(
                    "\n\n"
                    if low in {"p", "h1", "h2", "h3", "h4", "blockquote", "pre"}
                    else "\n"
                )

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low == "title":
            self._in_title = False
        if low == "h1":
            self._in_h1 = False
        if low in _VOID_TAGS:
            return

        was_ignored = self._is_ignored()
        if not was_ignored and low in _BLOCK_TAGS:
            self._append_break(
                "\n\n"
                if low in {"p", "h1", "h2", "h3", "h4", "blockquote", "pre"}
                else "\n"
            )

        for idx in range(len(self._stack) - 1, -1, -1):
            if self._stack[idx][0] == low:
                del self._stack[idx:]
                break

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._in_title:
            self.doc_title_parts.append(data)
            return
        if self._inside_head() or self._is_ignored():
            return
        if self._in_h1:
            self.h1_parts.append(data)

        if self._inside_tag("article"):
            self._article_chunks.append(data)
        if self._inside_tag("main"):
            self._main_chunks.append(data)
        self._body_chunks.append(data)

    def _append_break(self, sep: str) -> None:
        if self._inside_head() or self._is_ignored():
            return
        if self._inside_tag("article"):
            self._article_chunks.append(sep)
        if self._inside_tag("main"):
            self._main_chunks.append(sep)
        self._body_chunks.append(sep)


def extract_article_from_html(
    raw_html: str, *, max_chars: int = 4000
) -> dict[str, str]:
    """Extract clean article `{title, creator, summary, text}` from an HTML document."""
    parser = _ArticleExtractorParser()
    try:
        parser.feed(str(raw_html or ""))
        parser.close()
    except Exception:
        pass

    article_text = clean_external_text(
        "".join(parser._article_chunks), max_chars=max_chars, strip_html=False
    )
    main_text = clean_external_text(
        "".join(parser._main_chunks), max_chars=max_chars, strip_html=False
    )
    body_text = clean_external_text(
        "".join(parser._body_chunks), max_chars=max_chars, strip_html=False
    )

    if len(article_text) >= 60 or (
        article_text and len(body_text) <= len(article_text) * 2
    ):
        chosen = article_text
    elif len(main_text) >= 60 or (main_text and len(body_text) <= len(main_text) * 2):
        chosen = main_text
    else:
        chosen = body_text

    title = (
        clean_inline_field(parser.meta_title, max_chars=160)
        or clean_inline_field("".join(parser.h1_parts), max_chars=160)
        or clean_inline_field("".join(parser.doc_title_parts), max_chars=160)
    )
    creator = clean_inline_field(parser.meta_author, max_chars=80)
    summary = clean_inline_field(parser.meta_description or chosen, max_chars=300)
    return {
        "title": title,
        "creator": creator,
        "summary": summary,
        "text": chosen,
    }


def validate_manual_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = dict(config or {})
    url = str(cfg.get("url") or "").strip()
    if url:
        url = validate_url_sync(url, allow_private=False, require_dns=False)
    text = clean_external_text(
        cfg.get("text") or cfg.get("content") or "", max_chars=4000
    )
    title = clean_inline_field(cfg.get("title") or "", max_chars=160)
    creator = clean_inline_field(
        cfg.get("creator") or cfg.get("default_creator") or "", max_chars=80
    )

    out: dict[str, Any] = {
        "title": title,
        "creator": creator,
        "text": text,
        "url": url,
    }
    if cfg.get("default_creator"):
        out["default_creator"] = clean_inline_field(
            cfg.get("default_creator"), max_chars=80
        )
    return out


async def build_manual_candidate(
    source: Mapping[str, Any],
    *,
    title: str = "",
    creator: str = "",
    text: str = "",
    url: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Create a `NormalizedCandidate` from pasted clipping text and/or a public article URL."""
    clean_url = (
        validate_url_sync(url, allow_private=False, require_dns=False)
        if str(url or "").strip()
        else ""
    )
    body_text = clean_external_text(text, max_chars=4000)
    extracted_title = ""
    extracted_creator = ""
    extracted_summary = ""
    origin = "manual_text"

    if clean_url and not body_text:
        resp = await safe_http_get(
            clean_url,
            timeout=timeout,
            max_bytes=2 * 1024 * 1024,
            allow_private=False,
            headers={
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"
            },
        )
        ctype = resp.header("content-type", "").lower()
        raw_str = resp.text()
        if "text/plain" in ctype and "<html" not in raw_str[:500].lower():
            body_text = clean_external_text(raw_str, max_chars=4000)
        else:
            parsed = extract_article_from_html(raw_str, max_chars=4000)
            body_text = parsed["text"]
            extracted_title = parsed["title"]
            extracted_creator = parsed["creator"]
            extracted_summary = parsed["summary"]
        origin = "manual_url"

    if not body_text:
        raise ValueError("投喂内容为空，请粘贴正文或提供可提取正文的公开文章链接")

    first_line = body_text.split("\n", 1)[0].strip()
    final_title = (
        clean_inline_field(title or extracted_title or first_line, max_chars=160)
        or "管理员剪报"
    )
    final_creator = (
        clean_inline_field(
            creator or extracted_creator or source.get("name") or "管理员投喂",
            max_chars=80,
        )
        or "管理员投喂"
    )
    digest_seed = f"{clean_url}\n{final_title}\n{body_text[:1000]}"
    ext_id = "manual:" + hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()[:20]

    return normalize_candidate(
        external_id=ext_id,
        source=source,
        title=final_title,
        creator=final_creator,
        summary=extracted_summary or body_text,
        raw_url=clean_url,
        published_at=time.time(),
        content_ready=True,
        full_text=body_text,
        meta={"content_origin": origin},
    )


async def poll_manual_source(
    source: Mapping[str, Any],
    cursor: Mapping[str, Any] | None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    cfg = validate_manual_config(
        source.get("config") if isinstance(source.get("config"), Mapping) else {}
    )
    cur = dict(cursor or {})
    if not cfg.get("text") and not cfg.get("url"):
        return {
            "items": [],
            "next_cursor": cur,
            "cooldown_until": 0.0,
            "status": "idle",
        }

    now = time.time()
    try:
        item = await build_manual_candidate(
            source,
            title=cfg.get("title", ""),
            creator=cfg.get("creator", ""),
            text=cfg.get("text", ""),
            url=cfg.get("url", ""),
            timeout=timeout,
        )
    except SSRFBlockedError:
        raise
    except AdapterRateLimitError as exc:
        return {
            "items": [],
            "next_cursor": cur,
            "cooldown_until": now + exc.retry_after,
            "status": f"rate_limited: retry_after={int(exc.retry_after)}s",
        }
    except Exception as exc:
        return {
            "items": [],
            "next_cursor": cur,
            "cooldown_until": now + 120.0,
            "status": f"error: {redact_error_text(exc)}",
        }

    seen_ids = list(cur.get("seen_ids") or [])
    if item["external_id"] in seen_ids:
        return {
            "items": [],
            "next_cursor": cur,
            "cooldown_until": 0.0,
            "status": "ok",
        }

    seen_ids.append(item["external_id"])
    cur["seen_ids"] = seen_ids[-100:]
    return {
        "items": [item],
        "next_cursor": cur,
        "cooldown_until": 0.0,
        "status": "ok",
    }


async def fetch_manual_content(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    timeout: float = 25.0,
) -> dict[str, Any]:
    existing = clean_external_text(candidate.get("full_text") or "", max_chars=4000)
    if existing:
        origin = str(
            (candidate.get("meta") or {}).get("content_origin") or "manual_text"
        )
        return {
            "full_text": existing,
            "content_origin": origin,
            "chapter_label": clean_inline_field(
                candidate.get("chapter_label") or "", max_chars=120
            ),
            "total_chapters": candidate.get("total_chapters"),
        }

    raw_url = str(
        candidate.get("raw_url") or (source.get("config") or {}).get("url") or ""
    ).strip()
    if raw_url:
        rebuilt = await build_manual_candidate(
            source,
            title=str(candidate.get("title") or ""),
            creator=str(candidate.get("creator") or ""),
            url=raw_url,
            timeout=timeout,
        )
        return {
            "full_text": rebuilt["full_text"],
            "content_origin": "manual_url",
            "chapter_label": "",
            "total_chapters": None,
        }

    fallback = clean_external_text(candidate.get("summary") or "", max_chars=4000)
    return {
        "full_text": fallback,
        "content_origin": "manual_summary",
        "chapter_label": "",
        "total_chapters": None,
    }
