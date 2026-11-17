"""Ebook adapter: Wikisource, Gutendex, OPDS (Calibre-Web/Talebook), and local `.epub`/`.txt`."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import posixpath
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

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

EBOOK_PROVIDERS = ("wikisource", "gutendex", "opds", "local")

MAX_RAW_EBOOK_BYTES = 25 * 1024 * 1024
MAX_EPUB_MEMBERS = 2000
MAX_MEMBER_UNCOMPRESSED = 8 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 120

_XML_ENTITY_RE = re.compile(r"<!ENTITY\b", re.IGNORECASE)
_WIKI_LANG_RE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{2,8})?$")

_CHAPTER_HEADING_RE = re.compile(
    r"^\s*("
    r"第[零一二三四五六七八九十百千万两〇0-9]{1,8}[章节回卷部篇集幕折]\b[^\n]{0,60}"
    r"|(?:楔子|引子|序言|序章|序幕|前言|尾声|后记|终章|番外)\b[^\n]{0,50}"
    r"|(?:Chapter|CHAPTER|Part|PART|Book|BOOK|Section)\s+[0-9IVXLCDM]+\b[^\n]{0,80}"
    r")\s*$",
    re.MULTILINE,
)
_TXT_TITLE_META_RE = re.compile(
    r"^\s*(?:书名|作品|Title)\s*[：:]\s*(.+)$", re.IGNORECASE
)
_TXT_AUTHOR_META_RE = re.compile(
    r"^\s*(?:作者|著者|Author)\s*[：:]\s*(.+)$", re.IGNORECASE
)
_BOOK_BRACKET_RE = re.compile(r"《([^》]{1,80})》")

_GUTENBERG_START_RE = re.compile(
    r"\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*{3}",
    re.IGNORECASE,
)
_GUTENBERG_END_RE = re.compile(
    r"\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*{3}",
    re.IGNORECASE,
)
_WIKI_EDIT_RE = re.compile(r"\[(?:编辑|edit)\]", re.IGNORECASE)


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    if ":" in tag:
        return tag.rsplit(":", 1)[1].lower()
    return tag.lower()


def _safe_xml_fromstring(xml_text: str) -> ET.Element:
    if _XML_ENTITY_RE.search(xml_text):
        raise ValueError("拒绝包含 XML ENTITY 声明的文档（防 XML 炸弹/XXE）")
    cleaned = xml_text.lstrip("\ufeff \t\r\n")
    try:
        return ET.fromstring(cleaned)
    except ET.ParseError as exc:
        raise ValueError(f"XML 解析失败：{exc}") from exc


def _decode_text_bytes(raw_bytes: bytes) -> str:
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw_bytes.decode("utf-16")
    else:
        text = ""
        for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
            try:
                text = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            raise ValueError("无法识别文本文件编码（支持 UTF-8 / UTF-8-SIG / GB18030）")

    if "\x00" in text[:4096]:
        raise ValueError("检测到二进制内容，不是有效的纯文本电子书")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _window_text(body: str, chunk_chars: int) -> list[str]:
    """Split prose cleanly into windows of `<= chunk_chars` on paragraph/sentence boundaries."""
    cleaned = clean_external_text(
        body, max_chars=max(len(body) + 64, chunk_chars), strip_html=False
    )
    if not cleaned:
        return []
    if len(cleaned) <= chunk_chars:
        return [cleaned]

    chunks: list[str] = []
    pos = 0
    total = len(cleaned)
    min_cut = max(200, int(chunk_chars * 0.55))

    while pos < total:
        remaining = total - pos
        if remaining <= chunk_chars:
            tail = cleaned[pos:].strip()
            if tail:
                chunks.append(tail)
            break

        window = cleaned[pos : pos + chunk_chars]
        cut = -1
        for sep in ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? "):
            idx = window.rfind(sep)
            if idx >= min_cut:
                cut = idx + (0 if sep.startswith("\n") else len(sep))
                break
        if cut <= 0:
            cut = chunk_chars

        piece = cleaned[pos : pos + cut].strip()
        if piece:
            chunks.append(piece)
        pos += cut
        while pos < total and cleaned[pos] in " \t\n":
            pos += 1

    return chunks


def _build_indexed_chapters(
    raw_sections: list[tuple[str, str]],
    *,
    chunk_chars: int,
) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    for base_label, body in raw_sections:
        windows = _window_text(body, chunk_chars)
        if not windows:
            continue
        clean_label = clean_inline_field(base_label, max_chars=100)
        if len(windows) == 1:
            chapters.append(
                {
                    "index": len(chapters),
                    "label": clean_label or f"第{len(chapters) + 1}节",
                    "text": windows[0],
                }
            )
        else:
            for part_no, win in enumerate(windows, start=1):
                prefix = clean_label or f"第{len(chapters) + 1}节"
                chapters.append(
                    {
                        "index": len(chapters),
                        "label": clean_inline_field(
                            f"{prefix} ({part_no}/{len(windows)})", max_chars=120
                        ),
                        "text": win,
                    }
                )
    return chapters


def _parse_txt_ebook(
    filename: str, raw_bytes: bytes, *, chunk_chars: int
) -> dict[str, Any]:
    text = _decode_text_bytes(raw_bytes)
    stem = Path(filename or "ebook.txt").stem.strip()
    m_bracket = _BOOK_BRACKET_RE.search(stem)
    title = (
        clean_inline_field(m_bracket.group(1) if m_bracket else stem, max_chars=160)
        or "本地文本书"
    )
    creator = ""

    header_lines = text.split("\n", 20)[:18]
    for ln in header_lines:
        if not creator:
            ma = _TXT_AUTHOR_META_RE.match(ln)
            if ma:
                creator = clean_inline_field(ma.group(1), max_chars=80)
        mt = _TXT_TITLE_META_RE.match(ln)
        if mt:
            cand_t = clean_inline_field(mt.group(1).strip("《》 "), max_chars=160)
            if cand_t:
                title = cand_t

    matches = list(_CHAPTER_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if matches:
        preamble = text[: matches[0].start()].strip()
        if len(preamble) >= 120:
            sections.append(("楔子 / 前言", preamble))
        for idx, m in enumerate(matches):
            heading = m.group(1).strip()
            body_start = m.end()
            body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            body = text[body_start:body_end].strip()
            if body:
                sections.append((heading, body))
    else:
        for part_idx, win in enumerate(_window_text(text, chunk_chars), start=1):
            sections.append((f"第{part_idx}节", win))

    chapters = _build_indexed_chapters(sections, chunk_chars=chunk_chars)
    if not chapters and text.strip():
        chapters = [
            {
                "index": 0,
                "label": "正文",
                "text": clean_external_text(text, max_chars=chunk_chars),
            }
        ]
    if not chapters:
        raise ValueError("TXT 电子书内容为空")

    return {
        "title": title,
        "creator": creator,
        "chapters": chapters,
    }


def _validate_zip_member_path(member_name: str) -> str:
    if not member_name or "\x00" in member_name:
        raise ValueError("EPUB 包含非法空文件名或控制字符")
    if (
        member_name.startswith(("/", "\\"))
        or "\\" in member_name
        or ":" in member_name.split("/", 1)[0]
    ):
        raise ValueError(f"EPUB 检测到潜在路径穿越条目：{member_name}")
    pure = PurePosixPath(member_name)
    parts = pure.parts
    if any(p in {"..", ""} for p in parts):
        raise ValueError(f"EPUB 检测到非法相对路径：{member_name}")
    normalized = posixpath.normpath(member_name)
    if normalized.startswith("..") or normalized.startswith("/"):
        raise ValueError(f"EPUB 路径越界：{member_name}")
    return normalized


def _resolve_epub_href(base_dir: str, href: str) -> str:
    clean_href = unquote(href.split("#", 1)[0].split("?", 1)[0]).strip()
    if not clean_href:
        return ""
    if clean_href.startswith(("/", "\\")) or ":" in clean_href.split("/", 1)[0]:
        raise ValueError(f"EPUB manifest 引用越界路径：{href}")
    joined = (
        posixpath.normpath(posixpath.join(base_dir, clean_href))
        if base_dir
        else posixpath.normpath(clean_href)
    )
    if joined == "." or joined.startswith("..") or joined.startswith("/"):
        raise ValueError(f"EPUB manifest 相对路径逃逸：{href}")
    return joined


class _SafeEpubReader:
    """Bounded, path-traversal-safe, zip-bomb-guarded in-memory EPUB archive wrapper."""

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        infos = zf.infolist()
        if len(infos) > MAX_EPUB_MEMBERS:
            raise ValueError(f"EPUB 条目数超过安全上限 ({MAX_EPUB_MEMBERS})")

        self.by_path: dict[str, zipfile.ZipInfo] = {}
        self.by_lower: dict[str, zipfile.ZipInfo] = {}
        declared_total = 0

        for info in infos:
            norm = _validate_zip_member_path(info.filename)
            if info.is_dir():
                continue
            if info.file_size > MAX_MEMBER_UNCOMPRESSED:
                raise ValueError(f"EPUB 单个文件超过解压安全上限：{norm}")
            if info.file_size > 65536 and info.compress_size > 0:
                if (
                    info.file_size / max(info.compress_size, 1)
                ) > MAX_COMPRESSION_RATIO:
                    raise ValueError(f"EPUB 检测到疑似 Zip 炸弹高压缩比条目：{norm}")
            declared_total += max(0, int(info.file_size))
            if declared_total > MAX_TOTAL_UNCOMPRESSED:
                raise ValueError("EPUB 声明总体积超过安全上限")
            self.by_path[norm] = info
            self.by_lower[norm.lower()] = info

        self.total_read_bytes = 0

    def find_info(self, path: str) -> zipfile.ZipInfo | None:
        norm = posixpath.normpath(path)
        return self.by_path.get(norm) or self.by_lower.get(norm.lower())

    def read_bytes(
        self, path: str, *, max_bytes: int = MAX_MEMBER_UNCOMPRESSED
    ) -> bytes:
        info = self.find_info(path)
        if info is None:
            raise KeyError(f"EPUB 缺少文件：{path}")
        cap = min(max_bytes, MAX_MEMBER_UNCOMPRESSED)
        buf = bytearray()
        with self.zf.open(info, "r") as fp:
            while True:
                chunk = fp.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > cap:
                    raise ValueError(f"EPUB 解压实际字节数超出上限：{info.filename}")
                if len(buf) > 65536 and info.compress_size > 0:
                    if (len(buf) / max(info.compress_size, 1)) > MAX_COMPRESSION_RATIO:
                        raise ValueError(
                            f"EPUB 解压流检测到疑似 Zip 炸弹：{info.filename}"
                        )
                self.total_read_bytes += len(chunk)
                if self.total_read_bytes > MAX_TOTAL_UNCOMPRESSED:
                    raise ValueError("EPUB 累计解压体积超过安全上限")
        return bytes(buf)

    def read_text(self, path: str) -> str:
        raw = self.read_bytes(path)
        return _decode_text_bytes(raw)


def _parse_ncx_labels(reader: _SafeEpubReader, ncx_path: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    ncx_dir = posixpath.dirname(ncx_path)
    try:
        root = _safe_xml_fromstring(reader.read_text(ncx_path))
    except Exception:
        return labels

    for elem in root.iter():
        if _local_tag(elem.tag) != "navpoint":
            continue
        label_txt = ""
        src_href = ""
        for ch in elem:
            lname = _local_tag(ch.tag)
            if lname == "navlabel" and not label_txt:
                label_txt = clean_inline_field("".join(ch.itertext()), max_chars=100)
            elif lname == "content" and not src_href:
                raw_src = ch.attrib.get("src", "")
                if raw_src:
                    try:
                        src_href = _resolve_epub_href(ncx_dir, raw_src)
                    except ValueError:
                        src_href = ""
        if src_href and label_txt and src_href not in labels:
            labels[src_href] = label_txt
    return labels


def _parse_epub_ebook(
    filename: str, raw_bytes: bytes, *, chunk_chars: int
) -> dict[str, Any]:
    bio = io.BytesIO(raw_bytes)
    if not zipfile.is_zipfile(bio):
        raise ValueError("无效的 EPUB 文件（非合法 ZIP 容器）")
    bio.seek(0)

    with zipfile.ZipFile(bio, "r") as zf:
        reader = _SafeEpubReader(zf)
        opf_path = ""
        if reader.find_info("META-INF/container.xml") is not None:
            container_root = _safe_xml_fromstring(
                reader.read_text("META-INF/container.xml")
            )
            for elem in container_root.iter():
                if _local_tag(elem.tag) == "rootfile":
                    fp = (elem.attrib.get("full-path") or "").strip()
                    if fp:
                        opf_path = _validate_zip_member_path(fp)
                        break

        if not opf_path:
            for candidate_name in reader.by_path:
                if candidate_name.lower().endswith(".opf"):
                    opf_path = candidate_name
                    break
        if not opf_path or reader.find_info(opf_path) is None:
            raise ValueError("EPUB 缺少有效的 OPF 包描述文件")

        opf_dir = posixpath.dirname(opf_path)
        opf_root = _safe_xml_fromstring(reader.read_text(opf_path))

        title = ""
        creator = ""
        manifest: dict[
            str, tuple[str, str, str]
        ] = {}  # id -> (resolved_path, media_type, properties)
        spine_ids: list[str] = []
        fallback_spine_ids: list[str] = []
        toc_id = ""

        for child in opf_root:
            cname = _local_tag(child.tag)
            if cname == "metadata":
                for m in child.iter():
                    mname = _local_tag(m.tag)
                    txt = "".join(m.itertext()).strip()
                    if not txt:
                        continue
                    if mname == "title" and not title:
                        title = clean_inline_field(txt, max_chars=160)
                    elif mname == "creator" and not creator:
                        creator = clean_inline_field(txt, max_chars=80)
            elif cname == "manifest":
                for item in child:
                    if _local_tag(item.tag) != "item":
                        continue
                    item_id = (item.attrib.get("id") or "").strip()
                    href = (item.attrib.get("href") or "").strip()
                    mtype = (item.attrib.get("media-type") or "").strip().lower()
                    props = (item.attrib.get("properties") or "").strip().lower()
                    if item_id and href:
                        resolved = _resolve_epub_href(opf_dir, href)
                        manifest[item_id] = (resolved, mtype, props)
            elif cname == "spine":
                toc_id = (child.attrib.get("toc") or "").strip()
                for itemref in child:
                    if _local_tag(itemref.tag) != "itemref":
                        continue
                    idref = (itemref.attrib.get("idref") or "").strip()
                    linear = (itemref.attrib.get("linear") or "yes").strip().lower()
                    if idref:
                        fallback_spine_ids.append(idref)
                        if linear != "no":
                            spine_ids.append(idref)

        ordered_ids = spine_ids or fallback_spine_ids
        toc_labels: dict[str, str] = {}
        if toc_id and toc_id in manifest:
            toc_labels.update(_parse_ncx_labels(reader, manifest[toc_id][0]))
        else:
            for m_path, m_type, _ in manifest.values():
                if "dtbncx" in m_type or m_path.lower().endswith(".ncx"):
                    toc_labels.update(_parse_ncx_labels(reader, m_path))
                    break

        sections: list[tuple[str, str]] = []
        for idx, item_id in enumerate(ordered_ids):
            entry = manifest.get(item_id)
            if not entry:
                continue
            item_path, mtype, props = entry
            if "nav" in props.split() and len(ordered_ids) > 1:
                continue
            if mtype and not any(
                tok in mtype for tok in ("html", "xhtml", "xml", "text")
            ):
                continue
            if reader.find_info(item_path) is None:
                continue

            raw_doc = reader.read_text(item_path)
            if _XML_ENTITY_RE.search(raw_doc):
                raise ValueError("EPUB 章节包含受限 XML ENTITY 声明")
            parsed_html = extract_article_from_html(
                raw_doc, max_chars=max(chunk_chars * 8, 24000)
            )
            prose = parsed_html["text"].strip()
            if len(prose) < 6:
                continue
            ch_label = (
                toc_labels.get(item_path)
                or parsed_html["title"]
                or f"第{len(sections) + 1}章"
            )
            sections.append((ch_label, prose))

    stem = Path(filename or "ebook.epub").stem.strip()
    final_title = title or clean_inline_field(stem, max_chars=160) or "本地 EPUB 电子书"
    chapters = _build_indexed_chapters(sections, chunk_chars=chunk_chars)
    if not chapters:
        raise ValueError("EPUB 未能提取出有效正文章节")

    return {
        "title": final_title,
        "creator": creator,
        "chapters": chapters,
    }


def parse_local_ebook(
    filename: str,
    raw_bytes: bytes,
    *,
    chunk_chars: int = 3200,
) -> dict[str, Any]:
    """Parse a local `.txt` or `.epub` archive into `{title, creator, chapters}`."""
    if not isinstance(raw_bytes, (bytes, bytearray)) or not raw_bytes:
        raise ValueError("电子书文件内容不能为空")
    data = bytes(raw_bytes)
    if len(data) > MAX_RAW_EBOOK_BYTES:
        raise ValueError(
            f"电子书文件体积超过安全上限 ({MAX_RAW_EBOOK_BYTES // (1024 * 1024)}MB)"
        )

    try:
        window = int(chunk_chars)
    except (TypeError, ValueError):
        window = 3200
    window = max(500, min(window, 10000))

    name = str(filename or "").strip()
    ext = Path(name).suffix.lower()
    is_zip_magic = data[:4] == b"PK\x03\x04"

    if ext == ".epub" or (not ext and is_zip_magic):
        return _parse_epub_ebook(name or "book.epub", data, chunk_chars=window)
    if ext in {".txt", ".text", ".md"} or (not ext and not is_zip_magic):
        if is_zip_magic:
            return _parse_epub_ebook(name or "book.epub", data, chunk_chars=window)
        return _parse_txt_ebook(name or "book.txt", data, chunk_chars=window)
    raise ValueError(
        f"不支持的本地电子书格式：{ext or 'unknown'}（仅支持 .epub 与 .txt）"
    )


def _normalize_wiki_lang(language: str) -> str:
    lang = str(language or "zh").strip().lower()
    if not _WIKI_LANG_RE.match(lang):
        raise ValueError(f"无效的语言代码：{language}")
    return lang


async def search_public_ebooks(
    provider: str,
    query: str,
    *,
    language: str = "zh",
    limit: int = 10,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Search public-domain ebooks via `wikisource` or `gutendex` for one-click bookshelf addition."""
    prov = str(provider or "").strip().lower()
    if prov not in {"wikisource", "gutendex"}:
        raise ValueError(
            f"不支持的公版书检索源：{provider}（可选：wikisource / gutendex）"
        )

    q = clean_inline_field(query, max_chars=120)
    if not q:
        return []
    cap = max(1, min(int(limit or 10), 25))

    if prov == "wikisource":
        lang = _normalize_wiki_lang(language or "zh")
        api_url = f"https://{lang}.wikisource.org/w/api.php"
        resp = await safe_http_get(
            api_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": q,
                "srlimit": str(cap),
                "format": "json",
                "utf8": "1",
            },
            timeout=timeout,
            max_bytes=1_048_576,
            allow_private=False,
        )
        payload = json.loads(resp.text())
        rows = ((payload or {}).get("query") or {}).get("search") or []
        results: list[dict[str, Any]] = []
        if not isinstance(rows, list):
            return results
        for row in rows[:cap]:
            if not isinstance(row, Mapping):
                continue
            page_title = clean_inline_field(row.get("title") or "", max_chars=160)
            if not page_title:
                continue
            page_id = str(row.get("pageid") or page_title)
            snippet = clean_inline_field(
                strip_html_to_text(str(row.get("snippet") or "")), max_chars=300
            )
            raw_url = (
                f"https://{lang}.wikisource.org/wiki/{quote(page_title, safe='/')}"
            )
            results.append(
                {
                    "provider": "wikisource",
                    "source_kind": "ebook",
                    "id": page_id,
                    "book_id": f"wikisource:{lang}:{page_title}",
                    "page": page_title,
                    "title": page_title,
                    "creator": "维基文库公版文献",
                    "language": lang,
                    "summary": snippet,
                    "snippet": snippet,
                    "url": raw_url,
                    "raw_url": raw_url,
                    "download_count": 0,
                    "config": {
                        "provider": "wikisource",
                        "language": lang,
                        "page": page_title,
                        "title": page_title,
                        "url": raw_url,
                    },
                }
            )
        return results

    # gutendex
    params: dict[str, str] = {"search": q}
    lang_clean = str(language or "").strip().lower()
    if lang_clean and lang_clean != "all":
        params["languages"] = _normalize_wiki_lang(lang_clean)

    resp = await safe_http_get(
        "https://gutendex.com/books",
        params=params,
        timeout=timeout,
        max_bytes=1_572_864,
        allow_private=False,
    )
    payload = json.loads(resp.text())
    rows = (payload or {}).get("results") or []
    results = []
    if not isinstance(rows, list):
        return results

    for row in rows[:cap]:
        if not isinstance(row, Mapping):
            continue
        gid = row.get("id")
        if gid is None:
            continue
        gid_str = str(gid)
        title = clean_inline_field(
            row.get("title") or f"Gutenberg #{gid_str}", max_chars=160
        )
        authors = row.get("authors") if isinstance(row.get("authors"), list) else []
        author_names = [
            clean_inline_field(a.get("name"), max_chars=60)
            for a in authors
            if isinstance(a, Mapping) and a.get("name")
        ]
        creator = (
            clean_inline_field(" / ".join(author_names), max_chars=80)
            or "Project Gutenberg"
        )

        summaries = (
            row.get("summaries") if isinstance(row.get("summaries"), list) else []
        )
        subjects = row.get("subjects") if isinstance(row.get("subjects"), list) else []
        summary_raw = (
            str(summaries[0]) if summaries else "; ".join(str(s) for s in subjects[:4])
        )
        summary = clean_inline_field(summary_raw, max_chars=300)

        formats = row.get("formats") if isinstance(row.get("formats"), Mapping) else {}
        best_dl = _pick_gutendex_format_url(formats)
        langs = row.get("languages") if isinstance(row.get("languages"), list) else []
        book_lang = str(langs[0]) if langs else (lang_clean or "en")
        raw_url = f"https://www.gutenberg.org/ebooks/{gid_str}"

        cfg: dict[str, Any] = {
            "provider": "gutendex",
            "book_id": gid_str,
            "language": book_lang,
            "title": title,
            "creator": creator,
        }
        if best_dl:
            cfg["download_url"] = best_dl

        results.append(
            {
                "provider": "gutendex",
                "source_kind": "ebook",
                "id": gid_str,
                "gutenberg_id": int(gid_str) if gid_str.isdigit() else gid_str,
                "book_id": f"gutendex:{gid_str}",
                "title": title,
                "creator": creator,
                "authors": author_names,
                "language": book_lang,
                "summary": summary,
                "download_url": best_dl,
                "raw_url": raw_url,
                "download_count": int(row.get("download_count") or 0),
                "config": cfg,
            }
        )
    return results


def _pick_gutendex_format_url(formats: Mapping[str, Any]) -> str:
    preferred_prefixes = (
        "text/plain; charset=utf-8",
        "text/plain; charset=us-ascii",
        "text/plain",
        "application/epub+zip",
        "text/html; charset=utf-8",
        "text/html",
    )
    normalized_items = [
        (str(k).lower().strip(), str(v).strip())
        for k, v in formats.items()
        if v and not str(v).lower().endswith(".zip") or "epub" in str(k).lower()
    ]
    for pref in preferred_prefixes:
        for mime, url in normalized_items:
            if mime.startswith(pref):
                safe = safe_public_url(url, allow_private=False)
                if safe:
                    return safe
    for mime, url in formats.items():
        m_low = str(mime).lower()
        if any(t in m_low for t in ("text/plain", "epub", "text/html")):
            safe = safe_public_url(url, allow_private=False)
            if safe:
                return safe
    return ""


def _strip_gutenberg_boilerplate(text: str) -> str:
    s = text
    m_start = _GUTENBERG_START_RE.search(s)
    if m_start:
        s = s[m_start.end() :]
    m_end = _GUTENBERG_END_RE.search(s)
    if m_end:
        s = s[: m_end.start()]
    return s.strip()


def validate_ebook_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise ValueError("电子书源配置必须是对象")

    provider = (
        str(config.get("provider") or config.get("mode") or "local").strip().lower()
    )
    if provider not in EBOOK_PROVIDERS:
        raise ValueError(
            f"不支持的电子书源类型：{provider}（可选：wikisource / gutendex / opds / local）"
        )

    try:
        chunk_chars = int(config.get("chunk_chars", 3200))
    except (TypeError, ValueError) as exc:
        raise ValueError("章节切片字数 chunk_chars 必须是整数") from exc
    chunk_chars = max(500, min(chunk_chars, 10000))

    out: dict[str, Any] = {
        "provider": provider,
        "chunk_chars": chunk_chars,
    }

    if provider == "wikisource":
        lang = _normalize_wiki_lang(str(config.get("language") or "zh"))
        page = clean_inline_field(
            config.get("page") or config.get("title") or "", max_chars=160
        )
        raw_url = str(config.get("url") or "").strip()
        if raw_url:
            clean_url = validate_url_sync(
                raw_url, allow_private=False, require_dns=False
            )
            sp = urlsplit(clean_url)
            host = (sp.hostname or "").lower()
            if not (host == "wikisource.org" or host.endswith(".wikisource.org")):
                raise ValueError("维基文库 URL 必须属于 wikisource.org 域名")
            if host.endswith(".wikisource.org"):
                sub = host[: -len(".wikisource.org")]
                if _WIKI_LANG_RE.match(sub):
                    lang = sub
            if not page and "/wiki/" in sp.path:
                page = clean_inline_field(
                    unquote(sp.path.split("/wiki/", 1)[1]), max_chars=160
                )
            out["url"] = clean_url
        if not page:
            raise ValueError("维基文库源请填写条目名称 page（如 呐喊/狂人日记）")
        out["language"] = lang
        out["page"] = page
        if config.get("title"):
            out["title"] = clean_inline_field(config.get("title"), max_chars=160)
        return out

    if provider == "gutendex":
        raw_id = str(config.get("book_id") or config.get("gutenberg_id") or "").strip()
        if raw_id.startswith("gutendex:"):
            raw_id = raw_id.split(":", 1)[1].strip()
        query = clean_inline_field(
            config.get("query") or config.get("search") or "", max_chars=120
        )
        dl_url = str(config.get("download_url") or config.get("url") or "").strip()
        if dl_url:
            dl_url = validate_url_sync(dl_url, allow_private=False, require_dns=False)
            out["download_url"] = dl_url
        if raw_id:
            if raw_id.isdigit():
                if int(raw_id) <= 0:
                    raise ValueError("Gutendex book_id 必须是正整数")
                out["book_id"] = str(int(raw_id))
            else:
                # Allow search phrase entered in the single book_id/query input box
                query = query or clean_inline_field(raw_id, max_chars=120)
        if query:
            out["query"] = query
        if (
            not out.get("book_id")
            and not out.get("download_url")
            and not out.get("query")
        ):
            raise ValueError(
                "Gutendex 电子书源需要填写书籍 ID、检索关键词或有效的公网下载链接"
            )
        out["language"] = str(config.get("language") or "en").strip().lower()[:12]
        if config.get("title"):
            out["title"] = clean_inline_field(config.get("title"), max_chars=160)
        if config.get("creator"):
            out["creator"] = clean_inline_field(config.get("creator"), max_chars=80)
        return out

    if provider == "opds":
        allow_private = bool(config.get("allow_private_opds", False))
        raw_url = str(config.get("url") or config.get("feed_url") or "").strip()
        if not raw_url:
            raise ValueError("OPDS 书库源请填写目录 URL")
        clean_url = validate_url_sync(
            raw_url, allow_private=allow_private, require_dns=False
        )
        out["url"] = clean_url
        out["allow_private_opds"] = allow_private
        origins = config.get("credential_origins", [])
        if not isinstance(origins, list):
            raise ValueError("credential_origins must be a list")
        out["credential_origins"] = []
        for origin in origins:
            parsed = urlsplit(str(origin))
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in ("", "/")
            ):
                raise ValueError("credential_origins must contain HTTPS origins only")
            out["credential_origins"].append(str(origin).rstrip("/"))
        username = str(config.get("username") or "").strip()
        password = str(config.get("password") or "").strip()
        auth_token = str(config.get("auth_token") or "").strip()
        if username:
            out["username"] = username[:120]
        if password:
            out["password"] = password
        if auth_token:
            out["auth_token"] = auth_token
        return out

    # local
    for field in ("book_id", "title", "creator", "filename"):
        if config.get(field):
            out[field] = clean_inline_field(config.get(field), max_chars=160)
    if isinstance(config.get("chapters"), list):
        out["chapters"] = [
            {
                "index": int(ch.get("index", idx)),
                "label": clean_inline_field(
                    ch.get("label") or f"第{idx + 1}章", max_chars=120
                ),
                "text": clean_external_text(
                    ch.get("text") or "", max_chars=max(chunk_chars, 4000)
                ),
            }
            for idx, ch in enumerate(config["chapters"])
            if isinstance(ch, Mapping) and str(ch.get("text") or "").strip()
        ]
    elif config.get("content_base64") and config.get("filename"):
        out["filename"] = str(config["filename"])[:160]
        out["content_base64"] = str(config["content_base64"])
    return out


def _opds_auth_headers(cfg: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    headers: dict[str, str] = {}
    secrets: list[str] = []
    token = str(cfg.get("auth_token") or "").strip()
    username = str(cfg.get("username") or "").strip()
    password = str(cfg.get("password") or "").strip()

    if token and not is_masked_secret(token):
        headers["Authorization"] = f"Bearer {token}"
        secrets.append(token)
    elif username and password and not is_masked_secret(password):
        raw_pair = f"{username}:{password}".encode("utf-8")
        b64 = base64.b64encode(raw_pair).decode("ascii")
        headers["Authorization"] = f"Basic {b64}"
        secrets.extend([password, b64])
    return headers, secrets


def _extract_wikisource_parse(
    parse_obj: Mapping[str, Any],
    *,
    base_page: str,
    chunk_chars: int,
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, str]]]:
    """Return `(title, creator, direct_chapters, subpage_links)`."""
    display = strip_html_to_text(
        str(parse_obj.get("displaytitle") or parse_obj.get("title") or base_page)
    )
    title = clean_inline_field(display or base_page, max_chars=160)
    creator = ""

    html_block = ""
    if isinstance(parse_obj.get("text"), Mapping):
        html_block = str(parse_obj["text"].get("*") or "")
    elif isinstance(parse_obj.get("text"), str):
        html_block = parse_obj["text"]

    wikitext = ""
    if isinstance(parse_obj.get("wikitext"), Mapping):
        wikitext = str(parse_obj["wikitext"].get("*") or "")
    elif isinstance(parse_obj.get("wikitext"), str):
        wikitext = parse_obj["wikitext"]

    if wikitext:
        m_author = re.search(
            r"\|\s*(?:author|作者)\s*=\s*((?:\[\[[^\]]*\]\]|[^\n|}])+)",
            wikitext,
            re.IGNORECASE,
        )
        if m_author:
            creator = clean_inline_field(
                re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", m_author.group(1)),
                max_chars=80,
            )

    # Detect subpage chapter links for multi-chapter Wikisource index pages
    subpages: list[dict[str, str]] = []
    seen_sub: set[str] = set()
    if wikitext:
        for m in re.finditer(
            r"\[\[((?:/|(?:"
            + re.escape(base_page)
            + r")/)[^\]|#]+)(?:\|([^\]]+))?\]\]",
            wikitext,
        ):
            raw_target = m.group(1).strip()
            sub_target = (
                f"{base_page}{raw_target}" if raw_target.startswith("/") else raw_target
            )
            label = clean_inline_field(
                m.group(2) or sub_target.rsplit("/", 1)[-1], max_chars=100
            )
            if sub_target not in seen_sub:
                seen_sub.add(sub_target)
                subpages.append({"subpage": sub_target, "label": label})

    if not subpages and html_block:
        encoded_prefix = f"/wiki/{quote(base_page, safe='/')}/"
        for m in re.finditer(
            r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html_block,
            re.IGNORECASE | re.DOTALL,
        ):
            href = unquote(m.group(1).split("#", 1)[0])
            raw_prefix = f"/wiki/{base_page}/"
            if href.startswith(raw_prefix) or m.group(1).startswith(encoded_prefix):
                sub_target = href[len("/wiki/") :]
                label = clean_inline_field(
                    strip_html_to_text(m.group(2)) or sub_target.rsplit("/", 1)[-1],
                    max_chars=100,
                )
                if sub_target and sub_target not in seen_sub:
                    seen_sub.add(sub_target)
                    subpages.append({"subpage": sub_target, "label": label})

    prose = ""
    if html_block:
        extracted = extract_article_from_html(
            html_block, max_chars=max(chunk_chars * 12, 40000)
        )
        prose = _WIKI_EDIT_RE.sub("", extracted["text"]).strip()
    elif wikitext:
        stripped_wt = re.sub(r"\{\{[^{}]*\}\}", "", wikitext)
        stripped_wt = re.sub(
            r"\[\[(?:Category|分类):[^\]]+\]\]", "", stripped_wt, flags=re.IGNORECASE
        )
        stripped_wt = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", stripped_wt)
        prose = clean_external_text(stripped_wt, max_chars=max(chunk_chars * 12, 40000))

    # If it's clearly an index page (many subpage links and very little prose beyond TOC list), return subpage list
    if len(subpages) >= 2 and len(prose) < max(500, len(subpages) * 45):
        return title, creator or "维基文库", [], subpages

    matches = list(_CHAPTER_HEADING_RE.finditer(prose))
    if matches:
        sections: list[tuple[str, str]] = []
        pre = prose[: matches[0].start()].strip()
        if len(pre) >= 120:
            sections.append(("导言", pre))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(prose)
            body = prose[m.end() : end].strip()
            if body:
                sections.append((m.group(1).strip(), body))
        chapters = _build_indexed_chapters(sections, chunk_chars=chunk_chars)
    else:
        windows = _window_text(prose, chunk_chars)
        chapters = [
            {
                "index": idx,
                "label": f"第{idx + 1}节" if len(windows) > 1 else title,
                "text": win,
            }
            for idx, win in enumerate(windows)
        ]

    return title, creator or "维基文库", chapters, []


async def _poll_wikisource(
    cfg: Mapping[str, Any],
    source: Mapping[str, Any],
    cur: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    lang = cfg["language"]
    page = cfg["page"]
    chunk_chars = cfg["chunk_chars"]
    chapter_idx = max(0, int(cur.get("chapter_cursor", 0) or 0))
    api_url = f"https://{lang}.wikisource.org/w/api.php"

    resp = await safe_http_get(
        api_url,
        params={
            "action": "parse",
            "page": page,
            "prop": "text|wikitext|displaytitle",
            "redirects": "1",
            "format": "json",
            "utf8": "1",
        },
        timeout=timeout,
        max_bytes=4 * 1024 * 1024,
        allow_private=False,
    )
    payload = json.loads(resp.text())
    parse_obj = (payload or {}).get("parse")
    if not isinstance(parse_obj, Mapping):
        raise ValueError(f"维基文库未找到页面：{page}")

    title, creator, chapters, subpages = _extract_wikisource_parse(
        parse_obj, base_page=page, chunk_chars=chunk_chars
    )
    book_id = f"wikisource:{lang}:{page}"
    canonical_url = f"https://{lang}.wikisource.org/wiki/{quote(page, safe='/')}"

    if subpages:
        total = len(subpages)
        if chapter_idx >= total:
            next_cur = {
                **cur,
                "chapter_cursor": total,
                "total_chapters": total,
                "book_id": book_id,
            }
            return {
                "items": [],
                "next_cursor": next_cur,
                "cooldown_until": 0.0,
                "status": "finished",
            }
        sub = subpages[chapter_idx]
        sub_url = (
            f"https://{lang}.wikisource.org/wiki/{quote(sub['subpage'], safe='/')}"
        )
        cand = normalize_candidate(
            external_id=f"{book_id}:ch:{chapter_idx}",
            source=source,
            title=f"{title} · {sub['label']}",
            creator=creator,
            chapter_label=sub["label"],
            chapter_index=chapter_idx,
            total_chapters=total,
            book_id=book_id,
            summary=f"《{title}》连载章节：{sub['label']}（第 {chapter_idx + 1}/{total} 章）",
            raw_url=sub_url,
            content_ready=False,
            full_text="",
            meta={
                "provider": "wikisource",
                "language": lang,
                "subpage": sub["subpage"],
                "book_title": title,
            },
        )
        next_cur = {
            **cur,
            "chapter_cursor": chapter_idx + 1,
            "total_chapters": total,
            "book_id": book_id,
        }
        return {
            "items": [cand],
            "next_cursor": next_cur,
            "cooldown_until": 0.0,
            "status": "ok",
        }

    total = len(chapters)
    if total == 0:
        return {
            "items": [],
            "next_cursor": cur,
            "cooldown_until": 0.0,
            "status": "empty",
        }
    if chapter_idx >= total:
        next_cur = {
            **cur,
            "chapter_cursor": total,
            "total_chapters": total,
            "book_id": book_id,
        }
        return {
            "items": [],
            "next_cursor": next_cur,
            "cooldown_until": 0.0,
            "status": "finished",
        }

    ch = chapters[chapter_idx]
    cand = normalize_candidate(
        external_id=f"{book_id}:ch:{chapter_idx}",
        source=source,
        title=f"{title} · {ch['label']}" if ch["label"] != title else title,
        creator=creator,
        chapter_label=ch["label"],
        chapter_index=chapter_idx,
        total_chapters=total,
        book_id=book_id,
        summary=ch["text"],
        raw_url=canonical_url,
        content_ready=True,
        full_text=ch["text"],
        meta={
            "provider": "wikisource",
            "language": lang,
            "page": page,
            "book_title": title,
        },
    )
    next_cur = {
        **cur,
        "chapter_cursor": chapter_idx + 1,
        "total_chapters": total,
        "book_id": book_id,
    }
    return {
        "items": [cand],
        "next_cursor": next_cur,
        "cooldown_until": 0.0,
        "status": "ok",
    }


async def _load_gutendex_book(
    cfg: Mapping[str, Any],
    *,
    timeout: float,
) -> tuple[str, str, str, str, list[dict[str, Any]]]:
    chunk_chars = int(cfg.get("chunk_chars", 3200))
    gid = str(cfg.get("book_id") or "").strip()
    query = str(cfg.get("query") or "").strip()
    dl_url = str(cfg.get("download_url") or "").strip()
    title = clean_inline_field(cfg.get("title") or "", max_chars=160)
    creator = clean_inline_field(cfg.get("creator") or "", max_chars=80)

    if not gid and not dl_url and query:
        hits = await search_public_ebooks(
            "gutendex",
            query,
            language=str(cfg.get("language") or "en"),
            limit=1,
            timeout=timeout,
        )
        if not hits:
            raise ValueError(f"Gutendex 未检索到匹配书目：{query}")
        top = hits[0]
        gid = str(top.get("id") or "")
        title = title or str(top.get("title") or "")
        creator = creator or str(top.get("creator") or "")
        dl_url = dl_url or str(top.get("download_url") or "")

    if not dl_url or not title:
        if not gid:
            raise ValueError("缺少 Gutendex book_id")
        meta_resp = await safe_http_get(
            f"https://gutendex.com/books/{gid}",
            timeout=timeout,
            max_bytes=1_048_576,
            allow_private=False,
        )
        meta_json = json.loads(meta_resp.text())
        if not isinstance(meta_json, Mapping):
            raise ValueError("Gutendex 元数据格式无效")
        if not title:
            title = clean_inline_field(
                meta_json.get("title") or f"Gutenberg #{gid}", max_chars=160
            )
        if not creator:
            authors = (
                meta_json.get("authors")
                if isinstance(meta_json.get("authors"), list)
                else []
            )
            names = [
                clean_inline_field(a.get("name"), max_chars=60)
                for a in authors
                if isinstance(a, Mapping) and a.get("name")
            ]
            creator = (
                clean_inline_field(" / ".join(names), max_chars=80)
                or "Project Gutenberg"
            )
        if not dl_url:
            formats = (
                meta_json.get("formats")
                if isinstance(meta_json.get("formats"), Mapping)
                else {}
            )
            dl_url = _pick_gutendex_format_url(formats)

    if not dl_url:
        raise ValueError("该公版书未提供可下载的纯文本/EPUB/HTML 链接")

    file_resp = await safe_http_get(
        dl_url,
        timeout=timeout,
        max_bytes=12 * 1024 * 1024,
        allow_private=False,
    )
    ctype = file_resp.header("content-type", "").lower()
    raw = file_resp.body

    if "epub" in ctype or dl_url.lower().endswith(".epub") or raw[:4] == b"PK\x03\x04":
        parsed = parse_local_ebook(
            f"{title or 'gutenberg'}.epub", raw, chunk_chars=chunk_chars
        )
        chapters = parsed["chapters"]
    elif "html" in ctype or dl_url.lower().endswith((".htm", ".html")):
        html_str = _strip_gutenberg_boilerplate(file_resp.text())
        art = extract_article_from_html(
            html_str, max_chars=max(chunk_chars * 20, 80000)
        )
        parsed = parse_local_ebook(
            f"{title or 'gutenberg'}.txt",
            art["text"].encode("utf-8"),
            chunk_chars=chunk_chars,
        )
        chapters = parsed["chapters"]
    else:
        plain = _strip_gutenberg_boilerplate(_decode_text_bytes(raw))
        parsed = parse_local_ebook(
            f"{title or 'gutenberg'}.txt",
            plain.encode("utf-8"),
            chunk_chars=chunk_chars,
        )
        chapters = parsed["chapters"]

    book_id = (
        f"gutendex:{gid}"
        if gid
        else "gutendex:" + hashlib.sha256(dl_url.encode("utf-8")).hexdigest()[:12]
    )
    canonical_url = f"https://www.gutenberg.org/ebooks/{gid}" if gid else dl_url
    return (
        book_id,
        title or parsed["title"],
        creator or parsed["creator"] or "Project Gutenberg",
        canonical_url,
        chapters,
    )


async def _poll_gutendex(
    cfg: Mapping[str, Any],
    source: Mapping[str, Any],
    cur: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    chapter_idx = max(0, int(cur.get("chapter_cursor", 0) or 0))
    book_id, title, creator, canonical_url, chapters = await _load_gutendex_book(
        cfg, timeout=timeout
    )
    total = len(chapters)
    if total == 0:
        return {
            "items": [],
            "next_cursor": cur,
            "cooldown_until": 0.0,
            "status": "empty",
        }
    if chapter_idx >= total:
        next_cur = {
            **cur,
            "chapter_cursor": total,
            "total_chapters": total,
            "book_id": book_id,
        }
        return {
            "items": [],
            "next_cursor": next_cur,
            "cooldown_until": 0.0,
            "status": "finished",
        }

    ch = chapters[chapter_idx]
    cand = normalize_candidate(
        external_id=f"{book_id}:ch:{chapter_idx}",
        source=source,
        title=f"{title} · {ch['label']}",
        creator=creator,
        chapter_label=ch["label"],
        chapter_index=chapter_idx,
        total_chapters=total,
        book_id=book_id,
        summary=ch["text"],
        raw_url=canonical_url,
        content_ready=True,
        full_text=ch["text"],
        meta={"provider": "gutendex", "book_title": title},
    )
    next_cur = {
        **cur,
        "chapter_cursor": chapter_idx + 1,
        "total_chapters": total,
        "book_id": book_id,
    }
    return {
        "items": [cand],
        "next_cursor": next_cur,
        "cooldown_until": 0.0,
        "status": "ok",
    }


def _parse_opds_catalog(
    xml_text: str,
    *,
    feed_url: str,
    allow_private: bool,
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    root = _safe_xml_fromstring(xml_text)
    if _local_tag(root.tag) != "feed":
        raise ValueError("OPDS 目录根节点必须是 Atom <feed>")

    catalog_title = ""
    for ch in root:
        if _local_tag(ch.tag) == "title":
            catalog_title = clean_inline_field("".join(ch.itertext()), max_chars=80)
            break

    candidates: list[dict[str, Any]] = []
    for entry in root:
        if _local_tag(entry.tag) != "entry":
            continue
        entry_id = ""
        title = ""
        creator = ""
        summary = ""
        acq_url = ""
        acq_type = ""
        alt_url = ""

        for ch in entry:
            lname = _local_tag(ch.tag)
            if lname == "id" and not entry_id:
                entry_id = "".join(ch.itertext()).strip()
            elif lname == "title" and not title:
                title = clean_inline_field("".join(ch.itertext()), max_chars=160)
            elif lname == "author" and not creator:
                for sub in ch:
                    if _local_tag(sub.tag) == "name":
                        creator = clean_inline_field(
                            "".join(sub.itertext()), max_chars=80
                        )
                        break
            elif lname in {"summary", "content"} and not summary:
                summary = clean_inline_field(
                    strip_html_to_text("".join(ch.itertext())), max_chars=300
                )
            elif lname == "link":
                href = (ch.attrib.get("href") or "").strip()
                rel = (ch.attrib.get("rel") or "").strip().lower()
                mtype = (ch.attrib.get("type") or "").strip().lower()
                if not href:
                    continue
                resolved = safe_public_url(
                    urljoin(feed_url, href), allow_private=allow_private
                )
                if not resolved:
                    continue
                is_supported_acq = (
                    "opds-spec.org/acquisition" in rel
                    or mtype in {"application/epub+zip", "text/plain"}
                    or resolved.lower().endswith((".epub", ".txt"))
                )
                if is_supported_acq:
                    # Prefer EPUB or plain text over generic HTML/PDF
                    if any(k in mtype for k in ("pdf", "mobi")):
                        continue
                    if not acq_url or "epub" in mtype or "text/plain" in mtype:
                        acq_url = resolved
                        acq_type = mtype or (
                            "application/epub+zip"
                            if resolved.lower().endswith(".epub")
                            else "text/plain"
                        )
                elif rel == "alternate" and not alt_url:
                    alt_url = resolved

        if not acq_url or not title:
            continue

        book_hash = hashlib.sha256((entry_id or acq_url).encode("utf-8")).hexdigest()[
            :14
        ]
        book_id = f"opds:{book_hash}"
        candidates.append(
            normalize_candidate(
                external_id=f"{book_id}:ch:0",
                source=source,
                title=title,
                creator=creator or catalog_title or "OPDS 书架",
                chapter_label="第1章",
                chapter_index=0,
                total_chapters=None,
                book_id=book_id,
                summary=summary or f"来自 OPDS 书库《{title}》",
                raw_url=alt_url or acq_url,
                content_ready=False,
                full_text="",
                meta={
                    "provider": "opds",
                    "acquisition_url": acq_url,
                    "acquisition_type": acq_type,
                    "book_title": title,
                },
                allow_private_url=allow_private,
            )
        )
    return candidates


async def _poll_opds(
    cfg: Mapping[str, Any],
    source: Mapping[str, Any],
    cur: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    allow_private = bool(cfg.get("allow_private_opds", False))
    headers, secrets = _opds_auth_headers(cfg)
    headers["Accept"] = (
        "application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5"
    )

    resp = await safe_http_get(
        cfg["url"],
        headers=headers,
        timeout=timeout,
        max_bytes=3 * 1024 * 1024,
        allow_private=allow_private,
        etag=str(cur.get("etag") or ""),
        last_modified=str(cur.get("last_modified") or ""),
        secrets_to_redact=secrets,
    )

    next_cur = dict(cur)
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

    items = _parse_opds_catalog(
        resp.text(),
        feed_url=resp.url or cfg["url"],
        allow_private=allow_private,
        source=source,
    )
    seen_ids = list(next_cur.get("seen_ids") or [])
    seen_set = set(seen_ids)
    fresh = [it for it in items if it["external_id"] not in seen_set][:15]
    for it in fresh:
        seen_ids.append(it["external_id"])
        seen_set.add(it["external_id"])
    next_cur["seen_ids"] = seen_ids[-200:]
    return {
        "items": fresh,
        "next_cursor": next_cur,
        "cooldown_until": 0.0,
        "status": "ok",
    }


def _poll_local(
    cfg: Mapping[str, Any],
    source: Mapping[str, Any],
    cur: dict[str, Any],
) -> dict[str, Any]:
    chapters = cfg.get("chapters")
    if (
        not isinstance(chapters, list)
        and cfg.get("content_base64")
        and cfg.get("filename")
    ):
        raw = base64.b64decode(str(cfg["content_base64"]))
        parsed = parse_local_ebook(
            str(cfg["filename"]), raw, chunk_chars=int(cfg.get("chunk_chars", 3200))
        )
        chapters = parsed["chapters"]
        title = cfg.get("title") or parsed["title"]
        creator = cfg.get("creator") or parsed["creator"]
    else:
        title = str(cfg.get("title") or source.get("name") or "本地书目")
        creator = str(cfg.get("creator") or "")

    if not isinstance(chapters, list) or not chapters:
        return {"items": [], "next_cursor": cur, "cooldown_until": 0.0, "status": "ok"}

    chapter_idx = max(0, int(cur.get("chapter_cursor", 0) or 0))
    total = len(chapters)
    book_id = str(cfg.get("book_id") or f"local:{source.get('source_id') or 'book'}")
    if chapter_idx >= total:
        next_cur = {
            **cur,
            "chapter_cursor": total,
            "total_chapters": total,
            "book_id": book_id,
        }
        return {
            "items": [],
            "next_cursor": next_cur,
            "cooldown_until": 0.0,
            "status": "finished",
        }

    ch = chapters[chapter_idx]
    cand = normalize_candidate(
        external_id=f"{book_id}:ch:{chapter_idx}",
        source=source,
        title=f"{title} · {ch['label']}",
        creator=creator,
        chapter_label=ch["label"],
        chapter_index=chapter_idx,
        total_chapters=total,
        book_id=book_id,
        summary=ch["text"],
        raw_url="",
        content_ready=True,
        full_text=ch["text"],
        meta={"provider": "local", "book_title": title},
    )
    next_cur = {
        **cur,
        "chapter_cursor": chapter_idx + 1,
        "total_chapters": total,
        "book_id": book_id,
    }
    return {
        "items": [cand],
        "next_cursor": next_cur,
        "cooldown_until": 0.0,
        "status": "ok",
    }


async def poll_ebook_source(
    source: Mapping[str, Any],
    cursor: Mapping[str, Any] | None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    cfg = validate_ebook_config(
        source.get("config") if isinstance(source.get("config"), Mapping) else {}
    )
    cur = dict(cursor or {})
    now = time.time()
    _, secrets = _opds_auth_headers(cfg)

    try:
        prov = cfg["provider"]
        if prov == "wikisource":
            return await _poll_wikisource(cfg, source, cur, timeout)
        if prov == "gutendex":
            return await _poll_gutendex(cfg, source, cur, timeout)
        if prov == "opds":
            return await _poll_opds(cfg, source, cur, timeout)
        return _poll_local(cfg, source, cur)
    except SSRFBlockedError:
        raise
    except AdapterRateLimitError as exc:
        next_cur = dict(cur)
        next_cur["backoff_step"] = int(cur.get("backoff_step") or 0) + 1
        return {
            "items": [],
            "next_cursor": next_cur,
            "cooldown_until": now + exc.retry_after,
            "status": f"rate_limited: retry_after={int(exc.retry_after)}s",
        }
    except Exception as exc:
        step = int(cur.get("backoff_step") or 0) + 1
        wait_s = min(1800.0, 90.0 * (2 ** min(step - 1, 4)))
        next_cur = dict(cur)
        next_cur["backoff_step"] = step
        return {
            "items": [],
            "next_cursor": next_cur,
            "cooldown_until": now + wait_s,
            "status": f"error: {redact_error_text(exc, secrets)}",
        }


async def fetch_ebook_content(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    timeout: float = 25.0,
) -> dict[str, Any]:
    existing = clean_external_text(candidate.get("full_text") or "", max_chars=4000)
    meta = candidate.get("meta") if isinstance(candidate.get("meta"), Mapping) else {}
    cfg = validate_ebook_config(
        source.get("config") if isinstance(source.get("config"), Mapping) else {}
    )
    prov = str(meta.get("provider") or cfg.get("provider") or "local")

    if candidate.get("content_ready") and existing:
        return {
            "full_text": existing,
            "content_origin": f"ebook_{prov}",
            "chapter_label": clean_inline_field(
                candidate.get("chapter_label") or "", max_chars=120
            ),
            "total_chapters": candidate.get("total_chapters"),
        }

    target_idx = int(candidate.get("chapter_index") or 0)
    chunk_chars = min(4000, int(cfg.get("chunk_chars", 3200)))

    if prov == "wikisource":
        lang = str(meta.get("language") or cfg.get("language") or "zh")
        explicit_subpage = str(meta.get("subpage") or "").strip()
        base_page = explicit_subpage or str(cfg.get("page") or "").strip()
        resp = await safe_http_get(
            f"https://{lang}.wikisource.org/w/api.php",
            params={
                "action": "parse",
                "page": base_page,
                "prop": "text|wikitext|displaytitle",
                "redirects": "1",
                "format": "json",
                "utf8": "1",
            },
            timeout=timeout,
            max_bytes=4 * 1024 * 1024,
            allow_private=False,
        )
        parse_obj = (json.loads(resp.text()) or {}).get("parse") or {}
        _, _, chapters, subpages = _extract_wikisource_parse(
            parse_obj,
            base_page=base_page,
            chunk_chars=chunk_chars,
        )

        if not explicit_subpage and subpages and not chapters:
            sub_idx = max(0, min(target_idx, len(subpages) - 1))
            chosen = subpages[sub_idx]
            sub_resp = await safe_http_get(
                f"https://{lang}.wikisource.org/w/api.php",
                params={
                    "action": "parse",
                    "page": chosen["subpage"],
                    "prop": "text|wikitext|displaytitle",
                    "redirects": "1",
                    "format": "json",
                    "utf8": "1",
                },
                timeout=timeout,
                max_bytes=4 * 1024 * 1024,
                allow_private=False,
            )
            sub_parse = (json.loads(sub_resp.text()) or {}).get("parse") or {}
            _, _, sub_chapters, _ = _extract_wikisource_parse(
                sub_parse,
                base_page=chosen["subpage"],
                chunk_chars=chunk_chars,
            )
            sub_text = (
                "\n\n".join(c["text"] for c in sub_chapters)
                if sub_chapters
                else clean_external_text(candidate.get("summary") or "", max_chars=4000)
            )
            return {
                "full_text": clean_external_text(sub_text, max_chars=4000),
                "content_origin": "ebook_wikisource",
                "chapter_label": clean_inline_field(chosen["label"], max_chars=120),
                "total_chapters": len(subpages),
            }

        if chapters:
            ch_idx = (
                0 if explicit_subpage else max(0, min(target_idx, len(chapters) - 1))
            )
            ch = chapters[ch_idx]
            ch_text = ch["text"]
            ch_label = clean_inline_field(
                (candidate.get("chapter_label") if explicit_subpage else ch["label"])
                or ch["label"]
                or base_page,
                max_chars=120,
            )
            total_ch = (
                (candidate.get("total_chapters") or len(chapters))
                if explicit_subpage
                else len(chapters)
            )
        else:
            ch_text = clean_external_text(
                candidate.get("summary") or "", max_chars=4000
            )
            ch_label = clean_inline_field(
                candidate.get("chapter_label") or base_page, max_chars=120
            )
            total_ch = candidate.get("total_chapters")

        return {
            "full_text": clean_external_text(ch_text, max_chars=4000),
            "content_origin": "ebook_wikisource",
            "chapter_label": ch_label,
            "total_chapters": total_ch,
        }

    if prov == "gutendex":
        _, _, _, _, chapters = await _load_gutendex_book(cfg, timeout=timeout)
        if chapters:
            idx = max(0, min(target_idx, len(chapters) - 1))
            ch = chapters[idx]
            return {
                "full_text": clean_external_text(ch["text"], max_chars=4000),
                "content_origin": "ebook_gutendex",
                "chapter_label": ch["label"],
                "total_chapters": len(chapters),
            }

    if prov == "opds":
        allow_private = bool(cfg.get("allow_private_opds", False))
        acq_url = str(
            meta.get("acquisition_url") or candidate.get("raw_url") or ""
        ).strip()
        if not acq_url:
            raise ValueError("OPDS 条目缺少下载链接 acquisition_url")
        acq_url = validate_url_sync(
            acq_url, allow_private=allow_private, require_dns=False
        )
        headers, secrets = _opds_auth_headers(cfg)
        from .ssrf import _origin_tuple

        origin = _origin_tuple(acq_url)
        allowed = {_origin_tuple(cfg["url"])}
        for extra in cfg.get("credential_origins", []):
            allowed.add(_origin_tuple(extra))
        if origin not in allowed:
            headers = {}
        resp = await safe_http_get(
            acq_url,
            headers=headers,
            timeout=timeout,
            max_bytes=MAX_RAW_EBOOK_BYTES,
            allow_private=allow_private,
            secrets_to_redact=secrets,
        )
        acq_type = (
            resp.header("content-type") or str(meta.get("acquisition_type") or "")
        ).lower()
        book_title = str(
            meta.get("book_title") or candidate.get("title") or "opds_book"
        )
        if (
            "epub" in acq_type
            or acq_url.lower().endswith(".epub")
            or resp.body[:4] == b"PK\x03\x04"
        ):
            parsed = parse_local_ebook(
                f"{book_title}.epub", resp.body, chunk_chars=chunk_chars
            )
            origin = "opds_epub"
        elif "html" in acq_type:
            art = extract_article_from_html(
                resp.text(), max_chars=max(chunk_chars * 10, 40000)
            )
            parsed = parse_local_ebook(
                f"{book_title}.txt",
                art["text"].encode("utf-8"),
                chunk_chars=chunk_chars,
            )
            origin = "opds_html"
        else:
            parsed = parse_local_ebook(
                f"{book_title}.txt", resp.body, chunk_chars=chunk_chars
            )
            origin = "opds_txt"

        chapters = parsed["chapters"]
        idx = max(0, min(target_idx, len(chapters) - 1))
        ch = chapters[idx]
        return {
            "full_text": clean_external_text(ch["text"], max_chars=4000),
            "content_origin": origin,
            "chapter_label": ch["label"],
            "total_chapters": len(chapters),
        }

    return {
        "full_text": existing
        or clean_external_text(candidate.get("summary") or "", max_chars=4000),
        "content_origin": f"ebook_{prov}",
        "chapter_label": clean_inline_field(
            candidate.get("chapter_label") or "", max_chars=120
        ),
        "total_chapters": candidate.get("total_chapters"),
    }
