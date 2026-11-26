"""Unit tests for topic-inquiry projection matching and the `query_horizons_topics` LLM tool."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.projection import _STATUS_INQUIRY_RE, ProjectionCache
from astrbot_plugin_dynamics_horizons.core.sanitize import contains_raw_url
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.main import Main

IDENT_A = make_identity("anan", "anan")
IDENT_B = make_identity("bob", "bob")
UMO_1 = "qq:GroupMessage:620537488"
UMO_2 = "qq:GroupMessage:999999999"


def _make_snapshot(*, now: float = 10000.0) -> dict:
    return {
        "built_at": now,
        "by_identity": {
            IDENT_A: {
                "identity": IDENT_A,
                "blocked": ["敏感禁词"],
                "corrections": [],
                "member_topics": [
                    {"umo": UMO_1, "member_id": "u_block", "topic": "末日求生设计"}
                ],
                "interests": [
                    {
                        "interest_id": "int_1",
                        "topic": "多线叙事与演出过载",
                        "summary": "最近在留意节奏与信息密度怎么平衡 https://example.com/a",
                        "heat_score": 1.6,
                        "status": "active",
                        "expires_at": now + 86400,
                        "evidence_log_ids": ["log_1"],
                    }
                ],
                "logs": [
                    {
                        "log_id": "log_1",
                        "source_kind": "bilibili",
                        "citation": {
                            "title": "一口气看完高密度多线番剧解析",
                            "creator": "番剧观察",
                            "bvid": "BV1xx411c7mD",
                            "chapter_label": "",
                        },
                        "digest": "讨论一集塞太多支线会让人喘不过气。",
                        "reflection": "信息量太大反而抓不住重点。",
                        "created_at": now - 60,
                    }
                ],
                "discussion_reflections": [],
                "books": [],
                "questions": [
                    {
                        "question_id": "q_1",
                        "log_id": "log_1",
                        "interest_id": "int_1",
                        "topic": "多线叙事与演出过载",
                        "question": "大家追番是喜欢一口气看完，还是一周一集慢慢消化？",
                        "created_at": now - 30,
                        "expires_at": now + 3600,
                        "discussed_umos": {},
                    },
                    {
                        "question_id": "q_2",
                        "log_id": "log_2",
                        "interest_id": "int_2",
                        "topic": "普通招式偏门用法",
                        "question": "比起天降神技，大家更喜欢看把基础招式玩出花吗？",
                        "created_at": now - 60,
                        "expires_at": now + 3600,
                        "discussed_umos": {},
                    },
                    {
                        "question_id": "q_3",
                        "log_id": "log_3",
                        "interest_id": "int_3",
                        "topic": "末日求生设计",
                        "question": "如果发现同行者偷偷藏着咬痕，你会立刻拆穿吗？",
                        "created_at": now - 90,
                        "expires_at": now + 3600,
                        "discussed_umos": {},
                    },
                    {
                        "question_id": "q_done",
                        "log_id": "log_4",
                        "interest_id": "int_4",
                        "topic": "古建筑斗拱",
                        "question": "木结构不用钉子靠榫卯承重是不是很神奇？",
                        "created_at": now - 10,
                        "expires_at": now + 3600,
                        "discussed_umos": {UMO_1: now - 20000},
                    },
                    {
                        "question_id": "q_expired",
                        "log_id": "log_5",
                        "interest_id": "int_5",
                        "topic": "过期话题",
                        "question": "这条已经过期了不该出现",
                        "created_at": now - 50000,
                        "expires_at": now - 10,
                        "discussed_umos": {},
                    },
                ],
            },
            IDENT_B: {
                "identity": IDENT_B,
                "blocked": [],
                "corrections": [],
                "member_topics": [],
                "interests": [],
                "logs": [],
                "discussion_reflections": [],
                "books": [],
                "questions": [
                    {
                        "question_id": "q_other_persona",
                        "log_id": "log_b",
                        "interest_id": "int_b",
                        "topic": "鲍勃私密话题",
                        "question": "不该跨角色泄漏的问题",
                        "created_at": now - 5,
                        "expires_at": now + 3600,
                        "discussed_umos": {},
                    }
                ],
            },
        },
        "summary": {},
    }


@pytest.mark.parametrize(
    "text",
    [
        "最近有什么有趣的话题吗",
        "最近有什么有意思的话题",
        "最近有没有什么有意思的话题呀",
        "最近有啥好玩的话题",
        "有什么有趣的话题可以聊",
        "有什么有意思的话题吗",
        "找个有意思的话题聊聊",
        "分享点有趣的见闻",
        "最近看到什么有趣的",
        "最近在看什么书",
        "any interesting topics?",
    ],
)
def test_status_inquiry_regex_matches_topic_queries(text: str):
    assert _STATUS_INQUIRY_RE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "今天晚上吃什么",
        "哈哈太有趣了",
        "这个话题我不聊了",
        "没有什么有趣的话题",
        "最近有点累",
        "最近天气怎么样",
    ],
)
def test_status_inquiry_regex_ignores_non_inquiries(text: str):
    assert _STATUS_INQUIRY_RE.search(text) is None


def test_projection_topic_inquiry_returns_multiple_questions_and_supports_questions_only():
    cache = ProjectionCache()
    cache.update_snapshot(_make_snapshot())

    hit = cache.query(
        enabled=True,
        ready=True,
        allowed_sessions=[],
        scope=UMO_1,
        character_scope={"persona_id": "anan", "instance_id": "anan"},
        member="u_1",
        query="最近有什么有趣的话题吗",
        now=10000.0,
    )
    assert hit["matched"] is True
    assert hit["match_reason"] == "status_inquiry"
    qids = [q["question_id"] for q in hit["reply_projection"]["questions"]]
    assert qids == ["q_1", "q_2", "q_3"]
    assert "q_done" not in qids
    assert "q_expired" not in qids
    assert len(json.dumps(hit["reply_projection"], ensure_ascii=False)) <= 1800
    assert not contains_raw_url(hit["reply_projection"])
    assert not contains_raw_url(hit["decision_slot"])

    # Even when interests/logs/books are empty, active eligible_questions alone should still match
    snap_q_only = _make_snapshot()
    snap_q_only["by_identity"][IDENT_A]["interests"] = []
    snap_q_only["by_identity"][IDENT_A]["logs"] = []
    snap_q_only["by_identity"][IDENT_A]["books"] = []
    cache_q_only = ProjectionCache()
    cache_q_only.update_snapshot(snap_q_only)
    q_only_hit = cache_q_only.query(
        enabled=True,
        ready=True,
        allowed_sessions=[],
        scope=UMO_1,
        character_scope={"persona_id": "anan", "instance_id": "anan"},
        member="u_1",
        query="最近有什么有趣的话题吗",
        now=10000.0,
    )
    assert q_only_hit["matched"] is True
    assert [q["question_id"] for q in q_only_hit["reply_projection"]["questions"]] == [
        "q_1",
        "q_2",
        "q_3",
    ]


@pytest.mark.asyncio
async def test_query_horizons_topics_llm_tool_scoping_filtering_and_feedback_offer(tmp_path):
    ctx = SimpleNamespace(get_all_stars=lambda: {})
    cfg = {"enabled": True, "sessions": [UMO_1, UMO_2]}
    plugin = Main(ctx, cfg)
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    engine = HorizonsEngine(store, cfg, lambda *a, **kw: {}, context=ctx, clock=lambda: 10000.0)
    plugin.engine = engine
    plugin.ready = True
    plugin.closed = False
    try:
        engine.projection.update_snapshot(_make_snapshot())
        engine.projection.bind_umo_identity(UMO_1, IDENT_A)
        engine.projection.bind_umo_identity(UMO_2, IDENT_A)

        ev1 = SimpleNamespace(unified_msg_origin=UMO_1, get_sender_id=lambda: "u_1")
        raw = await plugin.query_horizons_topics(ev1, kind="questions", query="", limit=5)
        data = json.loads(raw)
        assert data["available"] is True
        assert "_offer" not in data
        assert not contains_raw_url(data)
        qids = [q["question_id"] for q in data["questions"]]
        assert qids == ["q_1", "q_2", "q_3"]
        assert "q_done" not in qids
        assert "q_other_persona" not in qids
        # Tool offer is recorded on the event for verified delivery feedback
        offer = getattr(ev1, "_horizons_tool_context_v1", None)
        assert isinstance(offer, dict)
        assert "q_1" in offer["question_ids"]
        assert "log_1" in offer["log_ids"]

        # Another group (UMO_2) still sees q_done because it was only discussed in UMO_1
        ev2 = SimpleNamespace(unified_msg_origin=UMO_2, get_sender_id=lambda: "u_2")
        data2 = json.loads(await plugin.query_horizons_topics(ev2, kind="questions", query="", limit=5))
        assert "q_done" in [q["question_id"] for q in data2["questions"]]

        # Member-blocked topic is filtered out for u_block
        ev_blk = SimpleNamespace(unified_msg_origin=UMO_1, get_sender_id=lambda: "u_block")
        data_blk = json.loads(await plugin.query_horizons_topics(ev_blk))
        assert "q_3" not in [q["question_id"] for q in data_blk["questions"]]

        # Global blocked keyword returns blocked_topic
        blocked_res = json.loads(await plugin.query_horizons_topics(ev1, query="聊聊敏感禁词"))
        assert blocked_res["available"] is False
        assert blocked_res["reason"] == "blocked_topic"

        # Disallowed session returns scope_not_allowed
        ev_other = SimpleNamespace(unified_msg_origin="qq:GroupMessage:777", get_sender_id=lambda: "u_9")
        denied = json.loads(await plugin.query_horizons_topics(ev_other))
        assert denied["available"] is False
        assert denied["reason"] == "scope_not_allowed"

        # Unbound session NEVER leaks another persona's questions
        plugin.config["sessions"] = []
        ev_unbound = SimpleNamespace(unified_msg_origin="qq:GroupMessage:888", get_sender_id=lambda: "u_9")
        unbound = json.loads(await plugin.query_horizons_topics(ev_unbound))
        assert unbound["available"] is False
        assert unbound["reason"] == "no_identity"

        # Keyword query hit vs fallback
        kw_hit = json.loads(await plugin.query_horizons_topics(ev1, kind="topics", query="追番"))
        assert kw_hit["matched_by"] == "query"
        assert kw_hit["questions"][0]["question_id"] == "q_1"

        kw_fb = json.loads(await plugin.query_horizons_topics(ev1, kind="questions", query="完全不存在的火星词"))
        assert kw_fb["matched_by"] == "recent_fallback"
        assert len(kw_fb["questions"]) >= 1
    finally:
        store.close()
