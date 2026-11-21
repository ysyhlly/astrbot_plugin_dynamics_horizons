"""Unit tests for Dynamics Horizons v2.0 cross-plugin hooks and series_panels badge/pulse."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from astrbot_plugin_dynamics_horizons.core.contract import (
    PLUGIN_ID,
    SHANGHAI_TZ,
    make_identity,
    shanghai_day_bounds,
)
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.main import (
    Main,
    _accepts_kwarg,
    build_cache_anchor_contexts,
)
from astrbot_plugin_dynamics_horizons.series_panels import (
    CATALOG,
    invalidate_series_summary_cache,
    presence,
)

IDENT_A = make_identity("rin", "rin-main")
IDENT_B = make_identity("alice", "alice-study")
NOW_TS = datetime(2026, 11, 21, 14, 30, 0, tzinfo=SHANGHAI_TZ).timestamp()


def _make_plugin(tmp_path, *, config=None, now: float = NOW_TS) -> Main:
    ctx = SimpleNamespace(get_all_stars=lambda: {})
    cfg = {
        "enabled": True,
        "sessions": ["qq:GroupMessage:10001"],
        "proactive_enabled": True,
        "interest_half_life_days": 6,
        **(config or {}),
    }
    plugin = Main(ctx, cfg)
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    engine = HorizonsEngine(store, cfg, lambda *a, **kw: {}, context=ctx, clock=lambda: now)
    plugin.engine = engine
    plugin.ready = True
    plugin.closed = False
    return plugin


def _seed_sample_data(plugin: Main, now: float = NOW_TS) -> dict[str, str]:
    store = plugin.engine.store
    src = store.create_source(
        identity=IDENT_A,
        source_kind="bilibili",
        name="营造学社科普",
        config={"mode": "up", "uid": "946974", "cookie": "SESSDATA=super-secret-token"},
        enabled=True,
        now=now - 3600,
    )
    book = store.create_book_with_chapters(
        identity=IDENT_A,
        title="银河铁道之夜",
        creator="宫泽贤治",
        chapters=[
            {"label": "第一章", "text": "午后授课讲述银河的故事。"},
            {"label": "第二章", "text": "星光洒在铁轨上。"},
        ],
        now=now - 7200,
    )
    prop_ok = store.create_proposal(
        identity=IDENT_A,
        source_kind="bilibili",
        title="佛光寺东大殿斗拱结构",
        creator="古建观察员",
        raw_url="https://www.bilibili.com/video/BV1xx411c7mD",
        digest="拆解唐代殿堂木构斗拱传力关系。",
        reflection="木头互相咬合的力学很迷人。",
        staged_interest={
            "topic": "古建筑榫卯",
            "summary": "对不用铁钉的木构受力结构很着迷",
        },
        questions=[
            {
                "topic": "古建筑榫卯",
                "question": "你们逛古建时会抬头看斗拱吗？",
            }
        ],
        now=now - 1800,
    )
    approved = store.approve_proposal_atomic(
        prop_ok["proposal_id"],
        reviewed_by="llm",
        reason="ok",
        now=now - 1200,
    )
    prop_pending = store.create_proposal(
        identity=IDENT_A,
        source_kind="feed",
        title="冬季花草茶冲泡笔记",
        digest="介绍迷迭香与洋甘菊的配比。",
        reflection="想在冷天泡一杯暖暖的草本茶。",
        staged_interest={"topic": "冬季花草茶", "summary": "研究冬日热饮冲泡"},
        now=now - 600,
    )
    # Seed a second persona (IDENT_B) to verify strict persona/scope isolation
    prop_b = store.create_proposal(
        identity=IDENT_B,
        source_kind="manual",
        title="异世界炼金手记",
        digest="记录水晶蒸馏温度曲线。",
        reflection="炼金术讲究耐心控温。",
        staged_interest={"topic": "水晶炼金", "summary": "探索低温结晶规律"},
        now=now - 900,
    )
    store.approve_proposal_atomic(
        prop_b["proposal_id"],
        reviewed_by="llm",
        reason="ok",
        now=now - 800,
    )
    store.bind_scope("qq:GroupMessage:10001", IDENT_A, True, now)
    plugin.engine.refresh_projection_sync()
    return {
        "source_id": src["source_id"],
        "book_id": book["book_id"],
        "log_id": approved["log"]["log_id"],
        "pending_proposal_id": prop_pending["proposal_id"],
    }


def test_get_series_summary_states_and_standalone_safety(tmp_path):
    # 1. Uninitialized / standalone plugin never raises
    raw_plugin = Main(SimpleNamespace(), {})
    unready = raw_plugin.get_series_summary()
    assert unready["badge"]["count"] == 0
    assert unready["badge"]["tone"] == "muted"
    assert isinstance(unready["pulse"]["headline"], str)

    # 2. Ready with pending review -> warm badge
    plugin = _make_plugin(tmp_path)
    ids = _seed_sample_data(plugin)
    summary = plugin.get_series_summary()
    assert summary["badge"]["count"] == 1
    assert summary["badge"]["tone"] == "warm"
    assert "待审" in summary["badge"]["label"]
    assert summary["pulse"]["updated_at"].startswith("2026-11-21T")

    # 3. Approve pending proposal -> today's logs accent badge
    plugin.engine.store.approve_proposal_atomic(
        ids["pending_proposal_id"], reviewed_by="human:admin", now=NOW_TS - 300
    )
    plugin.engine.refresh_projection_sync()
    summary_after = plugin.get_series_summary()
    assert summary_after["badge"]["tone"] == "accent"
    assert summary_after["badge"]["count"] >= 2
    assert "新精读" in summary_after["badge"]["label"]

    # 4. Disabled plugin -> muted badge
    plugin.config["enabled"] = False
    disabled_summary = plugin.get_series_summary()
    assert disabled_summary["badge"]["tone"] == "muted"
    assert disabled_summary["badge"]["count"] == 0
    plugin.engine.store.close()


def test_series_search_items_coverage_isolation_and_secret_safety(tmp_path):
    plugin = _make_plugin(tmp_path)
    _seed_sample_data(plugin)

    # Search active interest
    res_int = plugin.series_search_items("榫卯", scope="qq:GroupMessage:10001", limit=10)
    assert any(item["kind"] == "interest" and "古建筑榫卯" in item["title"] for item in res_int)
    for item in res_int:
        assert set(item.keys()) == {
            "kind",
            "title",
            "subtitle",
            "plugin_id",
            "page",
            "deep_link",
        }
        assert item["plugin_id"] == PLUGIN_ID
        assert item["page"] == "horizons"

    # Search reading journal (URL-free)
    res_log = plugin.series_search_items("佛光寺", scope="qq:GroupMessage:10001", limit=10)
    assert any(item["kind"] == "reading_log" and "佛光寺" in item["title"] for item in res_log)
    assert all("http" not in item["subtitle"] for item in res_log)

    # Search bookshelf book
    res_book = plugin.series_search_items("银河铁道", scope="qq:GroupMessage:10001", limit=10)
    assert any(item["kind"] == "book" and "银河铁道之夜" in item["title"] for item in res_book)

    # Search source (must NEVER leak cookie/secret!)
    res_src = plugin.series_search_items("营造学社", scope="qq:GroupMessage:10001", limit=10)
    assert any(item["kind"] == "source" and "营造学社科普" in item["title"] for item in res_src)
    assert all("super-secret-token" not in str(item) for item in res_src)

    # Scope isolation: IDENT_B's alchemy note must not leak into IDENT_A's bound scope
    assert plugin.series_search_items("炼金", scope="qq:GroupMessage:10001", limit=10) == []
    # Unmanaged scope returns empty
    assert plugin.series_search_items("榫卯", scope="qq:GroupMessage:99999", limit=10) == []
    # Global search (empty scope) finds IDENT_B too
    res_global = plugin.series_search_items("炼金", scope="", limit=10)
    assert any("炼金" in item["title"] for item in res_global)

    # Blocked topic is excluded from search
    plugin.engine.store.block_topic("榫卯", IDENT_A, now=NOW_TS)
    assert plugin.series_search_items("榫卯", scope="qq:GroupMessage:10001", limit=10) == []

    plugin.engine.store.close()


def test_get_series_scope_status_contract(tmp_path):
    plugin = _make_plugin(tmp_path)
    _seed_sample_data(plugin)

    managed = plugin.get_series_scope_status("qq:GroupMessage:10001")
    assert set(managed.keys()) == {
        "enabled",
        "supported",
        "persona_id",
        "activity_today",
        "summary",
        "version",
    }
    assert managed["enabled"] is True
    assert managed["supported"] is False
    assert managed["persona_id"] == "rin"
    assert managed["activity_today"] >= 1
    assert "活跃兴趣" in managed["summary"]

    unmanaged = plugin.get_series_scope_status("qq:GroupMessage:99999")
    assert unmanaged["enabled"] is False
    assert unmanaged["persona_id"] == ""

    plugin.config["enabled"] = False
    disabled = plugin.get_series_scope_status("qq:GroupMessage:10001")
    assert disabled["enabled"] is False
    assert "停用" in disabled["summary"]

    plugin.engine.store.close()


def test_get_journal_events_contract_and_filtering(tmp_path):
    plugin = _make_plugin(tmp_path)
    _seed_sample_data(plugin)

    events = plugin.get_journal_events(
        persona="rin", scope="qq:GroupMessage:10001", date="today", limit=20
    )
    assert len(events) >= 2
    for ev in events:
        assert set(ev.keys()) == {
            "id",
            "category",
            "source_label",
            "tone",
            "occurred_at",
            "title",
            "subtitle",
            "target_plugin",
            "target_route",
            "target_id",
        }
        assert ev["category"] == "horizons"
        assert ev["source_label"] == "群间见闻"
        assert ev["target_plugin"] == PLUGIN_ID
        assert ev["target_route"] == "horizons"
        assert "http" not in ev["subtitle"]
        assert "炼金" not in ev["title"]

    tones = {ev["tone"] for ev in events}
    assert "accent" in tones
    assert "warm" in tones

    # Yesterday has no events
    assert (
        plugin.get_journal_events(
            persona="rin", scope="qq:GroupMessage:10001", date="yesterday", limit=20
        )
        == []
    )
    # Explicit matching date string returns today's events
    by_date = plugin.get_journal_events(
        persona="rin", scope="qq:GroupMessage:10001", date="2026-11-21", limit=20
    )
    assert len(by_date) == len(events)

    # Unmanaged scope returns []
    assert (
        plugin.get_journal_events(
            persona="rin", scope="qq:GroupMessage:99999", date="today", limit=20
        )
        == []
    )

    plugin.engine.store.close()


def test_series_panels_presence_badge_pulse_cache_and_fallback(tmp_path):
    invalidate_series_summary_cache()
    catalog_map = {row[0]: row[1] for row in CATALOG}
    assert catalog_map["astrbot_plugin_chat_dynamics"] == "对话主控"
    assert catalog_map["astrbot_plugin_group_chronicle"] == "群文化沿革"

    calls = {"count": 0}

    class GoodSibling:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_series_summary(self):
            calls["count"] += 1
            return {
                "badge": {"count": 3, "tone": "warm", "label": "3 项待审"},
                "pulse": {
                    "headline": "今日已审查 12 条",
                    "updated_at": "2026-11-21T14:20:00+08:00",
                },
            }

    class BrokenSibling:
        def get_series_summary(self):
            raise RuntimeError("boom")

    class AsyncMistakeSibling:
        async def get_series_summary(self):
            return {"badge": {"count": 9, "tone": "danger", "label": "bad"}}

    good = GoodSibling()
    broken = BrokenSibling()
    async_bad = AsyncMistakeSibling()

    ctx = SimpleNamespace(
        get_all_stars=lambda: [
            SimpleNamespace(
                name="astrbot_plugin_dynamics_learning",
                activated=True,
                star=good,
                version="1.0.0",
            ),
            SimpleNamespace(
                name="astrbot_plugin_dynamics_memory",
                activated=True,
                star=broken,
                version="1.0.0",
            ),
            SimpleNamespace(
                name="astrbot_plugin_dynamics_world",
                activated=True,
                star=async_bad,
                version="1.0.0",
            ),
        ]
    )

    p1 = presence(ctx, PLUGIN_ID, root=tmp_path)
    by_id = {row["id"]: row for row in p1["plugins"]}
    assert by_id["astrbot_plugin_dynamics_learning"]["badge"] == {
        "count": 3,
        "tone": "warm",
        "label": "3 项待审",
    }
    assert by_id["astrbot_plugin_dynamics_learning"]["pulse"] == {
        "headline": "今日已审查 12 条",
        "updated_at": "2026-11-21T14:20:00+08:00",
    }
    assert by_id["astrbot_plugin_dynamics_memory"]["badge"] is None
    assert by_id["astrbot_plugin_dynamics_memory"]["pulse"] is None
    assert by_id["astrbot_plugin_dynamics_world"]["badge"] is None
    assert by_id["astrbot_plugin_dynamics_horizons"]["badge"] is None
    assert calls["count"] == 1

    # Second call within TTL hits memory cache
    p2 = presence(ctx, PLUGIN_ID, root=tmp_path)
    assert calls["count"] == 1
    assert (
        next(r for r in p2["plugins"] if r["id"] == "astrbot_plugin_dynamics_learning")[
            "badge"
        ]["count"]
        == 3
    )

    # Invalidation forces fresh read
    invalidate_series_summary_cache("astrbot_plugin_dynamics_learning")
    presence(ctx, PLUGIN_ID, root=tmp_path)
    assert calls["count"] == 2


@pytest.mark.parametrize(
    "sessions",
    [
        [],  # Empty sessions list = open to all connected groups
        ["qq:GroupMessage:10001", "qq:GroupMessage:20002"],  # Explicitly allowed unbound group
    ],
)
def test_unbound_allowed_scope_never_widens_to_all_personas(tmp_path, sessions):
    """Allowed group with no bound persona must return empty/0, never other personas' records."""
    plugin = _make_plugin(tmp_path, config={"sessions": sessions})
    _seed_sample_data(plugin)
    unbound_umo = "qq:GroupMessage:20002"
    bound_umo = "qq:GroupMessage:10001"

    try:
        # 1. Admin federated search: unbound scope returns [] for both IDENT_A and IDENT_B items
        assert plugin.series_search_items("榫卯", scope=unbound_umo, limit=10) == []
        assert plugin.series_search_items("佛光寺", scope=unbound_umo, limit=10) == []
        assert plugin.series_search_items("银河铁道", scope=unbound_umo, limit=10) == []
        assert plugin.series_search_items("营造学社", scope=unbound_umo, limit=10) == []
        assert plugin.series_search_items("炼金", scope=unbound_umo, limit=10) == []
        # Bound scope still finds its own persona's items only
        assert any(
            "佛光寺" in item["title"]
            for item in plugin.series_search_items("佛光寺", scope=bound_umo, limit=10)
        )
        assert plugin.series_search_items("炼金", scope=bound_umo, limit=10) == []
        # Explicit global search (empty scope) still finds all personas
        global_hits = plugin.series_search_items("炼金", scope="", limit=10)
        assert any("炼金" in item["title"] for item in global_hits)

        # 2. World living journal: unbound scope with empty persona returns []
        assert (
            plugin.get_journal_events(
                persona="", scope=unbound_umo, date="today", limit=20
            )
            == []
        )
        # Bound scope without explicit persona resolves IDENT_A and excludes IDENT_B
        bound_events = plugin.get_journal_events(
            persona="", scope=bound_umo, date="today", limit=20
        )
        assert len(bound_events) >= 2
        assert all("炼金" not in ev["title"] for ev in bound_events)
        # Explicit global journal (empty persona & empty scope) includes both personas
        global_events = plugin.get_journal_events(
            persona="", scope="", date="today", limit=20
        )
        assert any("佛光寺" in ev["title"] for ev in global_events)
        assert any("炼金" in ev["title"] for ev in global_events)

        # 3. Control matrix scope status: preserves open-to-all enabled=True, reports 0 counts
        status_unbound = plugin.get_series_scope_status(unbound_umo)
        assert status_unbound["enabled"] is True
        assert status_unbound["persona_id"] == ""
        assert status_unbound["activity_today"] == 0
        assert "活跃兴趣 0 项" in status_unbound["summary"]
        assert "今日精读 0 篇" in status_unbound["summary"]

        # Bound scope reports IDENT_A counts only (1 active interest, 1 today log)
        status_bound = plugin.get_series_scope_status(bound_umo)
        assert status_bound["enabled"] is True
        assert status_bound["persona_id"] == "rin"
        assert status_bound["activity_today"] == 1
        assert "活跃兴趣 1 项" in status_bound["summary"]
        assert "今日精读 1 篇" in status_bound["summary"]

        # Explicit global scope status (empty umo) reports combined counts across personas
        status_global = plugin.get_series_scope_status("")
        assert status_global["enabled"] is True
        assert status_global["activity_today"] >= 2
        assert "活跃兴趣 2 项" in status_global["summary"]
        assert "今日精读 2 篇" in status_global["summary"]
    finally:
        plugin.engine.store.close()


def test_store_empty_identity_requires_explicit_allow_all(tmp_path):
    """Defense-in-depth: store layer must not treat empty identity as all-persona query."""
    plugin = _make_plugin(tmp_path)
    _seed_sample_data(plugin)
    store = plugin.engine.store
    day_start, day_end = shanghai_day_bounds(NOW_TS)

    try:
        # Default allow_all=False rejects empty identity across all read-only query helpers
        assert store.search_items_readonly("炼金", identity="", now=NOW_TS) == []
        assert store.search_items_readonly("佛光寺", identity="", now=NOW_TS) == []
        assert (
            store.list_journal_events_readonly(
                identity="", start_ts=day_start, end_ts=day_end, limit=20
            )
            == []
        )
        zero_counts = store.summary_counts("", now=NOW_TS)
        assert zero_counts["active_interests"] == 0
        assert zero_counts["logs_total"] == 0
        assert zero_counts["sources_total"] == 0
        assert store.count_today_logs_and_latest("", now=NOW_TS) == (0, 0.0)

        # Explicit allow_all=True enables intentional global queries
        assert any(
            "炼金" in item["title"]
            for item in store.search_items_readonly(
                "炼金", identity="", allow_all=True, now=NOW_TS
            )
        )
        global_events = store.list_journal_events_readonly(
            identity="",
            allow_all=True,
            start_ts=day_start,
            end_ts=day_end,
            limit=20,
        )
        assert len(global_events) >= 4
        global_counts = store.summary_counts("", allow_all=True, now=NOW_TS)
        assert global_counts["active_interests"] == 2
        assert global_counts["logs_total"] == 2
        today_logs, latest_ts = store.count_today_logs_and_latest(
            "", allow_all=True, now=NOW_TS
        )
        assert today_logs == 2
        assert latest_ts > 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cache_anchor_contexts_merged_into_text_chat(tmp_path):
    """Online cache-anchor fix passes deterministic static anchor contexts when supported."""
    assert build_cache_anchor_contexts("") == []
    anchor_a = build_cache_anchor_contexts("Static system prompt", task="dynamics_horizons")
    anchor_b = build_cache_anchor_contexts("Static system prompt", task="dynamics_horizons")
    assert anchor_a == anchor_b
    assert len(anchor_a) == 2
    assert anchor_a[0]["role"] == "user"
    assert anchor_a[0]["content"].startswith("[cache_anchor task=dynamics_horizons prefix=")
    assert anchor_a[1] == {"role": "assistant", "content": "OK"}

    calls_with_ctx: list[dict] = []

    class ProviderWithContexts:
        async def text_chat(self, *, prompt, system_prompt="", contexts=None):
            calls_with_ctx.append(
                {"prompt": prompt, "system_prompt": system_prompt, "contexts": contexts}
            )
            return SimpleNamespace(completion_text='{"ok": true}')

    calls_no_ctx: list[dict] = []

    class ProviderWithoutContexts:
        async def text_chat(self, *, prompt, system_prompt=""):
            calls_no_ctx.append({"prompt": prompt, "system_prompt": system_prompt})
            return SimpleNamespace(completion_text='{"ok": true}')

    assert _accepts_kwarg(ProviderWithContexts().text_chat, "contexts") is True
    assert _accepts_kwarg(ProviderWithoutContexts().text_chat, "contexts") is False

    p_with = ProviderWithContexts()
    p_without = ProviderWithoutContexts()
    providers = {"p_with": p_with, "p_without": p_without}
    ctx = SimpleNamespace(
        get_all_stars=lambda: {},
        get_provider_by_id=lambda pid: providers.get(pid),
    )
    plugin = Main(
        ctx,
        {
            "enabled": True,
            "analysis_provider": "p_with",
            "review_provider": "p_without",
        },
    )
    res1 = await plugin._model("analysis_provider", "Static system prompt", {"x": 1})
    assert res1 == {"ok": True}
    assert len(calls_with_ctx) == 1
    assert calls_with_ctx[0]["contexts"] == anchor_a

    res2 = await plugin._model("review_provider", "Static system prompt", {"x": 2})
    assert res2 == {"ok": True}
    assert len(calls_no_ctx) == 1
    assert calls_no_ctx[0]["system_prompt"] == "Static system prompt"
