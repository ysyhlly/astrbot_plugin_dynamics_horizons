from __future__ import annotations

import asyncio
import json

import pytest

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.feedback import enqueue
from astrbot_plugin_dynamics_horizons.core.projection import ProjectionCache
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.adapters import ebook, feed, ssrf
from astrbot_plugin_dynamics_horizons.core.sanitize import sanitize_candidate_item
from astrbot_plugin_dynamics_horizons.tests.test_core_engine import (
    FakeContext,
    FakeWorldPlugin,
    _ts,
)

IDENT = make_identity("alice")
UMO = "qq:GroupMessage:1"


@pytest.fixture
def store(tmp_path):
    value = HorizonsStore(tmp_path / "horizons.sqlite3")
    yield value
    value.close()


def proposal(store, **extra):
    return store.create_proposal(
        identity=IDENT,
        source_kind="manual",
        title="茶文化记录",
        digest="资料介绍了宋代点茶。",
        reflection="对点茶很感兴趣。",
        staged_interest={"topic": "茶文化", "summary": "想了解点茶"},
        questions=[{"topic": "茶文化", "question": "你喜欢哪种茶？"}],
        now=_ts(),
        **extra,
    )


def engine(store, *, mode="llm", result=None, world=None):
    async def model(role, prompt, data):
        if "max_picks" in data:
            candidates = json.loads(
                data["untrusted_candidates_block"].split(">", 1)[1].rsplit("<", 1)[0]
            )
            if candidates:
                return {"selected_candidate_ids": [candidates[0]["candidate_id"]]}
            return (
                {"continue_book_ids": [data["reading_books"][0]["book_id"]]}
                if data["reading_books"]
                else {}
            )
        if role == "review_provider":
            if "delivered_reply" in data:
                return {"questions": [], "topics": []}
            return {"verdict": "approve", "reason": "evidence checked"}
        return (
            result
            if result is not None
            else {
                "digest": "资料介绍宋代点茶。",
                "reflection": "我想了解更多。",
                "continuity": {
                    "summary": "前文谈点茶",
                    "characters": "茶客",
                    "open_questions": "器具的用途",
                },
            }
        )

    ctx = FakeContext(
        stars=[
            {
                "name": "astrbot_plugin_dynamics_world",
                "star": world or FakeWorldPlugin(),
            }
        ]
    )
    return HorizonsEngine(
        store,
        {
            "enabled": True,
            "analysis_provider": "a",
            "review_provider": "r",
            "review_mode": mode,
            "daily_digest_quota": 5,
        },
        model,
        context=ctx,
        clock=_ts,
    )


def book(store):
    return store.create_book_with_chapters(
        identity=IDENT,
        title="茶书",
        chapters=[
            {"text": "第一章讲点茶的方法。" * 15},
            {"text": "第二章讲茶盏的用途。" * 15},
            {"text": "第三章讲茶叶的历史。" * 15},
        ],
        now=_ts(),
    )


@pytest.mark.asyncio
async def test_pending_chapter_does_not_duplicate(store):
    b = book(store)
    eng = engine(store, mode="human")
    await eng.run_digest_cycle(identity=IDENT)
    await eng.run_digest_cycle(identity=IDENT)
    assert store.list_proposals(identity=IDENT)["total"] == 1
    assert store.get_book(b["book_id"])["chapter_cursor"] == 0
    assert store.count_today_digests(IDENT, _ts()) == 1


@pytest.mark.asyncio
async def test_no_note_advances_and_counts_attempt(store):
    b = book(store)
    eng = engine(store, result={"worth_recording": False})
    await eng.run_digest_cycle(identity=IDENT)
    await eng.run_digest_cycle(identity=IDENT)
    assert store.get_book(b["book_id"])["chapter_cursor"] == 2
    assert store.count_today_digests(IDENT, _ts()) == 2
    assert store.list_proposals(identity=IDENT)["total"] == 0


@pytest.mark.asyncio
async def test_out_of_order_candidates_only_offer_next_chapter(store):
    b = book(store)
    src = store.create_source(
        identity=IDENT, source_kind="ebook", name="茶书", config={}, now=_ts()
    )
    for index in range(3):
        store.insert_candidates(
            [
                {
                    "external_id": str(index),
                    "book_id": b["book_id"],
                    "chapter_index": index,
                    "total_chapters": 3,
                    "title": f"章节{index}",
                    "full_text": f"第{index}章测试内容。" * 20,
                }
            ],
            source_id=src["source_id"],
            source_kind="ebook",
            identity=IDENT,
            now=_ts() + index,
        )
    assert [r["chapter_index"] for r in store.list_unread_candidates(IDENT)] == [0]
    await engine(store).run_digest_cycle(identity=IDENT)
    assert store.get_book(b["book_id"])["chapter_cursor"] == 1
    assert store.get_book(b["book_id"])["status"] == "reading"
    assert (
        store.db.execute(
            "SELECT count(*) FROM candidate_items WHERE status='digested'"
        ).fetchone()[0]
        == 1
    )


def test_approved_cannot_be_rejected_and_revocation_is_complete(store):
    p = proposal(store)
    store.approve_proposal_atomic(p["proposal_id"], now=_ts())
    with pytest.raises(ValueError):
        store.record_review_verdict(
            p["proposal_id"],
            state="rejected",
            verdict="reject",
            reason="late",
            now=_ts(),
        )
    store.revoke_proposal(p["proposal_id"], "human:admin", "incorrect", _ts() + 1)
    snapshot = store.build_projection_snapshot(now=_ts() + 2)
    assert not snapshot["by_identity"][IDENT]["logs"]
    assert not snapshot["by_identity"][IDENT]["interests"]
    assert not snapshot["by_identity"][IDENT]["questions"]
    assert (
        store.db.execute("SELECT operation FROM world_outbox").fetchone()[0] == "revoke"
    )
    assert len(store.proposal_audit(p["proposal_id"])) == 2


def test_stale_review_cannot_override_new_revision(store):
    p = proposal(store)
    store.record_review_verdict(
        p["proposal_id"],
        state="held",
        verdict="hold",
        reason="manual",
        expected_revision=p["revision"],
    )
    with pytest.raises(ValueError, match="stale_review"):
        store.approve_proposal_atomic(p["proposal_id"], expected_revision=p["revision"])


def test_unknown_persona_never_borrows_only_identity(store):
    p = proposal(store)
    store.approve_proposal_atomic(p["proposal_id"], now=_ts())
    cache = ProjectionCache()
    cache.update_snapshot(store.build_projection_snapshot(now=_ts()))
    assert cache.resolve_identity(umo=UMO, persona="f" * 64) is None
    assert cache.resolve_identity(umo=UMO, persona="") is None


@pytest.mark.asyncio
async def test_unavailable_world_is_not_awake(store):
    class Broken(FakeWorldPlugin):
        async def get_character_context(self, scope, event=None):
            return {"available": False, "disabled": True}

        async def get_snapshot(self, scope):
            raise RuntimeError("offline")

    wake = await engine(store, world=Broken()).world_bridge.inspect_wake_and_profile(
        IDENT, now=_ts(1)
    )
    assert not wake["awake"] and not wake["world_connected"]


@pytest.mark.asyncio
async def test_wrong_character_is_not_used(store):
    class Other(FakeWorldPlugin):
        async def get_character_context(self, scope, event=None):
            result = await super().get_character_context(scope, event)
            result["scope"]["persona_id"] = "bob"
            result["scope"]["instance_id"] = "bob"
            return result

    wake = await engine(store, world=Other()).world_bridge.inspect_wake_and_profile(
        IDENT, now=_ts()
    )
    assert not wake["awake"]


@pytest.mark.asyncio
async def test_legacy_offered_fields_cannot_create_evidence(store):
    p = proposal(store)
    out = store.approve_proposal_atomic(p["proposal_id"], now=_ts())
    eng = engine(store)
    for i in range(2):
        enqueue(
            eng,
            {
                "kind": "delivery.success",
                "delivered": True,
                "umo": UMO,
                "identity": IDENT,
                "member": "u",
                "text": "今天下雨了",
                "reply_text": "记得带伞",
                "question_ids": out["proposal"]["applied_question_ids"],
                "topics": ["茶文化"],
                "turn_id": str(i),
            },
        )
    await eng.process_feedback_jobs()
    assert (
        store.db.execute("SELECT count(*) FROM question_umo_state").fetchone()[0] == 0
    )
    assert (
        store.db.execute("SELECT count(*) FROM co_discussed_topics").fetchone()[0] == 0
    )


def test_verified_feedback_is_member_scoped_and_idempotent(store):
    p = proposal(store)
    out = store.approve_proposal_atomic(p["proposal_id"], now=_ts())
    qid = out["proposal"]["applied_question_ids"][0]
    kw = dict(
        identity=IDENT,
        umo=UMO,
        member_id="u",
        user_text="我喜欢红茶",
        reply_text="你喜欢哪种茶？",
        delivered=True,
        verified=True,
        offered_question_ids=[qid],
        offered_topics=["茶文化"],
        event_id="same",
        turn_id="same",
        now=_ts(),
    )
    store.record_turn_feedback(**kw)
    store.record_turn_feedback(**kw)
    assert (
        store.db.execute("SELECT turn_count FROM co_discussed_topics").fetchone()[0]
        == 1
    )
    assert store.db.execute("SELECT umo FROM question_umo_state").fetchone()[0] == UMO


def test_member_opt_out_without_delivery(store):
    eng = engine(store)
    enqueue(
        eng,
        {
            "kind": "message.received",
            "umo": UMO,
            "identity": IDENT,
            "member": "u",
            "text": "别主动找我",
            "event_id": "stop",
        },
    )
    assert (
        store.db.execute("SELECT opted_out FROM member_outreach_prefs").fetchone()[0]
        == 1
    )
    assert store.db.execute("SELECT count(*) FROM feedback_jobs").fetchone()[0] == 0


def test_topic_member_preference_not_global(store):
    enqueue(
        engine(store),
        {
            "kind": "message.received",
            "umo": UMO,
            "identity": IDENT,
            "member": "u",
            "text": "不要再给我推荐茶文化",
            "event_id": "topic-stop",
        },
    )
    row = store.db.execute("SELECT * FROM member_topic_prefs").fetchone()
    assert row["topic"] == "茶文化" and row["umo"] == UMO and row["member_id"] == "u"
    assert store.list_blocked_topics(IDENT) == []


def test_local_unblock_preserves_global_rule(store):
    store.block_topic("广告")
    store.block_topic("广告", IDENT)
    store.unblock_topic("广告", IDENT)
    assert store.is_topic_blocked("广告", IDENT)


def test_short_excerpt_does_not_become_full_article():
    raw = feed._parse_json_feed(
        {
            "version": "https://jsonfeed.org/version/1.1",
            "items": [
                {
                    "id": "1",
                    "title": "标题",
                    "url": "https://example.org/article",
                    "content_text": "短摘要。",
                }
            ],
        },
        base_url="https://example.org/",
        source={"source_id": "s"},
        fetch_full_article=True,
    )[0]
    clean = sanitize_candidate_item(
        raw, source_id="s", source_kind="feed", identity=IDENT
    )
    assert not raw["content_ready"] and not clean["content_ready"]


@pytest.mark.asyncio
async def test_opds_does_not_send_credentials_cross_origin():
    seen = []
    ssrf.set_dns_resolver_for_testing(lambda host: ["93.184.216.34"])

    async def transport(url, headers, timeout):
        seen.append(headers)
        return ssrf.HttpResponse(
            status=200,
            body=("测试正文。" * 50).encode(),
            headers={"Content-Type": "text/plain"},
            url=url,
        )

    ssrf.set_http_transport_for_testing(transport)
    try:
        await ebook.fetch_ebook_content(
            {
                "config": {
                    "provider": "opds",
                    "url": "https://library.example.org/catalog",
                    "auth_token": "FAKE_TOKEN",
                }
            },
            {
                "title": "测试书",
                "meta": {"acquisition_url": "https://other.example.net/book.txt"},
            },
        )
        assert all("Authorization" not in h for h in seen)
    finally:
        ssrf.set_http_transport_for_testing(None)
        ssrf.set_dns_resolver_for_testing(None)


@pytest.mark.asyncio
async def test_feed_backlog_drains_despite_etag():
    ssrf.set_dns_resolver_for_testing(lambda host: ["93.184.216.34"])
    calls = []

    async def transport(url, headers, timeout):
        calls.append(headers)
        if headers.get("If-None-Match"):
            return ssrf.HttpResponse(
                status=304, body=b"", headers={"ETag": "v1"}, url=url
            )
        return ssrf.HttpResponse(
            status=200,
            body=json.dumps(
                {
                    "version": "https://jsonfeed.org/version/1.1",
                    "items": [
                        {"id": str(i), "title": str(i), "content_text": str(i)}
                        for i in range(3)
                    ],
                }
            ).encode(),
            headers={"ETag": "v1"},
            url=url,
        )

    ssrf.set_http_transport_for_testing(transport)
    try:
        src = {
            "source_id": "s",
            "config": {"url": "https://example.org/feed", "limit": 1},
        }
        cursor = {}
        items = []
        for _ in range(4):
            result = await feed.poll_feed_source(src, cursor)
            cursor = result["next_cursor"]
            items.extend(result["items"])
        assert len({i["external_id"] for i in items}) == 3 and len(calls) == 2
    finally:
        ssrf.set_http_transport_for_testing(None)
        ssrf.set_dns_resolver_for_testing(None)


def test_archive_preserves_evidence_and_never_deletes(store):
    source = store.create_source(
        identity=IDENT, source_kind="manual", name="source", config={}
    )
    rows = store.insert_candidates(
        [
            {"external_id": str(i), "title": f"历史{i}", "full_text": f"材料{i}内容"}
            for i in range(3)
        ],
        source_id=source["source_id"],
        source_kind="manual",
        identity=IDENT,
        now=_ts() - 100 * 86400,
    )
    proposal(store, candidate_id=rows[0]["candidate_id"])
    store.archive_candidates(_ts())
    assert store.db.execute("SELECT count(*) FROM candidate_items").fetchone()[0] == 3
    assert (
        store.db.execute(
            "SELECT count(*) FROM candidate_items WHERE archived=1"
        ).fetchone()[0]
        == 2
    )


def test_review_retry_schedule_and_exhaustion(store):
    p = proposal(store)
    for delay in (60, 300, 1800):
        store.schedule_review(p["proposal_id"], "retry", _ts(), "timeout")
        assert (
            store.db.execute("SELECT next_at FROM review_jobs").fetchone()[0]
            == _ts() + delay
        )
    store.schedule_review(p["proposal_id"], "retry", _ts(), "timeout")
    assert store.due_reviews(_ts() + 86400) == []


def test_budget_reserves_failed_calls(store):
    cid = store.reserve_call(IDENT, "analysis", 1, _ts())
    store.finish_call(cid, "failed")
    with pytest.raises(ValueError, match="quota"):
        store.reserve_call(IDENT, "analysis", 1, _ts())


def test_migration_repeat_and_interrupted_work(store):
    unit = {"source_id": "", "candidate_id": "c", "book_id": "", "chapter_index": None}
    evidence = {"text": "read", "content_hash": "123"}
    tid = store.claim_reading(IDENT, unit, evidence, 5, _ts())
    store.migrate_lifecycle()
    store.migrate_lifecycle()
    assert store.db.execute("SELECT count(*) FROM hz_migrations").fetchone()[0] == 1
    assert (
        store.db.execute(
            "SELECT status FROM reading_tasks WHERE task_id=?", (tid,)
        ).fetchone()[0]
        == "interrupted"
    )


@pytest.mark.asyncio
async def test_continuity_uses_only_approved_chapters(store):
    b = book(store)
    eng = engine(store)
    await eng.run_digest_cycle(identity=IDENT)
    history = store.continuity(b["book_id"], 1)
    assert (
        len(history["recent_chapters"]) == 1
        and history["cumulative"]["summary"] == "前文谈点茶"
    )
    p = store.list_proposals(identity=IDENT)["items"][0]
    store.revoke_proposal(p["proposal_id"], "human", "bad evidence", _ts())
    assert store.continuity(b["book_id"], 1) == {
        "recent_chapters": [],
        "cumulative": {},
    }


@pytest.mark.asyncio
async def test_identity_change_during_model_call_discarded(store):
    b = book(store)
    eng = engine(store)
    orig = eng.model_caller

    async def changing(role, prompt, data):
        value = await orig(role, prompt, data)
        if "untrusted_media" in data:
            eng.config["analysis_provider"] = "replacement"
        return value

    eng.model_caller = changing
    result = await eng.run_digest_cycle(identity=IDENT)
    assert (
        result["status"] == "error"
        and store.list_proposals(identity=IDENT)["total"] == 0
    )
    assert store.get_book(b["book_id"])["chapter_cursor"] == 0


def test_all_managed_scope_preserves_full_platform_identity(store):
    store.bind_scope("qq1:GroupMessage:1", IDENT, True, _ts())
    store.bind_scope("qq2:GroupMessage:1", IDENT, True, _ts())
    store.bind_scope("qq1:FriendMessage:1", IDENT, True, _ts())
    eng = engine(store)
    eng.config["proactive_scope"] = "all_managed_groups"
    assert set(eng.effective_proactive_sessions()) == {
        "qq1:GroupMessage:1",
        "qq2:GroupMessage:1",
    }


@pytest.mark.asyncio
async def test_thread_io_is_drained_on_cancellation():
    import threading
    from astrbot_plugin_dynamics_horizons.core.io import run_io

    started = threading.Event()
    release = threading.Event()
    finished = []

    def write():
        started.set()
        release.wait(3)
        finished.append(True)

    task = asyncio.create_task(run_io(write))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True]


def test_poll_candidates_and_cursor_roll_back_together(store, monkeypatch):
    source = store.create_source(
        identity=IDENT, source_kind="manual", name="s", config={}
    )

    def fail(*args, **kwargs):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(store, "update_source_poll_state", fail)
    with pytest.raises(RuntimeError):
        store.commit_poll(
            source,
            [{"title": "新的条目", "full_text": "测试材料", "external_id": "new"}],
            {"etag": "new"},
            0,
            "ok",
            _ts(),
        )
    assert store.db.execute("SELECT count(*) FROM candidate_items").fetchone()[0] == 0
    assert store.get_source(source["source_id"])["cursor"] == {}


def test_quoted_opt_out_does_not_change_member_preference(store):
    enqueue(
        engine(store),
        {
            "kind": "message.received",
            "umo": UMO,
            "identity": IDENT,
            "member": "u",
            "text": "他跟我说“别打扰我”，怎么办？",
            "event_id": "quoted",
        },
    )
    assert (
        store.db.execute("SELECT count(*) FROM member_outreach_prefs").fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_world_travel_phase_blocks_reading(store):
    class Travelling(FakeWorldPlugin):
        async def get_snapshot(self, scope):
            result = await super().get_snapshot(scope)
            result["spatial"] = {"phase": "travelling"}
            return result

    wake = await engine(
        store, world=Travelling()
    ).world_bridge.inspect_wake_and_profile(IDENT, now=_ts())
    assert not wake["awake"] and wake["skip_reason"] == "world_in_transit"


def test_explicit_stop_sharing_phrase_is_immediate_and_reversible(store):
    eng = engine(store)
    event = {
        "kind": "message.received",
        "umo": UMO,
        "identity": IDENT,
        "member": "u",
        "text": "别再向我分享",
        "event_id": "stop-sharing",
    }
    enqueue(eng, event)
    assert (
        store.db.execute("SELECT opted_out FROM member_outreach_prefs").fetchone()[0]
        == 1
    )
    enqueue(eng, dict(event, text="恢复向我分享", event_id="resume-sharing"))
    assert (
        store.db.execute("SELECT opted_out FROM member_outreach_prefs").fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_continuity_is_independently_reviewed(store):
    book(store)
    eng = engine(store)
    original = eng.model_caller
    reviewed = []

    async def capture(role, prompt, data):
        if role == "review_provider":
            reviewed.append(data["continuity_evidence"])
        return await original(role, prompt, data)

    eng.model_caller = capture
    result = await eng.run_digest_cycle(identity=IDENT)
    assert result["approved"] == 1
    assert reviewed[0]["proposed"]["summary"] == "前文谈点茶"
    assert reviewed[0]["previous"]["recent_chapters"] == []


@pytest.mark.parametrize("mode", ["llm", "human"])
def test_proposal_queue_survives_restart_and_repairs_legacy_orphan(tmp_path, mode):
    path = tmp_path / "recovery.sqlite3"
    first = HorizonsStore(path)
    p = proposal(first, review_mode=mode)
    assert first.db.execute("SELECT count(*) FROM review_jobs").fetchone()[0] == 1
    with first.lock, first.db:
        first.db.execute("DELETE FROM review_jobs")
    first.close()
    second = HorizonsStore(path)
    try:
        row = second.db.execute("SELECT * FROM review_jobs").fetchone()
        assert row["proposal_id"] == p["proposal_id"]
        assert (p["proposal_id"] in second.due_reviews(_ts() + 120)) == (mode == "llm")
    finally:
        second.close()


@pytest.mark.asyncio
async def test_sleep_defers_review_without_exhausting_retries(store):
    p = proposal(store)
    eng = engine(store, world=FakeWorldPlugin(availability="asleep"))
    for _ in range(5):
        await eng.review_proposal(p["proposal_id"])
    row = store.db.execute("SELECT * FROM review_jobs").fetchone()
    assert (
        row["kind"] == "retry"
        and row["attempts"] == 0
        and row["next_at"] == _ts() + 300
    )
    assert store.db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0


def test_proposal_and_review_job_are_one_transaction(store, monkeypatch):
    original = store.db.execute

    def failed_queue(sql, *args, **kwargs):
        if sql.startswith("INSERT INTO review_jobs"):
            raise RuntimeError("simulated enqueue failure")
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(store.db, "execute", failed_queue)
    with pytest.raises(RuntimeError):
        proposal(store)
    assert original("SELECT count(*) FROM proposals").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_same_provider_requires_explicit_independent_call_policy(store, allowed):
    book(store)
    eng = engine(store)
    eng.config.update(
        analysis_provider="same",
        review_provider="same",
        allow_same_provider_for_review=allowed,
    )
    result = await eng.run_digest_cycle(identity=IDENT)
    assert result.get("approved", 0) == int(allowed)
    if not allowed:
        assert result["skip_reason"] == "independent_review_provider_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "umo", ["qq:FriendMessage:private", "qq:GroupMessage:unmanaged"]
)
async def test_unmanaged_or_private_feedback_scope_does_not_block_global_reading(
    store, umo
):
    class ScopedWorld(FakeWorldPlugin):
        async def get_snapshot(self, scope):
            if scope.get("umo") == umo:
                return {"managed": False, "scope": scope}
            return await super().get_snapshot(scope)

    eng = engine(store, world=ScopedWorld())
    eng.projection.bind_umo_identity(umo, IDENT)
    wake = await eng.world_bridge.inspect_wake_and_profile(IDENT, now=_ts())
    assert wake["awake"] and wake["world_connected"]
