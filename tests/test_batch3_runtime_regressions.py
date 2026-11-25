"""Batch 3 runtime, hot-path offload, content verification & timeout regressions for Dynamics Horizons.

Covers:
- P1-1: list_interests() returns evidence_log_ids so member corrections suppress derived staged_interests.
- P1-3: ingest_dynamics_event() offloads SQLite write & projection rebuild off the event loop;
  OPDS/Gutendex EPUB parsing runs in worker thread and reuses URL-keyed local file cache across chapters.
- P1-4: _compact_review_source_evidence() preserves bounded source text excerpt for Stage 3 verification.
- P1-5: download_media_to_file() enforces a total deadline across chunks/backup URLs/retries.
- P2: summary_counts() excludes archived candidates & revoked logs; update_intent() persists
  cancelled/expired states to SQLite; _conf_schema.json includes terminal_retention_days & bilibili_poll_jitter_max.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_dynamics_horizons.adapters import ebook as ebook_mod
from astrbot_plugin_dynamics_horizons.adapters import video_media as video_mod
from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.projection import ProjectionCache
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.tests.test_core_engine import _make_plugin, _ts

IDENT = make_identity("alice")
UMO = "qq:GroupMessage:1001"


# ---------------------------------------------------------------------------
# P1-1: list_interests() includes evidence_log_ids -> correction suppresses interest
# ---------------------------------------------------------------------------


def test_p1_1_list_interests_returns_evidence_log_ids_and_suppresses_on_correction(tmp_path: Path):
    now = _ts(14, 0)
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    try:
        prop = store.create_proposal(
            identity=IDENT,
            source_kind="feed",
            title="茶具科普",
            digest="建盏兔毫纹烧制工艺",
            reflection="对茶具釉面很有兴趣",
            staged_interest={"topic": "宋代点茶", "summary": "对宋代点茶和建盏有兴趣"},
            now=now,
        )
        committed = store.approve_proposal_atomic(prop["proposal_id"], now=now + 10)
        log_id = committed["log"]["log_id"]

        interests = store.list_interests(IDENT, active_only=True, now=now + 20)
        assert len(interests) == 1
        assert interests[0]["evidence_log_ids"] == [log_id]

        # Record a member correction against this reading log -> projection must suppress the interest
        with store.lock, store.db:
            store.db.execute(
                "INSERT INTO corrections(correction_id,identity,umo,member_id,log_id,statement,status,created_at) "
                "VALUES('cor_1',?,?, 'member_1', ?, '这条信息有误', 'pending', ?)",
                (IDENT, UMO, log_id, now + 30),
            )
        proj = ProjectionCache()
        proj.update_snapshot(store.build_projection_snapshot(now=now + 40))

        # Corrected member does NOT receive the derived staged interest
        ctx_corrected = proj.query(
            enabled=True,
            ready=True,
            allowed_sessions=[UMO],
            scope=UMO,
            persona="alice",
            member="member_1",
            query="聊聊宋代点茶吧",
            character_scope={"persona_id": "alice", "instance_id": "alice"},
            now=now + 40,
        )
        assert ctx_corrected["matched"] is False
        assert "宋代点茶" not in (ctx_corrected.get("decision_slot") or {}).get("topics", [])

        # Another member in the same group still receives it normally
        ctx_other = proj.query(
            enabled=True,
            ready=True,
            allowed_sessions=[UMO],
            scope=UMO,
            persona="alice",
            member="member_2",
            query="聊聊宋代点茶吧",
            character_scope={"persona_id": "alice", "instance_id": "alice"},
            now=now + 40,
        )
        assert ctx_other["matched"] is True
        assert "宋代点茶" in (ctx_other.get("decision_slot") or {}).get("topics", [])
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P1-3: Hot-path ingest offload + EPUB thread offload & URL cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_3_ingest_dynamics_event_offloads_sqlite_and_projection_off_loop(
    tmp_path: Path, monkeypatch
):
    plugin = await _make_plugin(
        tmp_path,
        providers={"prov_a": lambda *_: {}, "prov_r": lambda *_: {}},
    )
    loop_tid = threading.get_ident()
    enqueue_threads: list[int] = []
    refresh_threads: list[int] = []

    from astrbot_plugin_dynamics_horizons.core import feedback as fb_mod

    orig_enqueue = fb_mod.enqueue

    def spy_enqueue(eng, ev):
        enqueue_threads.append(threading.get_ident())
        return orig_enqueue(eng, ev)

    orig_refresh = plugin.engine.refresh_projection_sync

    def spy_refresh():
        refresh_threads.append(threading.get_ident())
        return orig_refresh()

    monkeypatch.setattr(fb_mod, "enqueue", spy_enqueue)
    monkeypatch.setattr(plugin.engine, "refresh_projection_sync", spy_refresh)

    try:
        res = plugin.ingest_dynamics_event(
            {
                "type": "message.received",
                "umo": UMO,
                "character_scope": {"persona_id": "alice", "instance_id": "alice"},
                "member": "u_offload",
                "text": "别再向我分享茶具",
                "event_id": "ev_offload_1",
                "occurred_at": _ts(14, 0),
            }
        )
        assert res["accepted"] is True
        assert res.get("async_offloaded") is True
        # Must NOT have run synchronously before returning
        assert enqueue_threads == []
        assert refresh_threads == []

        await plugin.engine.flush_pending_ingests()
        refresh_task = getattr(plugin.engine, "_projection_refresh_task", None)
        if refresh_task is not None:
            await refresh_task

        assert len(enqueue_threads) == 1 and enqueue_threads[0] != loop_tid
        assert len(refresh_threads) == 1 and refresh_threads[0] != loop_tid
    finally:
        await plugin.terminate()


def _build_minimal_epub_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>测试书</dc:title><dc:creator>作者</dc:creator></metadata>'
            '<manifest>'
            '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest>'
            '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>',
        )
        zf.writestr("OEBPS/ch1.xhtml", "<html><body><h1>第一章</h1><p>" + ("第一章节正文。" * 30) + "</p></body></html>")
        zf.writestr("OEBPS/ch2.xhtml", "<html><body><h1>第二章</h1><p>" + ("第二章节正文。" * 30) + "</p></body></html>")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_p1_3_opds_epub_parses_in_worker_thread_and_caches_remote_file(
    tmp_path: Path, monkeypatch
):
    epub_bytes = _build_minimal_epub_bytes()
    loop_tid = threading.get_ident()
    parse_threads: list[int] = []
    http_calls = {"n": 0}

    monkeypatch.setattr(ebook_mod, "_REMOTE_EBOOK_CACHE_DIR", tmp_path / "ebook_cache")
    orig_parse = ebook_mod.parse_local_ebook

    def spy_parse(*args, **kwargs):
        parse_threads.append(threading.get_ident())
        return orig_parse(*args, **kwargs)

    async def fake_validate(url, **kwargs):
        return url

    async def fake_http_get(url, **kwargs):
        http_calls["n"] += 1
        return SimpleNamespace(
            body=epub_bytes,
            header=lambda name, default="": "application/epub+zip" if name.lower() == "content-type" else default,
            text=lambda: "",
        )

    monkeypatch.setattr(ebook_mod, "parse_local_ebook", spy_parse)
    monkeypatch.setattr(ebook_mod, "validate_url_async", fake_validate)
    monkeypatch.setattr(ebook_mod, "safe_http_get", fake_http_get)

    source = {
        "source_id": "src_opds",
        "config": {"provider": "opds", "url": "https://books.example.org/opds"},
    }
    base_cand = {
        "title": "测试书",
        "raw_url": "https://books.example.org/download/book1.epub",
        "meta": {
            "provider": "opds",
            "acquisition_url": "https://books.example.org/download/book1.epub",
            "acquisition_type": "application/epub+zip",
            "book_title": "测试书",
        },
    }

    ch0 = await ebook_mod.fetch_ebook_content(source, {**base_cand, "chapter_index": 0})
    ch1 = await ebook_mod.fetch_ebook_content(source, {**base_cand, "chapter_index": 1})

    assert "第一章节正文" in ch0["full_text"]
    assert "第二章节正文" in ch1["full_text"]
    assert http_calls["n"] == 1  # Second chapter hit local disk cache instead of re-downloading!
    assert len(parse_threads) == 2 and all(tid != loop_tid for tid in parse_threads)


# ---------------------------------------------------------------------------
# P1-4: _compact_review_source_evidence keeps bounded source text excerpt
# ---------------------------------------------------------------------------


def test_p1_4_compact_review_source_evidence_retains_bounded_text_excerpt():
    long_text = "真实新闻正文内容。" * 400 + "INJECTION_MARKER"
    compact = HorizonsEngine._compact_review_source_evidence(
        {
            "content_origin": "feed_article",
            "text": long_text,
            "content_hash": "abc123",
        },
        max_text_chars=500,
    )
    assert compact["text"] == long_text[:500]
    assert compact["text_chars"] == len(long_text)
    assert compact["text_truncated"] is True

    short_compact = HorizonsEngine._compact_review_source_evidence(
        {"text": "简短原文内容"}, max_text_chars=500
    )
    assert short_compact["text"] == "简短原文内容"
    assert short_compact["text_truncated"] is False


# ---------------------------------------------------------------------------
# P1-5: download_media_to_file total deadline prevents Semaphore(1) starvation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_5_download_media_to_file_enforces_total_deadline(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    async def slow_stream(url, part_path, *, max_bytes, timeout, cookie_header="", secrets=()):
        calls["n"] += 1
        await asyncio.sleep(5.0)
        return 100

    monkeypatch.setattr(video_mod, "_stream_single_url_to_file", slow_stream)

    t0 = time.monotonic()
    with pytest.raises(video_mod.MediaDownloadError, match="总超时"):
        await video_mod.download_media_to_file(
            [
                "https://cdn1.example.com/a.m4s",
                "https://cdn2.example.com/a.m4s",
                "https://cdn3.example.com/a.m4s",
            ],
            tmp_path / "out.m4s",
            max_bytes=1024 * 1024,
            timeout=10.0,
            total_timeout=0.25,
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.2
    assert calls["n"] == 1  # Did not stack retries across all 3 backup URLs × 2 attempts


# ---------------------------------------------------------------------------
# P2: summary_counts exclusions, update_intent persistence & schema fields
# ---------------------------------------------------------------------------


def test_p2_summary_counts_update_intent_persistence_and_conf_schema(tmp_path: Path):
    now = _ts(14, 0)
    store = HorizonsStore(tmp_path / "horizons_p2.sqlite3")
    try:
        src = store.create_source(
            identity=IDENT,
            source_kind="feed",
            name="feed",
            config={"url": "https://example.org/rss"},
            now=now,
        )
        store.insert_candidates(
            [
                {
                    "external_id": "c_active",
                    "source_id": src["source_id"],
                    "identity": IDENT,
                    "source_kind": "feed",
                    "title": "未归档",
                    "summary": "s",
                },
                {
                    "external_id": "c_archived",
                    "source_id": src["source_id"],
                    "identity": IDENT,
                    "source_kind": "feed",
                    "title": "已归档",
                    "summary": "s",
                },
            ],
            source_id=src["source_id"],
            source_kind="feed",
            identity=IDENT,
            now=now,
        )
        with store.lock, store.db:
            store.db.execute(
                "UPDATE candidate_items SET archived=1 WHERE external_id='c_archived'"
            )

        p1 = store.create_proposal(
            identity=IDENT,
            source_kind="feed",
            title="保留记录",
            digest="d1",
            reflection="r1",
            now=now,
        )
        store.approve_proposal_atomic(p1["proposal_id"], now=now + 1)

        p2 = store.create_proposal(
            identity=IDENT,
            source_kind="feed",
            title="已撤销记录",
            digest="d2",
            reflection="r2",
            now=now + 2,
        )
        c2 = store.approve_proposal_atomic(p2["proposal_id"], now=now + 3)
        store.revoke_proposal(p2["proposal_id"], "admin", "撤销", now + 4)

        counts = store.summary_counts(IDENT, now=now + 5)
        assert counts["candidates_unread"] == 1
        assert counts["logs_total"] == 1
        assert counts["recent_title"] == "保留记录"

        # update_intent on invalidated evidence must persist status='cancelled' in SQLite
        with store.lock, store.db:
            store.db.execute(
                "INSERT INTO horizon_intents(intent_id,identity,umo,recipient_user_id,topic,log_id,status,payload_json,not_before,expires_at,created_at,updated_at) "
                "VALUES('hi_invalid',?,?, 'u1', 't', ?, 'pending', '{\"status\":\"pending\"}', ?, ?, ?, ?)",
                (IDENT, UMO, c2["log"]["log_id"], now, now + 3600, now, now),
            )
        res = store.update_intent(
            "hi_invalid", "claimed", now=now + 10, proactive_sessions=[UMO]
        )
        assert res["status"] == "cancelled"
        assert res["changed"] is False
        row = store.db.execute(
            "SELECT status FROM horizon_intents WHERE intent_id='hi_invalid'"
        ).fetchone()
        assert row["status"] == "cancelled"
    finally:
        store.close()

    # Schema fields check
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "terminal_retention_days" in schema
    assert schema["terminal_retention_days"]["default"] == 30
    assert "bilibili_poll_jitter_max" in schema
    assert schema["bilibili_poll_jitter_max"]["default"] == 1.5
