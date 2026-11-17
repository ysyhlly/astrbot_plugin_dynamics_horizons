from __future__ import annotations

from datetime import datetime
import json
from zoneinfo import ZoneInfo

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.projection import (
    ProjectionCache,
    _fit_reply_projection,
)
from astrbot_plugin_dynamics_horizons.core.sanitize import contains_raw_url
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore


def _note(**changes):
    note = {
        "reflection_id": "reflection-1",
        "identity": make_identity("alice", "alice"),
        "umo": "qq:GroupMessage:1001",
        "member_id": "u_1",
        "log_id": "log-coffee",
        "topic": "咖啡萃取",
        "member_quote": "我也试过降低水温，甜感会更明显。",
        "reflection": "她的对照让我想起，下次可以把研磨度也一起记录。",
        "created_at": 100,
        "occurred_at": 90,
        "status": "active",
    }
    note.update(changes)
    return note


def _cache(notes):
    ident = make_identity("alice", "alice")
    cache = ProjectionCache()
    cache.update_snapshot(
        {
            "by_identity": {
                ident: {
                    "blocked": [],
                    "interests": [],
                    "corrections": [],
                    "member_topics": [],
                    "logs": [
                        {
                            "log_id": "log-coffee",
                            "source_kind": "bilibili",
                            "citation": {"title": "咖啡萃取记录", "creator": "", "bvid": "", "chapter_label": ""},
                            "digest": "对照了两种研磨度与注水水温。",
                            "reflection": "原始阅读感想保持原样。",
                            "created_at": 80,
                        }
                    ],
                    "discussion_reflections": notes,
                    "books": [],
                    "questions": [],
                }
            }
        }
    )
    return cache


def _query(cache, *, persona="alice", umo="qq:GroupMessage:1001", member="u_1"):
    return cache.query(
        enabled=True,
        ready=True,
        allowed_sessions=[],
        scope=umo,
        persona=persona,
        member=member,
        query="最近在看什么？咖啡萃取",
        now=200,
    )


def test_reply_projection_includes_only_same_identity_umo_and_member():
    note = _note()
    note_from_other_member = _note(reflection_id="reflection-2", member_id="u_2")
    note_from_other_group = _note(reflection_id="reflection-3", umo="qq:GroupMessage:2002")
    note_from_other_character = _note(
        reflection_id="reflection-4", identity=make_identity("bob", "bob")
    )
    result = _query(
        _cache([note, note_from_other_member, note_from_other_group, note_from_other_character])
    )

    reading = result["reply_projection"]["readings"][0]
    assert reading["reflection"] == "原始阅读感想保持原样。"
    assert [r["reflection_id"] for r in reading["discussion_reflections"]] == ["reflection-1"]
    assert "下次可以把研磨度" in reading["discussion_reflections"][0]["reflection"]
    assert any(v == "discussion:reflection-1" for v in result["versions"])


def test_reply_projection_suppresses_member_blocked_and_global_topics():
    notes = [
        _note(reflection_id="member-blocked"),
        _note(reflection_id="global-blocked", topic="被屏蔽话题"),
    ]
    cache = _cache(notes)
    cache._snapshot["by_identity"][make_identity("alice", "alice")]["member_topics"] = [
        {"umo": "qq:GroupMessage:1001", "member_id": "u_1", "topic": "咖啡萃取"}
    ]
    cache._snapshot["by_identity"][make_identity("alice", "alice")]["blocked"] = ["被屏蔽话题"]

    reading = _query(cache)["reply_projection"]["readings"][0]
    assert reading["reflection"] == "原始阅读感想保持原样。"
    assert reading["discussion_reflections"] == []


def test_projection_sanitizes_urls_and_respects_existing_budget():
    note = _note(
        member_quote="我试过这款方法 https://example.test/private?q=1，甜感明显。",
        reflection="下次想对照更细的参数 https://example.test/private?q=2。",
    )
    result = _query(_cache([note]))["reply_projection"]
    assert len(json.dumps(result, ensure_ascii=False)) <= 1800
    assert not contains_raw_url(result)
    projected = result["readings"][0]["discussion_reflections"][0]
    assert "https://" not in projected["member_quote"]
    assert "https://" not in projected["reflection"]
    assert "自动变成固定人设" in result["policy"]

    huge = {
        "policy": "p" * 1700,
        "readings": [{"reflection": "r" * 200, "discussion_reflections": [{"reflection_id": "n", "member_quote": "q" * 200, "reflection": "f" * 200}]}],
        "interests": [],
    }
    bounded = _fit_reply_projection(huge)
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 1800


def test_real_store_snapshot_can_recall_a_new_angle_from_same_member(tmp_path):
    now = datetime(2026, 11, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    identity = make_identity("alice", "alice")
    umo = "qq:GroupMessage:1001"
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    try:
        with store.lock, store.db:
            store.db.execute(
                """INSERT INTO reading_logs(
                    log_id,proposal_id,identity,source_kind,citation_json,raw_url,
                    digest,reflection,fact_kind,created_at,status,revision
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "log-coffee", "proposal", identity, "bilibili",
                    json.dumps({"title": "手冲咖啡的水温对照", "creator": ""}),
                    "", "比较不同水温对风味的影响。", "原有感想只谈到水温。",
                    "external_media", now - 60, "active", 1,
                ),
            )
        assert store.record_discussion_reflection(
            identity=identity,
            umo=umo,
            member_id="u_1",
            log_id="log-coffee",
            topic="手冲咖啡",
            anchor_event_id="outgoing-message-1",
            source_message_id="incoming-message-1",
            member_quote="我试过冷水浸泡，甜感也会很明显。",
            reflection="她提到冷水浸泡后，我想下次单独比较浸泡时长。",
            source_log_revision=1,
            occurred_at=now - 10,
            now=now,
        )
        snapshot = store.build_projection_snapshot(now=now)
    finally:
        store.close()

    cache = ProjectionCache()
    cache.update_snapshot(snapshot)
    result = cache.query(
        enabled=True,
        ready=True,
        allowed_sessions=[umo],
        scope=umo,
        character_scope={"persona_id": "alice", "instance_id": "alice"},
        member="u_1",
        query="下次想比较冷水浸泡的时间长短",
        now=now,
    )
    assert result["matched"] is True
    assert result["reply_projection"]["readings"][0]["log_id"] == "log-coffee"
    assert "单独比较浸泡时长" in result["reply_projection"]["readings"][0]["discussion_reflections"][0]["reflection"]


