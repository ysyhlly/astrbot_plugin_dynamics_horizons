from __future__ import annotations

import asyncio
import json

import pytest
from bilibili_api import exceptions as bili_exc

from astrbot_plugin_dynamics_horizons.adapters import bilibili, ssrf
from astrbot_plugin_dynamics_horizons.adapters.bilibili import BilibiliRiskControlError
from astrbot_plugin_dynamics_horizons.core.contract import (
    make_identity,
    shanghai_day_bounds,
)
from astrbot_plugin_dynamics_horizons.core.discussion_feedback import queue_received
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.feedback import enqueue, process_jobs
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.tests.test_core_engine import (
    FakeContext,
    _make_plugin,
    _ts,
)

IDENT = make_identity("alice")
UMO = "qq:GroupMessage:1001"


def _make_engine(
    tmp_path,
    *,
    now_ref: list[float] | None = None,
    config: dict | None = None,
    model_fn=None,
    world=None,
) -> tuple[HorizonsStore, HorizonsEngine]:
    clock_ref = now_ref or [_ts(14, 0)]
    store = HorizonsStore(tmp_path / "opt.sqlite3")
    ctx = FakeContext(
        stars=(
            [{"name": "astrbot_plugin_dynamics_world", "activated": True, "star": world}]
            if world is not None
            else []
        )
    )

    async def default_model(role: str, prompt: str, data: dict):
        if model_fn is not None:
            return await model_fn(role, prompt, data)
        return {}

    eng = HorizonsEngine(
        store,
        {
            "enabled": True,
            "sessions": [UMO],
            "proactive_sessions": [UMO],
            "analysis_provider": "a",
            "review_provider": "r",
            "review_mode": "llm",
            "daily_digest_quota": 5,
            "daily_model_call_limit": 20,
            **(config or {}),
        },
        default_model,
        context=ctx,
        clock=lambda: clock_ref[0],
    )
    return store, eng


@pytest.mark.asyncio
async def test_enqueue_skips_projection_refresh_on_irrelevant_turn(tmp_path):
    store, eng = _make_engine(tmp_path)
    try:
        # Insert an active staged_interest whose topic is mentioned in chat
        # WITHOUT any horizons opening/questions/intents — enqueue should NOT match or dirty projection
        with store.lock, store.db:
            store.db.execute(
                """
                INSERT INTO staged_interests(
                    interest_id, identity, topic, summary, heat_score,
                    last_reinforced_at, expires_at, status, evidence_log_ids_json,
                    created_at, updated_at
                ) VALUES ('int_1', ?, '量子计算', 'summary', 1.5, ?, ?, 'active', '[]', ?, ?)
                """,
                (IDENT, _ts(), _ts() + 86400, _ts(), _ts()),
            )
        refreshed: list[str] = []
        eng.refresh_projection_sync = lambda: refreshed.append("refreshed")  # type: ignore[method-assign]

        # Normal delivery without horizons opening context and with unrelated message
        out = enqueue(
            eng,
            {
                "kind": "delivery.success",
                "delivered": True,
                "umo": UMO,
                "identity": IDENT,
                "member": "u1",
                "text": "我们今天聊聊量子计算吧",
                "reply_text": "好呀",
                "event_id": "ev_no_horizons",
                "turn_id": "turn_no_horizons",
                "occurred_at": _ts(),
            },
        )
        assert out["queued"] is False
        assert out["projection_dirty"] is False
        assert refreshed == []

        # Explicit opt-out preference DOES mutate outreach state & dirty projection
        opt_out = enqueue(
            eng,
            {
                "kind": "message.received",
                "umo": UMO,
                "identity": IDENT,
                "member": "u1",
                "text": "别再向我分享",
                "event_id": "ev_opt_out",
                "occurred_at": _ts() + 5,
            },
        )
        assert opt_out["projection_dirty"] is True
        assert (
            store.db.execute(
                "SELECT opted_out FROM member_outreach_prefs WHERE identity=? AND umo=? AND member_id='u1'",
                (IDENT, UMO),
            ).fetchone()[0]
            == 1
        )
    finally:
        store.close()


def test_discussion_feedback_per_opening_cap_and_cooldown(tmp_path):
    now_ref = [_ts(14, 0)]
    store, eng = _make_engine(tmp_path, now_ref=now_ref)
    try:
        anchor = "open_1"
        queued_open = enqueue(
            eng,
            {
                "kind": "delivery.success",
                "delivered": True,
                "umo": UMO,
                "identity": IDENT,
                "member": "u1",
                "log_ids": ["log_1"],
                "topics": ["茶文化"],
                "text": "茶文化很有意思",
                "reply_text": "我也喜欢点茶的绵密汤花。",
                "source_message_id": "msg_src_1",
                "reply_message_ids": ["msg_bot_1"],
                "event_id": anchor,
                "occurred_at": now_ref[0],
            },
        )
        assert queued_open["queued"] is True

        # Unquoted message 1 queues
        u1 = "我也喜欢建盏的兔毫纹理"
        r1 = queue_received(
            eng,
            {
                "kind": "message.received",
                "message_id": "msg_u_1",
                "event_id": "ev_u_1",
                "text": u1,
            },
            identity=IDENT,
            umo=UMO,
            member="u1",
            user=u1,
            now=now_ref[0] + 10,
        )
        assert r1 is True

        # Replay exact same message_id remains accepted/idempotent
        r1_dup = queue_received(
            eng,
            {
                "kind": "message.received",
                "message_id": "msg_u_1",
                "event_id": "ev_u_1_dup",
                "text": u1,
            },
            identity=IDENT,
            umo=UMO,
            member="u1",
            user=u1,
            now=now_ref[0] + 15,
        )
        assert r1_dup is True

        # Second unquoted message within 180s cooldown is suppressed
        u2 = "还有曜变天目也很少见"
        r2_fast = queue_received(
            eng,
            {
                "kind": "message.received",
                "message_id": "msg_u_2",
                "event_id": "ev_u_2",
                "text": u2,
            },
            identity=IDENT,
            umo=UMO,
            member="u1",
            user=u2,
            now=now_ref[0] + 60,
        )
        assert r2_fast is False

        # After cooldown (>180s), second unquoted queues (total=2)
        now_ref[0] += 200
        r2_ok = queue_received(
            eng,
            {
                "kind": "message.received",
                "message_id": "msg_u_2b",
                "event_id": "ev_u_2b",
                "text": u2,
            },
            identity=IDENT,
            umo=UMO,
            member="u1",
            user=u2,
            now=now_ref[0],
        )
        assert r2_ok is True

        # Third unquoted exceeds max 2 unquoted per opening
        now_ref[0] += 250
        u3_unq = "第三条未引用消息也很想讨论"
        r3_unquoted = queue_received(
            eng,
            {
                "kind": "message.received",
                "message_id": "msg_u_3_unq",
                "event_id": "ev_u_3_unq",
                "text": u3_unq,
            },
            identity=IDENT,
            umo=UMO,
            member="u1",
            user=u3_unq,
            now=now_ref[0],
        )
        assert r3_unquoted is False

        # Quoted reply still gets the 3rd total slot (total=3)
        u3_q = "对呀我也这么觉得"
        r3_quoted = queue_received(
            eng,
            {
                "kind": "message.received",
                "reply_to_message_id": "msg_bot_1",
                "message_id": "msg_u_3_q",
                "event_id": "ev_u_3_q",
                "text": u3_q,
            },
            identity=IDENT,
            umo=UMO,
            member="u1",
            user=u3_q,
            now=now_ref[0] + 10,
        )
        assert r3_quoted is True

        # 4th message on same opening hits max_per_opening=3 cap
        u4 = "第四条超出上限"
        r4_over_cap = queue_received(
            eng,
            {
                "kind": "message.received",
                "reply_to_message_id": "msg_bot_1",
                "message_id": "msg_u_4",
                "event_id": "ev_u_4",
                "text": u4,
            },
            identity=IDENT,
            umo=UMO,
            member="u1",
            user=u4,
            now=now_ref[0] + 20,
        )
        assert r4_over_cap is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_quota_lane_split_and_next_day_deferral(tmp_path):
    now_ref = [_ts(14, 0)]
    store, eng = _make_engine(
        tmp_path,
        now_ref=now_ref,
        config={"daily_model_call_quota": 8},
    )
    try:
        # 8 total -> reserve >= 4 for reading, up to 4 for feedback
        for _ in range(4):
            store.reserve_call(IDENT, "feedback_classify", 8, now_ref[0], lane="feedback")
        with pytest.raises(ValueError, match="feedback_model_call_quota_reached"):
            store.reserve_call(IDENT, "feedback_classify", 8, now_ref[0], lane="feedback")
        # Reading lane can still reserve calls using its reserved share
        cid = store.reserve_call(IDENT, "analysis_provider", 8, now_ref[0], lane="reading")
        assert cid

        # Enqueue feedback job while feedback quota is exhausted -> deferred to next Shanghai day, attempts=0
        enqueue(
            eng,
            {
                "kind": "delivery.success",
                "delivered": True,
                "umo": UMO,
                "identity": IDENT,
                "member": "u1",
                "text": "你最近看了什么推理作品",
                "reply_text": "刚看了一篇古典本格",
                "question_ids": ["q_1"],
                "event_id": "ev_fb_quota",
                "turn_id": "turn_fb_quota",
                "occurred_at": now_ref[0],
            },
        )
        await process_jobs(eng)
        job = store.db.execute(
            "SELECT * FROM feedback_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        _, next_day_start = shanghai_day_bounds(now_ref[0])
        assert job["status"] == "pending"
        assert job["attempts"] == 0
        assert job["next_at"] == next_day_start + 60.0

        # Proposal review hitting daily_model_call_quota_reached -> deferred to next day, not marked exhausted/held
        for _ in range(3):
            store.reserve_call(IDENT, "analysis_provider", 8, now_ref[0], lane="reading")
        p = store.create_proposal(
            identity=IDENT,
            source_kind="manual",
            title="quota test",
            digest="digest body",
            reflection="refl",
            now=now_ref[0],
        )
        await eng.review_proposal(p["proposal_id"])
        rj = store.db.execute(
            "SELECT * FROM review_jobs WHERE proposal_id=?", (p["proposal_id"],)
        ).fetchone()
        assert rj["kind"] == "retry"
        assert rj["attempts"] == 0
        assert rj["next_at"] == next_day_start + 60.0
        assert rj["last_error"] == "daily_model_call_quota_reached"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_bilibili_stage2_risk_control_backoff_and_circuit_breaker(tmp_path, monkeypatch):
    now_ref = [_ts(14, 0)]
    store, eng = _make_engine(tmp_path, now_ref=now_ref)
    try:
        s1 = store.create_source(
            identity=IDENT,
            source_kind="bilibili",
            name="B站主号",
            config={"mode": "popular"},
            now=now_ref[0],
        )
        s2 = store.create_source(
            identity=IDENT,
            source_kind="bilibili",
            name="B站副号",
            config={"mode": "recommend"},
            now=now_ref[0],
        )
        store.insert_candidates(
            [
                {
                    "external_id": "bili:BV1xx411c7mD",
                    "source_id": s1["source_id"],
                    "identity": IDENT,
                    "source_kind": "bilibili",
                    "title": "测试视频",
                    "bvid": "BV1xx411c7mD",
                    "summary": "原始简介",
                    "raw_url": "https://www.bilibili.com/video/BV1xx411c7mD",
                    "content_ready": False,
                }
            ],
            source_id=s1["source_id"],
            source_kind="bilibili",
            identity=IDENT,
            now=now_ref[0],
        )
        cand = store.list_unread_candidates(IDENT, limit=1)[0]

        class _RiskVideo:
            def __init__(self, bvid: str, credential=None):
                self.bvid = bvid

            async def get_info(self):
                raise bili_exc.ResponseCodeException(-412, "风控拦截", {})

            async def get_ai_conclusion(self, cid):
                return {}

            async def get_subtitle(self, cid):
                return {}

        monkeypatch.setattr(bilibili.video, "Video", _RiskVideo)

        # Adapter stage 2 MUST raise BilibiliRiskControlError rather than degrading
        with pytest.raises(BilibiliRiskControlError):
            await bilibili.fetch_bilibili_content(s1, cand, timeout=5.0)

        # Engine _stage2_deep_read_unit catches Stage 2 risk control, backs off s1 & domain, leaves candidate content_ready=0
        res = await eng._stage2_deep_read_unit(
            identity=IDENT,
            wake={"awake": True, "scope": {"persona_id": "alice", "instance_id": "alice"}},
            interests=[],
            blocked_topics=[],
            unit_kind="candidate",
            unit_obj=cand,
        )
        assert res is None
        s1_after = store.get_source(s1["source_id"], mask=True)
        assert s1_after["cooldown_until"] > now_ref[0]
        assert s1_after["cursor"]["backoff_step"] >= 1
        assert s1_after["status"].startswith("risk_control_backoff:")
        cand_after = store.db.execute(
            "SELECT content_ready, status FROM candidate_items WHERE candidate_id=?",
            (cand["candidate_id"],),
        ).fetchone()
        assert cand_after["content_ready"] == 0
        assert cand_after["status"] == "unread"

        # Second bilibili source skips network poll during domain cooldown circuit breaker
        called_s2: list[str] = []

        async def fail_if_called(source, cursor, *, timeout=20.0):
            called_s2.append(source["source_id"])
            return {
                "items": [],
                "next_cursor": cursor or {},
                "cooldown_until": 0.0,
                "status": "ok",
                "outcome": "success",
                "error": "",
            }

        eng.poll_source_adapter = fail_if_called  # type: ignore[method-assign]
        poll_s2 = await eng.poll_one_source(s2["source_id"], expected_identity=IDENT)
        assert poll_s2["status"] == "domain_cooldown_circuit_breaker"
        assert poll_s2["outcome"] == "cooldown"
        assert called_s2 == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ssrf_dns_lru_cache_proxy_validation_and_session_scope(monkeypatch):
    ssrf._PUBLIC_DNS_CACHE.clear()
    calls: list[str] = []

    class _FakeResp:
        def __init__(self, url: str, body: bytes):
            self._url = url
            self._body = body

        def geturl(self):
            return self._url

        def read(self, n: int):
            return self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakeOpener:
        def open(self, req, timeout=4):
            calls.append(req.full_url)
            payload = json.dumps(
                {"Status": 0, "Answer": [{"type": 1, "TTL": 120, "data": "93.184.216.34"}]}
            ).encode()
            return _FakeResp(req.full_url, payload)

    monkeypatch.setattr(ssrf, "build_opener", lambda *a, **k: _FakeOpener())
    monkeypatch.setenv("DYNAMICS_HORIZONS_DOH_ENDPOINTS", "https://bad.domain/dns-query,https://9.9.9.9/dns-query")
    assert ssrf._doh_endpoints() == ("https://9.9.9.9/dns-query",)

    for i in range(258):
        ssrf._resolve_public_dns(f"host-{i}.example.org")
    # LRU keeps newest 256 instead of clearing the entire cache
    assert len(ssrf._PUBLIC_DNS_CACHE) == 256
    assert "host-0.example.org" not in ssrf._PUBLIC_DNS_CACHE
    assert "host-257.example.org" in ssrf._PUBLIC_DNS_CACHE

    # Hitting cached host-257 uses LRU cache without calling opener again
    before_calls = len(calls)
    assert ssrf._resolve_public_dns("host-257.example.org") == ["93.184.216.34"]
    assert len(calls) == before_calls

    assert ssrf._resolve_proxy_url("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf._resolve_proxy_url("socks5://127.0.0.1:7890")

    async with ssrf.http_session_scope() as pool:
        assert isinstance(pool, dict)
    ssrf._PUBLIC_DNS_CACHE.clear()


def test_archive_clears_full_text_and_prune_terminal_records(tmp_path):
    now = _ts(14, 0, day=15)
    old = now - 45 * 86400
    recent = now - 2 * 86400
    store = HorizonsStore(tmp_path / "gc.sqlite3")
    try:
        src = store.create_source(
            identity=IDENT,
            source_kind="feed",
            name="feed",
            config={"url": "https://example.org/rss"},
            now=old,
        )
        store.insert_candidates(
            [
                {
                    "external_id": "old_c",
                    "source_id": src["source_id"],
                    "identity": IDENT,
                    "source_kind": "feed",
                    "title": "旧文章",
                    "summary": "s",
                    "full_text": "X" * 500,
                },
                {
                    "external_id": "new_c",
                    "source_id": src["source_id"],
                    "identity": IDENT,
                    "source_kind": "feed",
                    "title": "新文章",
                    "summary": "s2",
                    "full_text": "KEEP_ME",
                },
            ],
            source_id=src["source_id"],
            source_kind="feed",
            identity=IDENT,
            now=old,
        )
        with store.lock, store.db:
            store.db.execute(
                "UPDATE candidate_items SET created_at=? WHERE external_id='old_c'",
                (old,),
            )
            store.db.execute(
                "UPDATE candidate_items SET created_at=? WHERE external_id='new_c'",
                (recent,),
            )
            store.db.execute(
                "INSERT INTO feedback_jobs VALUES('fj_old',?,?, 'u1', '{}', 'done', 0, 0, ?)",
                (IDENT, UMO, old),
            )
            store.db.execute(
                "INSERT INTO feedback_jobs VALUES('fj_new',?,?, 'u1', '{}', 'done', 0, 0, ?)",
                (IDENT, UMO, recent),
            )
            store.db.execute("INSERT INTO feedback_receipts VALUES('fr_old', ?)", (old,))
            store.db.execute("INSERT INTO feedback_receipts VALUES('fr_new', ?)", (recent,))
            store.db.execute(
                "INSERT INTO reading_attempts VALUES('ra_old', 't1', ?, ?)",
                (IDENT, old),
            )
            store.db.execute(
                "INSERT INTO model_calls VALUES('mc_old', ?, 'analysis', 'ok', ?)",
                (IDENT, old),
            )
            store.db.execute(
                "INSERT INTO world_outbox VALUES('wo_old', ?, 1, 'add', '{}', 'done', 1, ?, '')",
                (IDENT, old),
            )
            store.db.execute(
                "INSERT INTO horizon_intents VALUES('hi_old', ?, ?, 'u1', 't', 'log_1', 'sent', '{}', ?, ?, ?, ?)",
                (IDENT, UMO, old, old, old, old),
            )
        shelf = store.create_book_with_chapters(
            identity=IDENT,
            title="完结旧书",
            chapters=[{"label": "Ch1", "text": "正文" * 200}],
            now=old,
        )
        store.update_book_status(shelf["book_id"], "finished", now=old)

        store.archive_candidates(now, days=30, limit=500)
        old_row = store.db.execute(
            "SELECT archived, full_text FROM candidate_items WHERE external_id='old_c'"
        ).fetchone()
        new_row = store.db.execute(
            "SELECT archived, full_text FROM candidate_items WHERE external_id='new_c'"
        ).fetchone()
        assert old_row["archived"] == 1 and old_row["full_text"] == ""
        assert new_row["archived"] == 0 and new_row["full_text"] == "KEEP_ME"

        gc_stats = store.prune_terminal_records(now, retention_days=30, attempt_days=30)
        assert gc_stats["feedback_jobs"] == 1
        assert gc_stats["feedback_receipts"] == 1
        assert gc_stats["reading_attempts"] == 1
        assert gc_stats["model_calls"] == 1
        assert gc_stats["world_outbox"] == 1
        assert gc_stats["horizon_intents"] == 1
        assert gc_stats["book_chapters"] == 1
        assert store.db.execute("SELECT count(*) FROM feedback_jobs").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM feedback_receipts").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_feed_overflow_ingested_to_candidates_not_cursor_json(tmp_path):
    now_ref = [_ts(14, 0)]
    store, eng = _make_engine(tmp_path, now_ref=now_ref)
    try:
        src = store.create_source(
            identity=IDENT,
            source_kind="feed",
            name="RSS Test",
            config={"url": "https://example.org/feed.json", "limit": 2},
            now=now_ref[0],
        )

        async def mock_feed(url: str, headers: dict, timeout: float):
            return ssrf.HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "version": "https://jsonfeed.org/version/1.1",
                        "items": [
                            {"id": f"item-{i}", "title": f"条目 {i}", "content_text": f"内容 {i}" * 30}
                            for i in range(6)
                        ],
                    }
                ).encode(),
                url=url,
            )

        ssrf.set_http_transport_for_testing(mock_feed)
        try:
            out = await eng.poll_one_source(src["source_id"], expected_identity=IDENT)
        finally:
            ssrf.set_http_transport_for_testing(None)

        assert out["polled"] is True
        assert out["added"] == 6
        updated_src = store.get_source(src["source_id"], mask=True)
        assert updated_src["cursor"]["pending_items"] == []
        unread_cnt = store.db.execute(
            "SELECT count(*) FROM candidate_items WHERE source_id=? AND status='unread'",
            (src["source_id"],),
        ).fetchone()[0]
        assert unread_cnt == 6
    finally:
        store.close()


def test_escape_like_due_intents_sql_filter_and_batched_queries(tmp_path):
    now = _ts(14, 0)
    store = HorizonsStore(tmp_path / "queries.sqlite3")
    try:
        # 1) _escape_like prevents wildcard overblocking
        src = store.create_source(
            identity=IDENT, source_kind="manual", name="m", config={}, now=now
        )
        store.insert_candidates(
            [
                {"external_id": "match", "source_id": src["source_id"], "identity": IDENT, "source_kind": "manual", "title": "关于100%_test的讨论", "summary": "literal"},
                {"external_id": "no_match", "source_id": src["source_id"], "identity": IDENT, "source_kind": "manual", "title": "关于100XXXXtest的讨论", "summary": "not literal"},
            ],
            source_id=src["source_id"],
            source_kind="manual",
            identity=IDENT,
            now=now,
        )
        store.block_topic("100%_test", IDENT, now=now)
        statuses = {
            r["external_id"]: r["status"]
            for r in store.db.execute("SELECT external_id, status FROM candidate_items").fetchall()
        }
        assert statuses["match"] == "blocked"
        assert statuses["no_match"] == "unread"

        # 2) list_due_intents filters unmanaged UMOs in SQL BEFORE LIMIT 50
        p = store.create_proposal(
            identity=IDENT,
            source_kind="manual",
            title="t",
            digest="摘要内容",
            reflection="感想",
            staged_interest={"topic": "推理", "summary": "s"},
            now=now,
        )
        store.approve_proposal_atomic(p["proposal_id"], now=now)
        log_id = store.list_logs(identity=IDENT)["items"][0]["log_id"]

        with store.lock, store.db:
            for i in range(65):
                payload = json.dumps({"intent_id": f"horizon:unmanaged_{i}", "umo": "qq:GroupMessage:other"})
                store.db.execute(
                    "INSERT INTO horizon_intents VALUES(?,?,?,?,?,?, 'pending', ?,?,?,?,?)",
                    (f"horizon:unmanaged_{i}", IDENT, "qq:GroupMessage:other", f"u_{i}", "推理", log_id, payload, now - 10, now + 3600, now - 10, now - 10),
                )
            good_payload = json.dumps({"intent_id": "horizon:managed_1", "umo": UMO, "recipient_user_id": "m_good", "topic": "推理", "log_id": log_id})
            store.db.execute(
                "INSERT INTO horizon_intents VALUES('horizon:managed_1', ?, ?, 'm_good', '推理', ?, 'pending', ?, ?, ?, ?, ?)",
                (IDENT, UMO, log_id, good_payload, now + 5, now + 3600, now, now),
            )
        due = store.list_due_intents(now + 10, proactive_sessions=[UMO], quiet_hours="00:00-08:00")
        assert [d["intent_id"] for d in due] == ["horizon:managed_1"]

        # 3) Batch list_proposals & list_logs execute accurately
        props = store.list_proposals(identity=IDENT)
        assert props["total"] == 1
        assert props["items"][0]["audit"]
        logs = store.list_logs(identity=IDENT)
        assert logs["total"] == 1
        assert logs["items"][0]["discussion_reflections"] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_world_bridge_disabled_or_unready_graceful_fallback(tmp_path):
    store, eng = _make_engine(tmp_path)

    class DisabledWorld:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "provider_id": "astrbot_plugin_dynamics_world", "enabled": False, "ready": False}

    class UnreadyWorld:
        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "provider_id": "astrbot_plugin_dynamics_world", "enabled": True, "ready": False}

    eng.context.stars.append(
        {"name": "astrbot_plugin_dynamics_world", "activated": True, "star": DisabledWorld()}
    )
    try:
        wake = await eng.world_bridge.inspect_wake_and_profile(IDENT, now=_ts(14, 0))
        assert wake["awake"] is True
        assert wake["world_connected"] is False
        assert wake["skip_reason"] == ""
        assert "爱丽丝" in wake["character_prompt"]

        eng.context.stars[0]["star"] = UnreadyWorld()
        wake_unready = await eng.world_bridge.inspect_wake_and_profile(IDENT, now=_ts(14, 0))
        assert wake_unready["awake"] is True
        assert wake_unready["world_connected"] is False
        assert wake_unready["skip_reason"] == ""
    finally:
        store.close()


@pytest.mark.asyncio
async def test_overview_get_is_read_only(tmp_path, monkeypatch):
    plugin = await _make_plugin(tmp_path)
    try:
        api = plugin.web_api
        store = plugin.engine.store
        with store.lock, store.db:
            store.db.execute(
                """
                INSERT INTO staged_interests(
                    interest_id, identity, topic, summary, heat_score,
                    last_reinforced_at, expires_at, status, evidence_log_ids_json,
                    created_at, updated_at
                ) VALUES ('int_ro', ?, '点茶', 's', 1.5, ?, ?, 'active', '[]', ?, ?)
                """,
                (IDENT, _ts() - 20 * 86400, _ts() + 86400, _ts() - 20 * 86400, _ts() - 20 * 86400),
            )
        import astrbot_plugin_dynamics_horizons.core.web_api as web_api_mod

        class _FakeReq:
            args = {"identity": IDENT}
            headers = {}

            def get_json(self, force=True, silent=True):
                return {}

        async def _fake_auth():
            return "admin"

        monkeypatch.setattr(web_api_mod, "request", _FakeReq())
        monkeypatch.setattr(
            web_api_mod,
            "json_response",
            lambda body, status_code=200: (body, status_code),
        )
        monkeypatch.setattr(api, "_authorize", _fake_auth)

        # Cancel background loops started by initialize() so we observe ONLY overview_get() SQLite writes
        for t in list(plugin.tasks):
            t.cancel()
        if plugin.tasks:
            await asyncio.gather(*plugin.tasks, return_exceptions=True)
        plugin.tasks.clear()

        changes_before = store.db.total_changes
        resp, status = await api.overview_get()
        assert status == 200
        assert resp["status"] == "ok"
        assert resp["data"]["active_identity"] == IDENT
        assert store.db.total_changes == changes_before
    finally:
        await plugin.terminate()
