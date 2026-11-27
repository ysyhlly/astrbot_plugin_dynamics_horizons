"""Wave 1 regression tests for Dynamics Horizons.

Covers:
1. Held proposals visibility in default pending queue, dedicated `held` filter, and badge counts.
2. Non-blocking `poll_loop` / `poll_all_due_sources` event-loop yielding, timeout guards, and `to_thread` offloading.
3. Single Bilibili video URL clipping (`bilibili.com/video/BV...`, `av...`, `b23.tv/...`) with SSRF guard
   and passthrough to the existing subtitle / AI summary / multimodal video pipeline.
4. Series scope write contract (`set_series_scope_enabled` with optimistic lock), `activate_series_runtime`,
   and extended `get_series_shared_config` inheritance (`light_analysis_provider`, `vision_provider`, `persona_id`).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_dynamics_horizons.adapters import (
    SSRFBlockedError,
    build_manual_candidate,
    fetch_item_content,
    poll_source,
)
from astrbot_plugin_dynamics_horizons.adapters import bilibili as bili_mod
from astrbot_plugin_dynamics_horizons.adapters import feed as feed_mod
from astrbot_plugin_dynamics_horizons.adapters.ssrf import (
    HttpResponse,
    set_dns_resolver_for_testing,
    set_http_transport_for_testing,
)
from astrbot_plugin_dynamics_horizons.core.contract import SHANGHAI_TZ, make_identity
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.main import Main

IDENT = make_identity("rin", "rin-main")
NOW_TS = datetime(2026, 11, 27, 10, 0, 0, tzinfo=SHANGHAI_TZ).timestamp()


@pytest.fixture(autouse=True)
def _reset_http_hooks():
    set_dns_resolver_for_testing(lambda _host: ["93.184.216.34"])
    set_http_transport_for_testing(None)
    yield
    set_http_transport_for_testing(None)
    set_dns_resolver_for_testing(None)


class _SaveableConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0

    def save_config(self) -> None:
        self.save_calls += 1


def _make_plugin(tmp_path, *, config=None, ctx=None, now: float = NOW_TS) -> Main:
    context = ctx or SimpleNamespace(get_all_stars=lambda: {})
    cfg = _SaveableConfig(
        {
            "enabled": True,
            "sessions": ["qq:GroupMessage:10001"],
            "proactive_enabled": True,
            "interest_half_life_days": 6,
            **(config or {}),
        }
    )
    plugin = Main(context, cfg)
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    engine = HorizonsEngine(
        store, cfg, lambda *a, **kw: {}, context=context, clock=lambda: now
    )
    plugin.engine = engine
    plugin.ready = True
    plugin.closed = False
    return plugin


# ---------------------------------------------------------------------------
# 1. Held Proposals Queue & Badge Regression
# ---------------------------------------------------------------------------


def test_held_proposals_included_in_pending_queue_and_counts(tmp_path):
    plugin = _make_plugin(tmp_path)
    store = plugin.engine.store
    try:
        p_pending = store.create_proposal(
            identity=IDENT,
            source_kind="feed",
            title="待审普通提案",
            digest="摘要一",
            reflection="随感一",
            now=NOW_TS - 300,
        )
        p_held = store.create_proposal(
            identity=IDENT,
            source_kind="bilibili",
            title="需人工复核提案",
            digest="摘要二",
            reflection="随感二",
            now=NOW_TS - 200,
        )
        store.record_review_verdict(
            p_held["proposal_id"],
            state="held",
            verdict="hold",
            reason="存在边界疑点需人工确认",
            risks=["raw_url_detected", "观点表述需人工复核"],
            reviewed_by="llm",
            now=NOW_TS - 150,
        )
        p_rejected = store.create_proposal(
            identity=IDENT,
            source_kind="manual",
            title="已拒绝提案",
            digest="摘要三",
            reflection="随感三",
            now=NOW_TS - 100,
        )
        store.record_review_verdict(
            p_rejected["proposal_id"],
            state="rejected",
            verdict="reject",
            reason="拒绝",
            risks=["spam:promo_spam"],
            reviewed_by="guardrail",
            now=NOW_TS - 90,
        )

        # Default pending_review & pending views must include both pending_review and held
        for q_state in ("pending_review", "pending"):
            res = store.list_proposals(identity=IDENT, state=q_state)
            ids = {it["proposal_id"] for it in res["items"]}
            assert ids == {p_pending["proposal_id"], p_held["proposal_id"]}
            assert res["total"] == 2

        # Dedicated held filter returns ONLY held proposals with review_risks intact
        held_only = store.list_proposals(identity=IDENT, state="held")
        assert held_only["total"] == 1
        assert held_only["items"][0]["proposal_id"] == p_held["proposal_id"]
        assert held_only["items"][0]["review_risks"] == [
            "raw_url_detected",
            "观点表述需人工复核",
        ]

        # Summary counts & series badge include held proposals
        counts = store.summary_counts(IDENT, now=NOW_TS)
        assert counts["pending_reviews"] == 2
        assert counts["held_reviews"] == 1

        plugin.engine.refresh_projection_sync()
        summary = plugin.get_series_summary()
        assert summary["badge"]["count"] == 2
        assert summary["badge"]["tone"] == "warm"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. Non-Blocking poll_loop & to_thread Offloading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_feed_offloads_parsing_to_thread_without_blocking_loop(
    monkeypatch,
):
    import time as _time

    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Tech Feed</title>
      <item><title>Async Article</title><link>https://example.org/a1</link>
      <description>Long enough summary text for feed testing.</description></item>
    </channel></rss>"""

    async def fake_transport(url: str, headers, timeout: float):
        return HttpResponse(
            status=200,
            body=rss_xml.encode("utf-8"),
            headers={"Content-Type": "application/rss+xml; charset=utf-8"},
            url=url,
        )

    set_http_transport_for_testing(fake_transport)

    orig_parse = feed_mod._parse_xml_feed

    def slow_parse(*args, **kwargs):
        _time.sleep(0.12)
        return orig_parse(*args, **kwargs)

    monkeypatch.setattr(feed_mod, "_parse_xml_feed", slow_parse)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.015)
            ticks += 1

    hb_task = asyncio.create_task(heartbeat())
    res = await poll_source(
        {
            "source_id": "src-rss-1",
            "source_kind": "feed",
            "identity": IDENT,
            "name": "Tech Feed",
            "config": {"url": "https://example.org/rss.xml"},
        },
        {},
    )
    await hb_task

    assert res["outcome"] == "success"
    assert len(res["items"]) == 1
    assert ticks >= 4  # Event loop kept ticking during blocking XML parse


@pytest.mark.asyncio
async def test_poll_all_due_sources_timeouts_hanging_source_and_continues(
    tmp_path, monkeypatch
):
    plugin = _make_plugin(
        tmp_path,
        config={"poll_source_timeout_seconds": 5.0},
    )
    store = plugin.engine.store
    try:
        s1 = store.create_source(
            identity=IDENT,
            source_kind="feed",
            name="Slow Feed",
            config={"url": "https://slow.example.org/rss.xml"},
            enabled=True,
            now=NOW_TS - 1000,
        )
        s2 = store.create_source(
            identity=IDENT,
            source_kind="feed",
            name="Fast Feed",
            config={"url": "https://fast.example.org/rss.xml"},
            enabled=True,
            now=NOW_TS - 1000,
        )

        # Tighten wait_for timeout specifically via small override in poll_one_source test
        async def fake_poll_one(source_id: str, *, force: bool = False, expected_identity: str = ""):
            if source_id == s1["source_id"]:
                await asyncio.sleep(5.0)
                return {"polled": True, "added": 0, "outcome": "success"}
            return {"polled": True, "added": 2, "outcome": "success"}

        orig_wait_for = asyncio.wait_for

        async def fast_wait_for(aw, timeout=None):
            return await orig_wait_for(aw, timeout=min(float(timeout or 1.0), 0.08))

        monkeypatch.setattr(plugin.engine, "poll_one_source", fake_poll_one)
        monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

        out = await plugin.engine.poll_all_due_sources(force=True)
        assert out["failed_sources"] == 1
        assert out["polled_sources"] == 1
        assert out["added_candidates"] == 2
        assert f"poll_timeout:{s1['source_id']}" in plugin.engine.last_error
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. Single Bilibili Video Clip Parsing + SSRF + Video Pipeline Passthrough
# ---------------------------------------------------------------------------


def test_parse_bilibili_video_ref_variants():
    bv = bili_mod.parse_bilibili_video_ref(
        "https://www.bilibili.com/video/BV1xx411c7mD/?p=2&spm_id_from=333"
    )
    assert bv is not None
    assert bv["bvid"] == "BV1xx411c7mD"
    assert bv["page"] == 2
    assert bv["needs_redirect"] is False

    av = bili_mod.parse_bilibili_video_ref("https://m.bilibili.com/video/av170001")
    assert av is not None
    assert av["aid"] == 170001
    assert av["bvid"] == ""
    assert av["needs_redirect"] is False

    short = bili_mod.parse_bilibili_video_ref("https://b23.tv/AbCdEfG")
    assert short is not None
    assert short["needs_redirect"] is True

    # Non-video Bilibili article links must not be misclassified as video
    assert bili_mod.parse_bilibili_video_ref("https://www.bilibili.com/read/cv123456") is None
    assert bili_mod.parse_bilibili_video_ref("https://example.org/video/BV1xx411c7mD") is None


@pytest.mark.asyncio
async def test_manual_clip_resolves_b23_shortlink_and_routes_to_video_pipeline(
    monkeypatch,
):
    # Mock b23.tv -> bilibili.com/video/BV... redirect
    async def fake_transport(url: str, headers, timeout: float):
        if url.startswith("https://b23.tv/abc123"):
            return HttpResponse(
                status=302,
                body=b"",
                headers={"Location": "https://www.bilibili.com/video/BV1xx411c7mD?p=1"},
                url=url,
            )
        if url.startswith("https://b23.tv/ssrf-evil"):
            return HttpResponse(
                status=302,
                body=b"",
                headers={"Location": "http://127.0.0.1:8080/video/BV1xx411c7mD"},
                url=url,
            )
        return HttpResponse(status=404, body=b"", headers={}, url=url)

    set_http_transport_for_testing(fake_transport)

    class FakeVideo:
        def __init__(self, bvid=None, aid=None, credential=None):
            self.bvid = bvid
            self.aid = aid
            self.credential = credential

        async def get_info(self):
            return {
                "bvid": self.bvid or "BV1xx411c7mD",
                "aid": self.aid or 170001,
                "cid": 998877,
                "title": "十分钟看懂斗拱木构力学",
                "desc": "深入浅出拆解唐代殿堂铺作层。",
                "duration": 420,
                "pubdate": int(NOW_TS - 3600),
                "owner": {"name": "古建观察员", "mid": 556677},
                "pages": [{"cid": 998877, "page": 1}],
            }

    monkeypatch.setattr(bili_mod.video, "Video", FakeVideo)

    manual_src = {
        "source_id": "src-manual-1",
        "source_kind": "manual",
        "identity": IDENT,
        "name": "管理员手动投喂",
        "config": {},
    }

    # 1. b23.tv short link resolves cleanly into a video candidate
    cand = await build_manual_candidate(manual_src, url="https://b23.tv/abc123")
    assert cand["bvid"] == "BV1xx411c7mD"
    assert cand["title"] == "十分钟看懂斗拱木构力学"
    assert cand["creator"] == "古建观察员"
    assert cand["content_ready"] is False
    assert cand["meta"]["bili_video_clip"] is True
    assert cand["meta"]["cid"] == 998877
    assert HorizonsEngine._is_bilibili_video_candidate(cand) is True

    # 2. av... link converts to canonical BV candidate
    cand_av = await build_manual_candidate(
        manual_src, url="https://www.bilibili.com/video/av170001"
    )
    assert cand_av["bvid"] == "BV1xx411c7mD"
    assert cand_av["meta"]["aid"] == 170001

    # 3. SSRF redirect from b23.tv to loopback MUST be blocked!
    with pytest.raises(SSRFBlockedError):
        await build_manual_candidate(manual_src, url="https://b23.tv/ssrf-evil")

    # 4. Stage-2 fetch_item_content routes manual Bilibili clip straight into fetch_bilibili_content
    captured: dict[str, Any] = {}

    async def fake_fetch_bili(src, candidate, **kwargs):
        captured["bvid"] = candidate.get("bvid")
        captured["media_options"] = kwargs.get("media_options")
        captured["fallback_cookies"] = kwargs.get("fallback_cookies")
        return {
            "full_text": "[00:05] 斗拱通过层层挑出分担屋檐荷载。",
            "content_origin": "bilibili_subtitle",
            "chapter_label": "",
            "total_chapters": None,
            "media_bundle": {
                "watch_mode": "subtitle_only",
                "content_origin": "bilibili_subtitle",
                "has_visual": False,
                "has_subtitle": True,
                "has_audio_transcript": False,
                "cleanup": lambda: None,
            },
        }

    monkeypatch.setattr(bili_mod, "fetch_bilibili_content", fake_fetch_bili)
    fetched = await fetch_item_content(
        manual_src,
        cand,
        fallback_cookies=["SESSDATA=readonly_tok"],
        media_options={"visual_enabled": True, "max_frames": 8},
    )
    assert captured["bvid"] == "BV1xx411c7mD"
    assert captured["media_options"]["visual_enabled"] is True
    assert captured["fallback_cookies"] == ["SESSDATA=readonly_tok"]
    assert fetched["content_origin"] == "bilibili_subtitle"
    assert "斗拱" in fetched["full_text"]


# ---------------------------------------------------------------------------
# 4. Series Scope Write Contract, activate_series_runtime & Shared Config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_series_scope_enabled_optimistic_lock_and_activate_runtime(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        config={"enabled": False, "sessions": ["qq:GroupMessage:10001"]},
    )
    try:
        # Activate runtime
        act = plugin.activate_series_runtime()
        assert act["ok"] is True
        assert act["enabled"] is True
        assert plugin.config["enabled"] is True
        assert plugin.config.save_calls >= 1

        st0 = plugin.get_series_scope_status("qq:GroupMessage:10001")
        assert st0["enabled"] is True
        assert st0["writable"] is True
        assert st0["supported"] is True
        v0 = st0["version"]

        # Stale expected_version must fail with version_conflict
        conflict = await plugin.set_series_scope_enabled(
            "qq:GroupMessage:10001", False, expected_version="stale-version"
        )
        assert conflict["ok"] is False
        assert conflict["reason"] == "version_conflict"
        assert conflict["conflict"] is True
        assert conflict["version"] == v0

        # Disable the ONLY group in allowlist -> must NOT fall back to open-to-all!
        res_off = await plugin.set_series_scope_enabled(
            "qq:GroupMessage:10001", False, expected_version=v0
        )
        assert res_off["ok"] is True
        assert res_off["enabled"] is False
        assert res_off["version"] != v0
        assert plugin.get_series_scope_status("qq:GroupMessage:10001")["enabled"] is False
        assert plugin.get_series_scope_status("qq:GroupMessage:20002")["enabled"] is False

        # Re-enable with fresh version
        v1 = res_off["version"]
        res_on = await plugin.set_series_scope_enabled(
            "qq:GroupMessage:10001", True, expected_version=v1
        )
        assert res_on["ok"] is True
        assert res_on["enabled"] is True
        assert plugin.get_series_scope_status("qq:GroupMessage:10001")["enabled"] is True
        assert plugin.get_series_scope_status("qq:GroupMessage:20002")["enabled"] is False

        # Enable a second group into allowlist
        res_add = await plugin.set_series_scope_enabled(
            "qq:GroupMessage:20002", True, expected_version=res_on["version"]
        )
        assert res_add["ok"] is True
        assert res_add["enabled"] is True
        assert "qq:GroupMessage:20002" in plugin.config["sessions"]
    finally:
        plugin.engine.store.close()


@pytest.mark.asyncio
async def test_set_series_scope_enabled_in_open_mode_disables_single_umo(tmp_path):
    plugin = _make_plugin(tmp_path, config={"enabled": True, "sessions": []})
    try:
        st_a = plugin.get_series_scope_status("qq:GroupMessage:10001")
        assert st_a["enabled"] is True
        assert st_a["mode"] == "open"

        off = await plugin.set_series_scope_enabled(
            "qq:GroupMessage:10001", False, expected_version=st_a["version"]
        )
        assert off["ok"] is True
        assert off["enabled"] is False
        assert plugin.get_series_scope_status("qq:GroupMessage:10001")["enabled"] is False
        # Other groups stay open
        assert plugin.get_series_scope_status("qq:GroupMessage:20002")["enabled"] is True

        on = await plugin.set_series_scope_enabled(
            "qq:GroupMessage:10001", True, expected_version=off["version"]
        )
        assert on["ok"] is True
        assert on["enabled"] is True
    finally:
        plugin.engine.store.close()


def test_extended_series_shared_config_inheritance_models_and_persona(tmp_path):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": "series-reply",
                "decision_provider": "series-decision",
                "light_analysis_provider": "series-light",
                "vision_provider": "series-vision",
                "persona_id": "anan",
                "takeover_groups": ["qq:GroupMessage:777"],
            }

    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=True,
                star_cls=ChatOwner(),
            )
        },
        persona_manager=SimpleNamespace(
            personas=[
                {"persona_id": "other", "prompt": "其他角色设定"},
                {"persona_id": "anan", "prompt": "安安温柔体贴爱读书。"},
            ]
        ),
    )
    plugin = _make_plugin(
        tmp_path,
        config={
            "analysis_provider": "",
            "video_provider": "",
            "review_provider": "",
            "sessions": [],
        },
        ctx=ctx,
    )
    try:
        eng = plugin.engine
        assert eng.effective_provider("analysis_provider") == "series-light"
        assert eng.effective_provider("video_provider") == "series-vision"
        assert eng.effective_provider("review_provider") == "series-decision"
        assert plugin._effective_provider("analysis_provider") == "series-light"
        assert plugin._effective_provider("video_provider") == "series-vision"

        # Shared persona_id is promoted to default first persona
        personas = eng.world_bridge.discover_personas(eng.store, eng.sessions)
        assert personas[0]["persona_id"] == "anan"
    finally:
        plugin.engine.store.close()
