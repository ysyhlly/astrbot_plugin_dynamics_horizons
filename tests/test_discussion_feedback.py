from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.feedback import enqueue, process_jobs
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore

IDENTITY = make_identity("discussion-feedback-test")
OTHER_IDENTITY = make_identity("discussion-feedback-other")
UMO = "qq:GroupMessage:discussion-feedback"
OTHER_UMO = "qq:GroupMessage:other"
MEMBER = "member-1"
OTHER_MEMBER = "member-2"
LOG_ID = "log-discussion-1"
NOW = 1_800_000_000.0
OPENING_TEXT = "我读到一篇关于茶文化的文章，作者比较了不同的冲泡方式。"
USER_TEXT = "我觉得水温影响很大，尤其是绿茶容易苦。"
REFLECTION = "对方提到水温的影响，这个角度让我想再看看不同茶类的冲泡差异。"


class Model:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.calls = []
        self.on_call = None

    async def __call__(self, role, instruction, payload):
        self.calls.append((role, instruction, payload))
        if self.on_call:
            self.on_call(role, instruction, payload)
        if not self.outputs:
            return {"questions": [], "topics": [], "openings": [], "reflections": []}
        result = self.outputs.pop(0)
        if isinstance(result, BaseException):
            raise result
        if result.get("reflections") and payload.get("openings"):
            anchor_id = payload["openings"][0].get("anchor_event_id", "")
            for item in result["reflections"]:
                if isinstance(item, dict) and not item.get("anchor_event_id"):
                    item["anchor_event_id"] = anchor_id
        return result


def _log(store, *, identity=IDENTITY, status="active", revision=2):
    with store.lock, store.db:
        store.db.execute(
            """INSERT INTO reading_logs(
                log_id,proposal_id,identity,source_kind,citation_json,raw_url,
                digest,reflection,fact_kind,created_at,status,revision
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                LOG_ID, "proposal-1", identity, "manual", json.dumps({"title": "茶文化"}),
                "", "不同冲泡方式的资料摘要", "角色原有阅读感想", "external_media",
                NOW - 100, status, revision,
            ),
        )


def _question(store, question_id="question-1"):
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO discussion_questions(question_id,identity,log_id,topic,question,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (question_id, IDENTITY, LOG_ID, "茶文化", "不同水温会帶來什么变化？", "active", NOW - 100, NOW + 86400),
        )


def _sent_intent(store, intent_id="horizon:intent-1", *, identity=IDENTITY, umo=UMO, member=MEMBER, status="sent"):
    payload = {
        "intent_id": intent_id, "provider": "dynamics_horizons", "log_id": LOG_ID,
        "recipient_user_id": member, "topic": "茶文化", "status": status,
    }
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO horizon_intents(intent_id,identity,umo,recipient_user_id,topic,log_id,status,payload_json,not_before,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent_id, identity, umo, member, "茶文化", LOG_ID, status,
             json.dumps(payload, ensure_ascii=False), NOW - 100, NOW + 86400, NOW - 90, NOW - 90),
        )


class Rig:
    def __init__(self, path: Path, model: Model):
        self.clock_value = NOW
        self.store = HorizonsStore(path)
        self.model = model
        self.engine = HorizonsEngine(
            self.store,
            {"daily_model_call_quota": 100, "review_provider": "test-review", "review_mode": "llm", "sessions": [UMO, OTHER_UMO]},
            model,
            clock=lambda: self.clock_value,
        )
        _log(self.store)

    def tick(self, seconds=1):
        self.clock_value += seconds

    def close(self):
        self.store.close()


def _delivery_event(*, member=MEMBER, umo=UMO, event_id="delivery-1", intent_id="", log_ids=None, question_ids=None, source_message_id="source-user-1", reply_ids=None, identity=IDENTITY, reply_text=OPENING_TEXT):
    event = {
        "kind": "delivery.success", "delivered": True, "identity": identity,
        "umo": umo, "member": member, "event_id": event_id,
        "turn_id": event_id, "source_message_id": source_message_id,
        "reply_message_ids": list(reply_ids or ["bot-question-1"]),
        "reply": {"message_id": "bot-question-1", "text": reply_text, "delivered": True},
        "reply_text": reply_text, "log_ids": list([LOG_ID] if log_ids is None else log_ids),
        "question_ids": list(question_ids or []), "topics": ["茶文化"],
    }
    if intent_id:
        event["intent_id"] = intent_id
    return event


def _incoming(*, member=MEMBER, umo=UMO, message_id="member-reply-1", quote="bot-question-1", identity=IDENTITY, text=USER_TEXT):
    return {
        "kind": "message.received", "delivered": False, "identity": identity,
        "umo": umo, "member": member, "event_id": message_id,
        "message_id": message_id, "reply_to_message_id": quote,
        "reply_to_sender_id": "bot", "text": text,
    }


def _model(*, opening=True, response=True, empty_opening=False, fabricate_member=False, fail_first_response=False, revoke_during_response=None):
    outputs: list[Any] = []
    if opening:
        outputs.append({
            "questions": [], "topics": [],
            "openings": [] if empty_opening else [{"log_id": LOG_ID, "reply_quote": OPENING_TEXT[:16]}],
        })
    if fail_first_response:
        outputs.append(RuntimeError("review provider unavailable"))
    if response:
        outputs.append({
            "reflections": [{
                "anchor_event_id": "", "log_id": LOG_ID,
                "member_quote": "fabricated quote not in user text" if fabricate_member else USER_TEXT[:16],
                "reflection": REFLECTION,
            }]
        })
    model = Model(outputs)
    if revoke_during_response:
        def mutate(role, instruction, payload):
            if "explicit_reply" in payload:
                with revoke_during_response.store.lock, revoke_during_response.store.db:
                    revoke_during_response.store.db.execute(
                        "UPDATE reading_logs SET status='revoked' WHERE log_id=?", (LOG_ID,)
                    )
        model.on_call = mutate
    return model


def _count_reflections(rig):
    return rig.store.db.execute("SELECT count(*) FROM discussion_reflections").fetchone()[0]


def _opening_job(rig):
    return rig.store.db.execute(
        "SELECT * FROM feedback_jobs WHERE identity=? AND umo=? AND member_id=? ORDER BY created_at LIMIT 1",
        (IDENTITY, UMO, MEMBER),
    ).fetchone()


@pytest.mark.asyncio
async def test_sent_proactive_intent_opens_feedback_and_silent_reply_is_recorded(tmp_path):
    model = _model()
    rig = Rig(tmp_path / "proactive.sqlite3", model)
    try:
        _sent_intent(rig.store)
        assert enqueue(rig.engine, _delivery_event(intent_id="horizon:intent-1"))["queued"] is True
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"] is True
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 1
        note = rig.store.list_discussion_reflections(IDENTITY, umo=UMO, member_id=MEMBER)[0]
        assert note["log_id"] == LOG_ID
        assert note["member_quote"] == USER_TEXT[:16]
        assert note["reflection"] == REFLECTION
        assert note["source_message_id"] == "member-reply-1"
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_verified_question_delivery_can_open_reading_log(tmp_path):
    model = Model([
        {"questions": [{"question_id": "question-1", "reply_quote": "我读到一篇关于茶文化"}], "topics": [], "openings": []},
        {"reflections": [{"anchor_event_id": "", "log_id": LOG_ID, "member_quote": USER_TEXT[:16], "reflection": REFLECTION}]},
    ])
    rig = Rig(tmp_path / "question.sqlite3", model)
    try:
        _question(rig.store)
        assert enqueue(rig.engine, _delivery_event(question_ids=["question-1"]))["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"]
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 1
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_incoming_reply_waits_for_pending_opening_review_then_survives(tmp_path):
    rig = Rig(tmp_path / "pending.sqlite3", _model())
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"]
        opening = _opening_job(rig)
        with rig.store.lock, rig.store.db:
            rig.store.db.execute("UPDATE feedback_jobs SET next_at=? WHERE event_id=?", (rig.clock_value + 20, opening["event_id"]))
        await process_jobs(rig.engine)
        pending = rig.store.db.execute(
            "SELECT status,next_at FROM feedback_jobs WHERE json_extract(payload_json,'$.kind')='discussion_reply'"
        ).fetchone()
        assert pending["status"] == "pending"
        assert pending["next_at"] > rig.clock_value
        rig.tick(30)
        with rig.store.lock, rig.store.db:
            rig.store.db.execute("UPDATE feedback_jobs SET next_at=? WHERE event_id=?", (rig.clock_value, opening["event_id"]))
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 1
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_implicit_recent_offer_is_same_member_only_but_explicit_quote_can_cross_member(tmp_path):
    rig = Rig(tmp_path / "member-scope.sqlite3", _model(opening=False, response=False))
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming(message_id="same-implicit", quote=""))["queued"]
        assert enqueue(rig.engine, _incoming(member=OTHER_MEMBER, message_id="other-implicit", quote=""))["queued"] is False
        assert enqueue(rig.engine, _incoming(member=OTHER_MEMBER, message_id="other-explicit", quote="bot-question-1"))["queued"]
        rows = rig.store.db.execute(
            "SELECT member_id,payload_json FROM feedback_jobs WHERE json_extract(payload_json,'$.kind')='discussion_reply' ORDER BY created_at"
        ).fetchall()
        assert [r["member_id"] for r in rows] == [MEMBER, OTHER_MEMBER]
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_cross_umo_persona_and_wrong_quote_do_not_queue(tmp_path):
    rig = Rig(tmp_path / "scope-guard.sqlite3", _model(opening=False, response=False))
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming(umo=OTHER_UMO, message_id="cross-umo"))["queued"] is False
        assert enqueue(rig.engine, _incoming(identity=OTHER_IDENTITY, message_id="cross-persona"))["queued"] is False
        assert enqueue(rig.engine, _incoming(quote="some-other-bot-message", message_id="wrong-quote"))["queued"] is False
        assert rig.store.db.execute(
            "SELECT count(*) FROM feedback_jobs WHERE json_extract(payload_json,'$.kind')='discussion_reply'"
        ).fetchone()[0] == 0
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_duplicate_replay_is_idempotent_and_original_source_is_not_a_reply(tmp_path):
    rig = Rig(tmp_path / "replay.sqlite3", _model(opening=False, response=False))
    try:
        assert enqueue(rig.engine, _delivery_event(source_message_id="same-id"))["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming(message_id="same-id", quote=""))["queued"] is False
        first = _incoming(message_id="reply-replay", quote="bot-question-1")
        assert enqueue(rig.engine, first)["queued"]
        assert enqueue(rig.engine, first)["queued"]
        assert rig.store.db.execute(
            "SELECT count(*) FROM feedback_jobs WHERE json_extract(payload_json,'$.kind')='discussion_reply'"
        ).fetchone()[0] == 1
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_unverified_opening_and_empty_reviewer_create_no_reflection(tmp_path):
    model = Model([
        {"questions": [], "topics": [], "openings": [{"log_id": LOG_ID, "reply_quote": "这段话不在实际送达内容中"}]},
        {"questions": [], "topics": [], "openings": []},
    ])
    rig = Rig(tmp_path / "unverified.sqlite3", model)
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"] is True
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 0

        # A separately verified opening with an empty follow-up reviewer result is also a no-op.
        model.outputs = [
            {"questions": [], "topics": [], "openings": [{"log_id": LOG_ID, "reply_quote": OPENING_TEXT[:16]}]},
            {"reflections": []},
        ]
        assert enqueue(rig.engine, _delivery_event(event_id="delivery-2", reply_ids=["bot-question-2"]))["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming(message_id="reply-2", quote="bot-question-2"))["queued"]
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 0
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_fabricated_member_quote_is_rejected(tmp_path):
    rig = Rig(tmp_path / "fabricated.sqlite3", _model(fabricate_member=True))
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"]
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 0
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_discussion_reviewer_failure_retries_and_then_records(tmp_path):
    rig = Rig(tmp_path / "retry.sqlite3", _model(fail_first_response=True))
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"]
        await process_jobs(rig.engine)
        reply = rig.store.db.execute(
            "SELECT event_id,attempts,status,next_at FROM feedback_jobs WHERE json_extract(payload_json,'$.kind')='discussion_reply'"
        ).fetchone()
        assert reply["attempts"] == 1
        assert reply["status"] == "pending"
        assert _count_reflections(rig) == 0
        rig.clock_value = reply["next_at"] + 1
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 1
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_revoked_reading_during_model_review_is_rejected(tmp_path):
    rig = Rig(tmp_path / "revoked-race.sqlite3", Model([
        {"questions": [], "topics": [], "openings": [{"log_id": LOG_ID, "reply_quote": OPENING_TEXT[:16]}]},
        {"reflections": [{"anchor_event_id": "", "log_id": LOG_ID, "member_quote": USER_TEXT[:16], "reflection": REFLECTION}]},
    ]))
    try:
        def revoke(role, instruction, payload):
            if "explicit_reply" in payload:
                with rig.store.lock, rig.store.db:
                    rig.store.db.execute("UPDATE reading_logs SET status='revoked' WHERE log_id=?", (LOG_ID,))
        rig.model.on_call = revoke
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"]
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 0
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_correction_after_reflection_targets_opened_reading_log(tmp_path):
    rig = Rig(tmp_path / "correction.sqlite3", _model())
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"]
        await process_jobs(rig.engine)
        assert _count_reflections(rig) == 1
        rig.tick()
        result = enqueue(rig.engine, _incoming(message_id="correction-1", quote="", text="这条见闻不对"))
        assert result["accepted"] is True
        correction = rig.store.db.execute(
            "SELECT log_id FROM corrections WHERE identity=? AND umo=? AND member_id=? ORDER BY created_at DESC LIMIT 1",
            (IDENTITY, UMO, MEMBER),
        ).fetchone()
        assert correction["log_id"] == LOG_ID
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_proactive_intent_must_match_stored_umo_and_recipient(tmp_path):
    rig = Rig(tmp_path / "proactive-scope.sqlite3", _model(opening=False, response=False))
    try:
        _sent_intent(rig.store, umo=UMO, member=MEMBER)
        assert enqueue(rig.engine, _delivery_event(intent_id="horizon:intent-1", umo=OTHER_UMO))["queued"]
        rig.tick()
        other = _incoming(umo=OTHER_UMO, message_id="other-umo", quote="bot-question-1")
        assert enqueue(rig.engine, other)["queued"] is True
        await process_jobs(rig.engine)
        assert len(rig.model.calls) == 1
        assert _count_reflections(rig) == 0
    finally:
        rig.close()
