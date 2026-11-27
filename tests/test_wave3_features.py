from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.main import Main

IDENT = make_identity("rin", "rin-main")
UMO_A = "napcat:GroupMessage:999001"
UMO_B = "napcat:GroupMessage:999002"


def _make_wake():
    return {
        "world_connected": True,
        "awake": True,
        "asleep": False,
        "paused": False,
        "in_transit": False,
        "skip_reason": "",
        "availability": "available",
        "activity": "在书架前看书",
        "character_name": "凛",
        "character_prompt": "你是温和而细心的图书管理员凛，喜欢与群友共读文学作品。",
        "preferences": ["文学", "古建筑"],
        "scope": {
            "platform": "napcat",
            "umo": "",
            "persona_id": "rin",
            "instance_id": "rin-main",
        },
        "world_link": {"connected": True, "summary": "已联动"},
    }


def _make_setup(tmp_path: Path, model_fn, *, stars=None, clock_start: float = 1763200000.0):
    store = HorizonsStore(tmp_path / "wave3.sqlite3")
    clock_box = [clock_start]
    cfg = {
        "enabled": True,
        "analysis_provider": "prov_a",
        "review_provider": "prov_r",
        "review_mode": "llm",
        "proactive_enabled": True,
        "sessions": [UMO_A, UMO_B],
        "daily_digest_quota": 5,
        "daily_model_call_quota": 50,
        "enable_memory_sync": True,
    }
    ctx = SimpleNamespace(
        get_all_stars=lambda: list(stars or []),
        get_config=lambda: {"dashboard": {"username": "admin"}},
    )
    eng = HorizonsEngine(store, cfg, model_fn, context=ctx, clock=lambda: clock_box[0])
    eng.world_bridge.inspect_wake_and_profile = AsyncMock(return_value=_make_wake())
    eng.world_bridge.discover_personas = lambda _s, _sess, **_kw: [
        {"identity": IDENT, "persona_id": "rin", "label": "凛"}
    ]
    for u in (UMO_A, UMO_B):
        store.bind_scope(u, IDENT, True, clock_box[0])
        eng.projection.bind_umo_identity(u, IDENT)
    return store, eng, ctx, cfg, clock_box


@pytest.mark.asyncio
async def test_co_reading_anti_spoiler_cursor_milestone_and_stage2_guard(tmp_path: Path):
    stage2_calls: list[tuple[str, dict]] = []

    async def model(role, instruction, payload):
        if role == "review_provider":
            return {"verdict": "approve", "reason": "ok", "risks": []}
        stage2_calls.append((instruction, payload))
        return {
            "worth_recording": True,
            "digest": "第四章讲述了星际列车停靠白鸟站的奇妙旅程。",
            "reflection": "白鸟站的风景描写好美，很期待和大家讨论前面章节的伏笔。",
            "staged_interest": {"topic": "银河铁道之夜", "summary": "追读到第四章"},
            "discussion_questions": [
                {"topic": "银河铁道之夜", "question": "前两章里乔班尼在镇上散步那段大家喜欢吗？"}
            ],
        }

    store, eng, _ctx, _cfg, clock_box = _make_setup(tmp_path, model)
    try:
        book = store.create_book_with_chapters(
            identity=IDENT,
            title="银河铁道之夜",
            creator="宫泽贤治",
            chapters=[
                {"label": f"第 {i + 1} 章", "text": f"这是银河铁道之夜第 {i + 1} 章的正文内容，讲述星空铁道的故事。"}
                for i in range(6)
            ],
            now=clock_box[0],
        )
        # Character already read up to chapter 5 (cursor=5); group A is at chapter 2
        store.update_book_status(
            book["book_id"], "reading", chapter_cursor=5, now=clock_box[0]
        )
        prog = await eng.set_group_reading_cursor(
            book_id=book["book_id"],
            umo=UMO_A,
            group_cursor=3,
            updated_by="小明",
        )
        assert prog["group_cursor"] == 3
        assert prog["ahead_chapters"] == 2
        assert prog["milestone_hit"] is True
        assert prog["tea_question"] is not None
        assert "共读小茶会" in prog["tea_question"]["question"]
        assert "不剧透" in prog["tea_question"]["question"]

        # Zero-I/O ProjectionCache injects anti-spoiler boundary for UMO_A (ahead by 2 chapters)
        proj_a = eng.projection.query(
            enabled=True,
            ready=True,
            allowed_sessions=[UMO_A, UMO_B],
            scope=UMO_A,
            persona=IDENT,
            member="u1",
            query="最近在看什么书？聊聊银河铁道之夜吧",
            now=clock_box[0] + 10,
        )
        assert proj_a["matched"] is True
        rp_a = proj_a["reply_projection"]
        assert "【防剧透边界】" in rp_a["policy"]
        assert "第 3 章" in rp_a["policy"]
        assert rp_a["spoiler_guards"][0]["max_safe_chapter"] == 3

        # UMO_B has no group cursor -> no spoiler guard injected for UMO_B
        proj_b = eng.projection.query(
            enabled=True,
            ready=True,
            allowed_sessions=[UMO_A, UMO_B],
            scope=UMO_B,
            persona=IDENT,
            member="u2",
            query="聊聊银河铁道之夜吧",
            now=clock_box[0] + 10,
        )
        assert proj_b["matched"] is True
        assert "spoiler_guards" not in proj_b["reply_projection"]

        # Stage 2 reading of chapter 5 (index 5 > group_cursor 3) injects anti-spoiler prompt guard
        digest_res = await eng.run_digest_cycle(identity=IDENT)
        assert digest_res["approved"] == 1
        assert len(stage2_calls) == 1
        sys_prompt, payload = stage2_calls[0]
        assert "【防剧透边界】" in sys_prompt
        assert payload["metadata"]["co_reading_spoiler_guard"]["max_safe_chapter"] == 3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_invite_to_read_games_activity_and_memory_completion_loop(tmp_path: Path):
    created_activities: list[dict] = []
    memory_notes: list[dict] = []

    class FakeGamesPlugin:
        async def create_activity_from_companion(
            self,
            umo,
            category,
            title,
            *,
            description="",
            creator_name="",
            source_plugin="",
            meta=None,
        ):
            rec = {
                "umo": umo,
                "category": category,
                "title": title,
                "description": description,
                "creator_name": creator_name,
                "source_plugin": source_plugin,
                "meta": meta,
            }
            created_activities.append(rec)
            return {"ok": True, "activity_id": "act_read_001", "title": title}

    class FakeMemoryPlugin:
        async def ingest_horizons_note(self, payload: dict):
            memory_notes.append(payload)
            return {"ok": True}

    stars = [
        {"name": "astrbot_plugin_dynamics_games", "activated": True, "star_cls": FakeGamesPlugin()},
        {"name": "astrbot_plugin_dynamics_memory", "activated": True, "star_cls": FakeMemoryPlugin()},
    ]

    async def model(role, instruction, payload):
        if role == "review_provider":
            return {"verdict": "approve", "reason": "ok", "risks": []}
        return {
            "worth_recording": True,
            "digest": "第一章以温暖的笔触铺陈小镇的星祭之夜与少年的孤独心境，细节极富诗意。",
            "reflection": "太适合拉上群友一起慢慢共读这本书了。",
            "staged_interest": {"topic": "星祭童话", "summary": "想和群友一起共读"},
            "discussion_questions": [],
        }

    store, eng, ctx, cfg, clock_box = _make_setup(tmp_path, model, stars=stars)
    try:
        book = store.create_book_with_chapters(
            identity=IDENT,
            title="银河铁道之夜",
            creator="宫泽贤治",
            chapters=[
                {"label": "第 1 章", "text": "银河铁道之夜第一章正文，星祭之夜的微风吹过小镇。"},
                {"label": "第 2 章", "text": "银河铁道之夜第二章正文，印刷厂里的灯火。"},
            ],
            now=clock_box[0],
        )
        # 1. Reading & approving a book chapter generates `invite_reading_activity` intent
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["approved"] == 1
        due = store.list_due_intents(
            clock_box[0] + 5.0,
            proactive_sessions=[UMO_A],
            quiet_hours="00:00-00:01",
        )
        invite_intents = [
            it for it in due if it.get("horizon_kind") == "invite_reading_activity"
        ]
        assert len(invite_intents) == 1
        assert invite_intents[0]["book_id"] == book["book_id"]
        assert invite_intents[0]["activity_category"] == "reading"

        # 2. Launch group co-reading via `dynamics_games.create_activity_from_companion`
        launched = await eng.launch_group_co_reading(
            umo=UMO_A,
            book_id=book["book_id"],
            identity=IDENT,
            creator_name="小明",
            initial_cursor=1,
        )
        assert launched["ok"] is True
        assert launched["activity_id"] == "act_read_001"
        assert len(created_activities) == 1
        assert created_activities[0]["category"] == "reading"
        assert created_activities[0]["meta"]["book_id"] == book["book_id"]

        # 3. Completion callback marks book「已共读」& writes shared experience to dynamics_memory
        memory_notes.clear()
        plugin = Main(ctx, cfg)
        plugin.engine = eng
        plugin.ready = True
        completed = await plugin.mark_co_reading_completed(
            umo=UMO_A,
            activity_id="act_read_001",
            summary="大家一起读完并聊了很多关于星空与告别的感想。",
            participants=["小明", "小红"],
        )
        assert completed["ok"] is True
        assert completed["co_read_completed"] is True
        assert completed["memory_synced"] is True
        assert len(memory_notes) == 1
        assert memory_notes[0]["umo"] == UMO_A
        assert "小明" in memory_notes[0]["participants"]

        books = store.list_books(IDENT)
        assert books[0]["co_read_completed"] is True
    finally:
        store.close()


@pytest.mark.asyncio
async def test_event_bus_vibe_suppression_and_quiet_window_release(tmp_path: Path):
    bus_subs: dict[str, tuple[str, object]] = {}
    unsubscribed: list[str] = []

    class FakeChatDynamicsBus:
        def subscribe_dynamics_event(self, event_type, callback, *, owner=""):
            tok = f"tok_{event_type}_{len(bus_subs) + 1}"
            bus_subs[tok] = (event_type, callback)
            return tok

        def unsubscribe_dynamics_event(self, token):
            unsubscribed.append(token)
            return bus_subs.pop(token, None) is not None

    bus = FakeChatDynamicsBus()
    stars = [
        {"name": "astrbot_plugin_chat_dynamics", "activated": True, "star_cls": bus},
    ]

    async def model(role, instruction, payload):
        return {"verdict": "approve", "reason": "ok", "risks": []}

    store, eng, ctx, cfg, clock_box = _make_setup(tmp_path, model, stars=stars)
    plugin = Main(ctx, cfg)
    plugin.engine = eng
    plugin.ready = True
    try:
        # 1. Subscribe to vibe_transition & session_quiet_window
        count = eng.subscribe_series_event_bus()
        assert count == 2
        event_types = {ev for ev, _ in bus_subs.values()}
        assert event_types == {"vibe_transition", "session_quiet_window"}

        # Seed two pending intents on UMO_A and UMO_B
        prop = store.create_proposal(
            identity=IDENT,
            source_kind="feed",
            title="古建木构专题长文",
            digest="深入解析中国传统木构建筑斗拱演变与抗震原理的长篇专题。",
            reflection="非常扎实的科普文章，想找机会跟大家聊。",
            staged_interest={"topic": "古建木构", "summary": "斗拱抗震原理"},
            questions=[],
            now=clock_box[0],
        )
        committed = await eng.approve_proposal(prop["proposal_id"], reviewed_by="llm")
        assert len(committed["created_intents"]) == 2

        # 2. Emit serious_inquiry on UMO_A -> UMO_A suppressed from list_due_intents & claim blocked
        vibe_cb = next(cb for ev, cb in bus_subs.values() if ev == "vibe_transition")
        quiet_cb = next(cb for ev, cb in bus_subs.values() if ev == "session_quiet_window")

        await vibe_cb(
            {
                "umo": UMO_A,
                "old_vibe": "casual",
                "new_vibe": "serious_inquiry",
                "serious_mode": True,
            }
        )
        due_after_serious = plugin.list_due_intents(now=clock_box[0] + 5)
        umos_returned = {it["target_umo"] for it in due_after_serious}
        assert UMO_A not in umos_returned
        assert UMO_B in umos_returned

        # Direct claim on UMO_A's intent is also blocked with vibe_suppressed
        intent_a = next(
            it for it in committed["created_intents"] if it["target_umo"] == UMO_A
        )
        claim_res = plugin.update_intent(intent_a["intent_id"], "claimed")
        assert claim_res["status"] == "vibe_suppressed"
        assert claim_res["changed"] is False

        # 3. UMO_A returns to casual & enters quiet_window -> released and prioritized first!
        await vibe_cb(
            {
                "umo": UMO_A,
                "old_vibe": "serious_inquiry",
                "new_vibe": "casual",
                "serious_mode": False,
            }
        )
        await quiet_cb(
            {"umo": UMO_A, "quiet_seconds": 420, "last_activity_at": clock_box[0] - 420}
        )
        due_released = plugin.list_due_intents(now=clock_box[0] + 10)
        assert len(due_released) == 2
        assert due_released[0]["target_umo"] == UMO_A
        assert due_released[0]["quiet_window_ready"] is True
        assert due_released[1]["target_umo"] == UMO_B
        assert due_released[1]["quiet_window_ready"] is False

        # 4. Unsubscribe cleans up all tokens safely
        removed = eng.unsubscribe_series_event_bus()
        assert removed == 2
        assert len(bus_subs) == 0
        assert len(unsubscribed) == 2
    finally:
        store.close()
