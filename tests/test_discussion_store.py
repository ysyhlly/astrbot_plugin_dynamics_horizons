from __future__ import annotations

import json

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore


IDENTITY = make_identity("reflection-test")
UMO = "qq:GroupMessage:reflection-test"


def _store(tmp_path):
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    with store.lock, store.db:
        store.db.execute(
            """INSERT INTO reading_logs(
                log_id,proposal_id,identity,source_kind,citation_json,raw_url,
                digest,reflection,fact_kind,created_at,status,revision
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "log-1", "proposal-1", IDENTITY, "manual", json.dumps({"title": "茶"}),
                "", "围绕茶的资料", "原有角色随感", "external_media", 10.0, "active", 3,
            ),
        )
    return store


def _record(store, **overrides):
    values = {
        "identity": IDENTITY,
        "umo": UMO,
        "member_id": "member-1",
        "log_id": "log-1",
        "topic": "茶文化",
        "anchor_event_id": "intent-1",
        "source_message_id": "message-1",
        "member_quote": "我喜欢点茶的仪式感。",
        "reflection": "她提到点茶时，语气里有种安静的喜欢。",
        "source_log_revision": 3,
        "occurred_at": 20.0,
        "now": 21.0,
    }
    values.update(overrides)
    return store.record_discussion_reflection(**values)


def test_records_once_with_scope_and_safe_text(tmp_path):
    store = _store(tmp_path)
    try:
        assert _record(
            store,
            member_quote="我喜欢点茶。 https://example.test/private",
            reflection="她说得真好。 https://example.test/private",
        ) is True
        assert _record(store) is False

        rows = store.list_discussion_reflections(IDENTITY, log_id="log-1")
        assert len(rows) == 1
        row = rows[0]
        assert row["umo"] == UMO
        assert row["member_id"] == "member-1"
        assert row["source_log_revision"] == 3
        assert "https://" not in row["member_quote"] + row["reflection"]
        assert store.list_discussion_reflections(IDENTITY, umo="another-umo") == []
        assert store.list_discussion_reflections(
            IDENTITY, umo=UMO, member_id="member-2"
        ) == []
        log = store.list_logs(identity=IDENTITY)["items"][0]
        assert log["discussion_reflections"][0]["member_id"] == "member-1"
    finally:
        store.close()


def test_rejects_stale_or_revoked_log_and_revision(tmp_path):
    store = _store(tmp_path)
    try:
        assert _record(store, source_log_revision=2) is False
        with store.lock, store.db:
            store.db.execute(
                "UPDATE reading_logs SET status='revoked' WHERE log_id='log-1'"
            )
        assert _record(store) is False
    finally:
        store.close()


def test_rechecks_topic_member_optout_and_pending_correction(tmp_path):
    store = _store(tmp_path)
    try:
        with store.lock, store.db:
            store.db.execute(
                "INSERT INTO member_outreach_prefs VALUES(?,?,?,?,?)",
                (IDENTITY, UMO, "member-1", 1, 22.0),
            )
        assert _record(store) is False
        with store.lock, store.db:
            store.db.execute(
                "UPDATE member_outreach_prefs SET opted_out=0 WHERE identity=?",
                (IDENTITY,),
            )
            store.db.execute(
                "INSERT INTO member_topic_prefs VALUES(?,?,?,?,?,?,?)",
                (IDENTITY, UMO, "member-1", "茶文化", 1, "event", 23.0),
            )
        assert _record(store) is False
        with store.lock, store.db:
            store.db.execute(
                "DELETE FROM member_topic_prefs WHERE identity=?", (IDENTITY,)
            )
            store.db.execute(
                "INSERT INTO corrections(correction_id,identity,umo,member_id,log_id,statement,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("correction-1", IDENTITY, UMO, "member-1", "log-1", "有误", "pending", 24.0),
            )
        assert _record(store) is False
        with store.lock, store.db:
            store.db.execute(
                "INSERT INTO blocked_topics(topic,identity,created_at) VALUES(?,?,?)",
                ("茶文化", IDENTITY, 25.0),
            )
            store.db.execute("UPDATE corrections SET status='rejected'")
        assert _record(store) is False
    finally:
        store.close()


def test_additive_migration_reopen_preserves_reflections(tmp_path):
    path = tmp_path / "horizons.sqlite3"
    first = HorizonsStore(path)
    with first.lock, first.db:
        first.db.execute(
            """INSERT INTO reading_logs(
                log_id,proposal_id,identity,source_kind,citation_json,raw_url,
                digest,reflection,fact_kind,created_at,status,revision
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("log-1", "", IDENTITY, "manual", "{}", "", "d", "r", "external_media", 1.0, "active", 1),
        )
    first.close()

    # Simulate a database created before the additive discussion table existed.
    import sqlite3

    legacy = sqlite3.connect(path)
    legacy.execute("DROP TABLE discussion_reflections")
    legacy.commit()
    legacy.close()

    second = HorizonsStore(path)
    assert _record(second, source_log_revision=1) is True
    second.close()

    third = HorizonsStore(path)
    try:
        assert len(third.list_discussion_reflections(IDENTITY)) == 1
        columns = {
            row["name"]
            for row in third.db.execute("PRAGMA table_info(discussion_reflections)")
        }
        assert {
            "identity", "umo", "member_id", "log_id", "source_message_id",
            "source_log_revision", "reflection",
        } <= columns
    finally:
        third.close()
