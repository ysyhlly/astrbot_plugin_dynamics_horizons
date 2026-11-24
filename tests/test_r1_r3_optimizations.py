"""Focused unit tests for R1 (P0 circuit/backoff/batch-break) and R3 (P1 video budget/prompt cache/version)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_dynamics_horizons import main as main_mod
from astrbot_plugin_dynamics_horizons import series_panels
from astrbot_plugin_dynamics_horizons.adapters import video_media as vm
from astrbot_plugin_dynamics_horizons.core import engine as engine_mod
from astrbot_plugin_dynamics_horizons.core import feedback as feedback_mod
from astrbot_plugin_dynamics_horizons.core import runtime as runtime_mod
from astrbot_plugin_dynamics_horizons.core.contract import (
    PLUGIN_VERSION,
    encode_json,
    make_identity,
)
from astrbot_plugin_dynamics_horizons.core.discussion_feedback import DiscussionModelError
from astrbot_plugin_dynamics_horizons.core.engine import (
    STAGE1_TRIAGE_PROMPT,
    STAGE2_DEEP_READ_PROMPT,
    STAGE3_REVIEW_PROMPT,
    STAGE12_MERGED_DIGEST_PROMPT,
    HorizonsEngine,
    build_persona_system_prompt,
)
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.main import (
    Main,
    build_cache_anchor_contexts,
    extract_persona_system_prompt,
)
from astrbot_plugin_dynamics_horizons.series_panels import ProviderBudgetBusyError
from astrbot_plugin_dynamics_horizons.tests.test_core_engine import (
    FakeContext,
    FakeWorldPlugin,
    _ts,
)

IDENT_A = make_identity("persona_a", "persona_a")
IDENT_B = make_identity("persona_b", "persona_b")


class _StructuredBudgetBusyError(ProviderBudgetBusyError):
    """Simulates synced ProviderBudgetBusyError carrying structured retry_after."""

    def __init__(self, message: str = "budget_circuit_open", *, retry_after: float = 0.0):
        super().__init__(message, provider="p_rev", purpose="review", reason="circuit_open")
        self.retry_after = retry_after


class _CustomWorldPlugin(FakeWorldPlugin):
    def __init__(
        self,
        *,
        character_name: str = "爱丽丝",
        character_prompt: str = "爱丽丝性格冷静克制，热爱古典推理与茶文化，讨厌浮夸营销。",
        availability: str = "available",
    ):
        super().__init__(availability=availability)
        self._character_name = character_name
        self._character_prompt = character_prompt

    async def get_character_context(self, scope, event=None):
        res = await super().get_character_context(scope, event=event)
        res["name"] = self._character_name
        res["core_prompt"] = self._character_prompt
        return res


def _make_engine(
    tmp_path: Path,
    model_caller,
    *,
    now_ref: list[float] | None = None,
    config: dict | None = None,
    world: FakeWorldPlugin | None = None,
    context=None,
) -> tuple[HorizonsStore, HorizonsEngine, list[float]]:
    clock_box = now_ref if now_ref is not None else [_ts(14, 0)]
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    cfg = {
        "enabled": True,
        "analysis_provider": "prov_analysis",
        "video_provider": "prov_video",
        "review_provider": "prov_review",
        "video_audio_stt_provider": "prov_stt",
        "allow_same_provider_for_review": False,
        "review_mode": "llm",
        "daily_digest_quota": 5,
        "daily_model_call_quota": 50,
    }
    if config:
        cfg.update(config)
    world_inst = world or _CustomWorldPlugin()
    if context is not None:
        ctx = context
    else:
        ctx = FakeContext(
            stars=[
                {
                    "name": "astrbot_plugin_dynamics_world",
                    "activated": True,
                    "star": world_inst,
                }
            ]
        )
    eng = HorizonsEngine(
        store,
        cfg,
        model_caller,
        context=ctx,
        clock=lambda: clock_box[0],
    )
    return store, eng, clock_box


def _insert_pending_feedback_job(
    store: HorizonsStore,
    event_id: str,
    *,
    now: float,
    identity: str = IDENT_A,
    kind: str = "delivery",
    extra_payload: dict | None = None,
) -> dict:
    payload = {
        "kind": kind,
        "user_text": "我们聊聊宋代点茶吧",
        "reply_text": "宋代点茶用建盏兔毫纹特别讲究呢",
        "question_ids": [],
        "log_ids": [],
        "topics": ["宋代点茶"],
        "turn_id": f"turn_{event_id}",
        "proactive": False,
        "intent_id": "",
        "source_message_id": f"msg_{event_id}",
        "reply_message_ids": [],
    }
    if extra_payload:
        payload.update(extra_payload)
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO feedback_jobs(event_id,identity,umo,member_id,payload_json,status,attempts,next_at,created_at) "
            "VALUES(?,?,?,?,?,'pending',0,?,?)",
            (event_id, identity, "g:GroupMessage:100", "u1", encode_json(payload), now - 5.0, now - 10.0),
        )
        row = store.db.execute(
            "SELECT * FROM feedback_jobs WHERE event_id=?", (event_id,)
        ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# 1. Defensive circuit helper & retry_after extraction
# ---------------------------------------------------------------------------


def test_defensive_circuit_remaining_and_retry_after(monkeypatch):
    ctx = SimpleNamespace()
    # When series_panels does not yet define provider_circuit_remaining -> safely returns 0.0
    monkeypatch.delattr(series_panels, "provider_circuit_remaining", raising=False)
    assert runtime_mod.provider_circuit_remaining(ctx, "prov_a") == 0.0
    assert runtime_mod.provider_circuit_remaining(None, "prov_a") == 0.0
    assert runtime_mod.provider_circuit_remaining(ctx, "") == 0.0

    # When synced series_panels exposes provider_circuit_remaining -> returns value
    monkeypatch.setattr(
        series_panels,
        "provider_circuit_remaining",
        lambda _c, pid: 185.5 if pid == "prov_a" else 0.0,
        raising=False,
    )
    assert runtime_mod.provider_circuit_remaining(ctx, "prov_a") == 185.5
    assert runtime_mod.provider_circuit_remaining(ctx, "prov_b") == 0.0

    # Structured retry_after respected with floor
    assert runtime_mod.busy_retry_after(_StructuredBudgetBusyError(retry_after=240.0)) == 240.0
    assert runtime_mod.busy_retry_after(_StructuredBudgetBusyError(retry_after=30.0)) == 120.0
    assert runtime_mod.busy_retry_after(ProviderBudgetBusyError("busy")) == 120.0


# ---------------------------------------------------------------------------
# 2. Pre-flight circuit checks: due_reviews, feedback_jobs, run_digest_cycle, video
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_preflight_skips_reviews_and_feedback_jobs(tmp_path: Path, monkeypatch):
    calls: list[tuple] = []

    async def model(role, instruction, payload):
        calls.append((role, instruction))
        return {"verdict": "approve", "topics": [], "questions": [], "openings": []}

    store, eng, clock_box = _make_engine(tmp_path, model)
    try:
        p = store.create_proposal(
            identity=IDENT_A,
            source_kind="manual",
            title="预检提案",
            digest="摘要内容。",
            reflection="随感。",
            now=clock_box[0] - 120.0,
        )
        _insert_pending_feedback_job(store, "fb_preflight_1", now=clock_box[0])

        monkeypatch.setattr(
            series_panels,
            "provider_circuit_remaining",
            lambda _c, pid: 150.0 if pid == "prov_review" else 0.0,
            raising=False,
        )

        await eng.process_maintenance()
        assert calls == []
        # Proposal and feedback job remain pending without attempts burned
        rj = store.db.execute(
            "SELECT * FROM review_jobs WHERE proposal_id=?", (p["proposal_id"],)
        ).fetchone()
        fj = store.db.execute(
            "SELECT * FROM feedback_jobs WHERE event_id='fb_preflight_1'"
        ).fetchone()
        assert rj["kind"] == "retry" and rj["attempts"] == 0
        assert fj["status"] == "pending" and fj["attempts"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_circuit_preflight_skips_digest_cycle_and_video_download(
    tmp_path: Path, monkeypatch
):
    model_calls: list[str] = []
    fetch_calls: list[str] = []

    async def model(role, instruction, payload, media_payload=None):
        model_calls.append(role)
        return {"worth_recording": True, "digest": "x", "reflection": "y"}

    async def fake_fetch(source, candidate, **kwargs):
        fetch_calls.append(candidate["candidate_id"])
        return {"full_text": "should not run", "content_origin": "bilibili_subtitle"}

    store, eng, clock_box = _make_engine(tmp_path, model)
    try:
        src = store.create_source(
            identity=IDENT_A,
            source_kind="bilibili",
            name="B站源",
            config={"mode": "up", "uid": "12345"},
            now=clock_box[0],
        )
        inserted = store.insert_candidates(
            [
                {
                    "external_id": "BV1preflight01",
                    "bvid": "BV1preflight01",
                    "title": "熔断预检视频",
                    "creator": "UP",
                    "summary": "简介",
                    "full_text": "",
                    "content_ready": False,
                    "raw_url": "https://www.bilibili.com/video/BV1preflight01",
                }
            ],
            source_id=src["source_id"],
            source_kind="bilibili",
            identity=IDENT_A,
            now=clock_box[0],
        )
        monkeypatch.setattr(engine_mod, "fetch_item_content", fake_fetch)

        # (A) When analysis_provider circuit is open -> run_digest_cycle returns deferred immediately
        monkeypatch.setattr(
            series_panels,
            "provider_circuit_remaining",
            lambda _c, pid: 95.0 if pid == "prov_analysis" else 0.0,
            raising=False,
        )
        res = await eng.run_digest_cycle(identity=IDENT_A)
        assert res["status"] == "deferred"
        assert res["skip_reason"] == "budget_circuit_open"
        assert res["retry_after"] == 95.0
        assert model_calls == []
        assert fetch_calls == []

        # (B) When analysis_provider is healthy but video_provider circuit is open ->
        #     video download/acquire_video_media is skipped and candidate stays unread!
        monkeypatch.setattr(
            series_panels,
            "provider_circuit_remaining",
            lambda _c, pid: 180.0 if pid == "prov_video" else 0.0,
            raising=False,
        )
        res2 = await eng.run_digest_cycle(identity=IDENT_A)
        assert res2["proposals_created"] == 0
        assert fetch_calls == []
        assert model_calls == []
        cand = store.db.execute(
            "SELECT status FROM candidate_items WHERE candidate_id=?",
            (inserted[0]["candidate_id"],),
        ).fetchone()
        assert cand["status"] == "unread"

        # (C) STT provider circuit open -> _build_audio_transcriber returns None
        eng.context.get_provider_by_id = lambda pid: SimpleNamespace(
            get_text=lambda path: "hi"
        )
        monkeypatch.setattr(
            series_panels,
            "provider_circuit_remaining",
            lambda _c, pid: 60.0 if pid == "prov_stt" else 0.0,
            raising=False,
        )
        assert eng._build_audio_transcriber() is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. Batch break + reschedule on ProviderBudgetBusyError (reviews, feedback, digest)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_due_reviews_breaks_batch_and_reschedules_remaining(tmp_path: Path):
    review_calls: list[str] = []

    async def model(role, instruction, payload):
        review_calls.append(payload["proposal"]["title"])
        raise _StructuredBudgetBusyError("circuit open", retry_after=240.0)

    store, eng, clock_box = _make_engine(tmp_path, model)
    try:
        pids = []
        for idx in range(3):
            p = store.create_proposal(
                identity=IDENT_A,
                source_kind="manual",
                title=f"提案-{idx}",
                digest=f"摘要内容 {idx}",
                reflection=f"随感 {idx}",
                now=clock_box[0] - 120.0 + idx,
            )
            pids.append(p["proposal_id"])

        await eng.process_maintenance()

        # Only item 1 was attempted before immediately breaking the batch
        assert review_calls == ["提案-0"]

        rows = {
            r["proposal_id"]: dict(r)
            for r in store.db.execute("SELECT * FROM review_jobs").fetchall()
        }
        # Item 1 deferred with retry_after=240s and defer_count=1, attempts still 0
        assert rows[pids[0]]["kind"] == "retry"
        assert rows[pids[0]]["attempts"] == 0
        assert rows[pids[0]]["defer_count"] == 1
        assert rows[pids[0]]["next_at"] == pytest.approx(clock_box[0] + 240.0)
        # Remaining items rescheduled to now + 240s without burning attempts or defer_count
        for pid in pids[1:]:
            assert rows[pid]["kind"] == "retry"
            assert rows[pid]["attempts"] == 0
            assert rows[pid]["defer_count"] == 0
            assert rows[pid]["next_at"] == pytest.approx(clock_box[0] + 240.0)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_feedback_jobs_breaks_batch_and_reschedules_remaining(tmp_path: Path):
    fb_calls: list[str] = []

    async def model(role, instruction, payload):
        fb_calls.append(payload["member_text"])
        raise _StructuredBudgetBusyError("circuit open", retry_after=180.0)

    store, eng, clock_box = _make_engine(tmp_path, model)
    try:
        for i in range(3):
            _insert_pending_feedback_job(
                store,
                f"fb_batch_{i}",
                now=clock_box[0] + i,
                extra_payload={"user_text": f"话题消息-{i}"},
            )
        # Ensure all 3 are due at clock_box[0] + 10
        clock_box[0] += 10.0

        await feedback_mod.process_jobs(eng)

        # Immediately broke after item 1!
        assert fb_calls == ["话题消息-0"]

        rows = {
            r["event_id"]: dict(r)
            for r in store.db.execute("SELECT * FROM feedback_jobs").fetchall()
        }
        # Item 1: deferred with attempts=0, status='pending', budget_busy_defers=1, next_at=now+180
        assert rows["fb_batch_0"]["status"] == "pending"
        assert rows["fb_batch_0"]["attempts"] == 0
        assert rows["fb_batch_0"]["next_at"] == pytest.approx(clock_box[0] + 180.0)
        p0 = json.loads(rows["fb_batch_0"]["payload_json"])
        assert p0["feedback_outcome"] == "budget_busy"
        assert p0["budget_busy_defers"] == 1

        # Remaining items: rescheduled to now+180, attempts=0, status='pending'
        for eid in ("fb_batch_1", "fb_batch_2"):
            assert rows[eid]["status"] == "pending"
            assert rows[eid]["attempts"] == 0
            assert rows[eid]["next_at"] == pytest.approx(clock_box[0] + 180.0)
            p_rem = json.loads(rows[eid]["payload_json"])
            assert p_rem["feedback_outcome"] == "budget_busy"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_digest_cycle_breaks_persona_batch_on_budget_busy(tmp_path: Path):
    attempted_identities: list[str] = []

    async def model(role, instruction, payload):
        raise _StructuredBudgetBusyError("circuit open", retry_after=210.0)

    store, eng, clock_box = _make_engine(tmp_path, model)
    try:
        for ident in (IDENT_A, IDENT_B):
            src = store.create_source(
                identity=ident,
                source_kind="manual",
                name=f"源-{ident}",
                config={},
                now=clock_box[0],
            )
            store.insert_candidates(
                [
                    {
                        "external_id": f"ext_{ident}",
                        "title": f"标题-{ident}",
                        "creator": "作者",
                        "summary": "摘要",
                        "full_text": "正文内容足够精读。",
                        "content_ready": True,
                    }
                ],
                source_id=src["source_id"],
                source_kind="manual",
                identity=ident,
                now=clock_box[0],
            )

        eng.world_bridge.discover_personas = lambda _s, _sess: [
            {"identity": IDENT_A},
            {"identity": IDENT_B},
        ]
        orig_digest = eng._digest_for_identity

        async def wrapped_digest(ident: str):
            attempted_identities.append(ident)
            return await orig_digest(ident)

        eng._digest_for_identity = wrapped_digest

        res = await eng.run_digest_cycle()
        assert attempted_identities == [IDENT_A]
        assert len(res["results"]) == 1
        assert res["results"][0]["status"] == "error"
        assert res["results"][0]["deferred"] is True
        assert res["results"][0]["retry_after"] == 210.0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 4. Feedback _retry_job: no attempts/no held + bounded backoff (120->300->600)
# ---------------------------------------------------------------------------


def test_feedback_retry_job_budget_busy_no_held_and_bounded_backoff(tmp_path: Path):
    store, eng, clock_box = _make_engine(tmp_path, lambda *a, **kw: {})
    try:
        row = _insert_pending_feedback_job(store, "fb_backoff", now=clock_box[0])
        expected_delays = [120.0, 300.0, 600.0, 600.0, 600.0]

        for step, expected_delay in enumerate(expected_delays, start=1):
            now = clock_box[0] + step * 1000.0
            clock_box[0] = now
            exc = (
                DiscussionModelError("wrapped")
                if step == 2
                else ProviderBudgetBusyError("busy")
            )
            if step == 2:
                exc.__cause__ = ProviderBudgetBusyError("busy_inner")
            feedback_mod._retry_job(eng, row, exc, "model")
            cur = dict(
                store.db.execute(
                    "SELECT * FROM feedback_jobs WHERE event_id='fb_backoff'"
                ).fetchone()
            )
            assert cur["status"] == "pending"
            assert cur["attempts"] == 0
            assert cur["next_at"] == pytest.approx(now + expected_delay)
            parsed = json.loads(cur["payload_json"])
            assert parsed["feedback_outcome"] == "budget_busy"
            assert parsed["budget_busy_defers"] == step
            row = cur

        # Respects larger retry_after even when backoff is capped at 600s
        clock_box[0] += 1000.0
        feedback_mod._retry_job(
            eng, row, _StructuredBudgetBusyError(retry_after=900.0), "model"
        )
        cur = dict(
            store.db.execute(
                "SELECT * FROM feedback_jobs WHERE event_id='fb_backoff'"
            ).fetchone()
        )
        assert cur["status"] == "pending"
        assert cur["attempts"] == 0
        assert cur["next_at"] == pytest.approx(clock_box[0] + 900.0)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 5. Lifecycle defer_review: progressive backoff & min_delay without exhausting
# ---------------------------------------------------------------------------


def test_defer_review_tracks_and_increments_backoff(tmp_path: Path):
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    try:
        base_now = _ts(14, 0)
        p = store.create_proposal(
            identity=IDENT_A,
            source_kind="manual",
            title="退避测试提案",
            digest="摘要。",
            reflection="随感。",
            now=base_now,
        )
        pid = p["proposal_id"]

        # Progressive backoff for base delay=120s: 120 -> 300 -> 600 -> 1200 -> 1800 -> 1800
        expected = [120.0, 300.0, 600.0, 1200.0, 1800.0, 1800.0]
        for idx, exp_delay in enumerate(expected, start=1):
            now = base_now + idx * 10.0
            store.defer_review(pid, now, "provider_budget_busy", delay=120.0, backoff=True)
            row = dict(
                store.db.execute(
                    "SELECT * FROM review_jobs WHERE proposal_id=?", (pid,)
                ).fetchone()
            )
            assert row["kind"] == "retry"
            assert row["attempts"] == 0
            assert row["defer_count"] == idx
            assert row["next_at"] == pytest.approx(now + exp_delay)

        # min_delay (retry_after) overrides if larger than cap
        now_last = base_now + 1000.0
        store.defer_review(
            pid,
            now_last,
            "provider_budget_busy",
            delay=120.0,
            min_delay=2400.0,
            backoff=True,
        )
        row = dict(
            store.db.execute(
                "SELECT * FROM review_jobs WHERE proposal_id=?", (pid,)
            ).fetchone()
        )
        assert row["next_at"] == pytest.approx(now_last + 2400.0)

        # backoff=False preserves fixed delay without incrementing defer_count
        prev_defers = row["defer_count"]
        store.defer_review(pid, now_last, "asleep", delay=300.0, backoff=False)
        row2 = dict(
            store.db.execute(
                "SELECT * FROM review_jobs WHERE proposal_id=?", (pid,)
            ).fetchone()
        )
        assert row2["defer_count"] == prev_defers
        assert row2["next_at"] == pytest.approx(now_last + 300.0)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 6. Video adaptive keyframe budgeting (P1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_video_adaptive_keyframe_budgeting(tmp_path: Path, monkeypatch):
    opts = vm.normalize_media_options({"max_frames": 12, "frame_max_width": 960})
    # Subtitle-less keeps full configured budget
    assert vm.resolve_keyframe_budget(opts, has_subtitle=False, duration_sec=120.0) == (12, 960)
    assert vm.resolve_keyframe_budget(opts, has_subtitle=False, duration_sec=1200.0) == (12, 960)

    # Subtitle-backed uses compact 4-6 frames at 640px
    assert vm.resolve_keyframe_budget(opts, has_subtitle=True, duration_sec=90.0) == (4, 640)
    assert vm.resolve_keyframe_budget(opts, has_subtitle=True, duration_sec=360.0) == (5, 640)
    assert vm.resolve_keyframe_budget(opts, has_subtitle=True, duration_sec=1200.0) == (6, 640)

    # Verify acquire_video_media forwards adaptive budget into extract_keyframes_from_video
    seen_calls: list[dict] = []

    class FakeVideoObj:
        async def get_download_url(self, *, cid: int):
            return {
                "timelength": 120_000,
                "dash": {
                    "duration": 120,
                    "video": [{"id": 32, "codecid": 7, "bandwidth": 500_000, "baseUrl": "https://cdn.example/v.m4s"}],
                    "audio": [],
                },
            }

    async def fake_download(urls, dest: Path, **kwargs):
        dest.write_bytes(b"fake-video")

    async def fake_extract(video_path, out_dir: Path, **kwargs):
        seen_calls.append(dict(kwargs))
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / "f0.jpg"
        fp.write_bytes(b"\xff\xd8\xff\xd9")
        return [{"path": str(fp), "timestamp": 10.0, "label": "00:10"}]

    monkeypatch.setattr(vm, "download_media_to_file", fake_download)
    monkeypatch.setattr(vm, "extract_keyframes_from_video", fake_extract)

    b_sub = await vm.acquire_video_media(
        video_obj=FakeVideoObj(),
        bvid="BV1sub000001",
        cid=1,
        duration_sec=120.0,
        has_subtitle=True,
        subtitle_source="cc",
        media_options={"visual_enabled": True, "prefer_native_video": False},
    )
    vm.cleanup_media_bundle(b_sub)

    b_nosub = await vm.acquire_video_media(
        video_obj=FakeVideoObj(),
        bvid="BV1nosub0001",
        cid=2,
        duration_sec=120.0,
        has_subtitle=False,
        subtitle_source="",
        media_options={"visual_enabled": True, "prefer_native_video": False},
    )
    vm.cleanup_media_bundle(b_nosub)

    assert len(seen_calls) == 2
    assert seen_calls[0]["max_frames"] == 4
    assert seen_calls[0]["max_width"] == 640
    assert seen_calls[1]["max_frames"] == 12
    assert seen_calls[1]["max_width"] == 960


# ---------------------------------------------------------------------------
# 7. core_profile in system_prompt + cache prefix stability (P1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_profile_in_system_prompt_across_stages_and_main_model(tmp_path: Path):
    long_profile = "林知微是一位克制理性、酷爱器物史与古典推理的书斋型角色。" * 45
    assert len(long_profile) > 1200

    # Helper consistency check across Stage 1, Stage 2, Stage 3
    s1 = build_persona_system_prompt(
        STAGE1_TRIAGE_PROMPT, character_name="林知微", core_profile=long_profile
    )
    s2 = build_persona_system_prompt(
        STAGE2_DEEP_READ_PROMPT, character_name="林知微", core_profile=long_profile
    )
    s3 = build_persona_system_prompt(
        STAGE3_REVIEW_PROMPT, character_name="林知微", core_profile=long_profile
    )
    expected_prefix = f"【角色固定核心设定】\n角色名：林知微\n{long_profile[:1500]}\n\n【任务说明】\n"
    assert s1.startswith(expected_prefix)
    assert s2.startswith(expected_prefix)
    assert s3.startswith(expected_prefix)
    assert s1.endswith(STAGE1_TRIAGE_PROMPT)
    assert s2.endswith(STAGE2_DEEP_READ_PROMPT)
    assert s3.endswith(STAGE3_REVIEW_PROMPT)

    # End-to-end engine check: Stage 1 + Stage 2 + Stage 3 place core_profile in system_prompt,
    # NOT in user payload, and share the exact persona prefix
    seen: list[tuple[str, str, dict]] = []

    async def model(role, instruction, payload):
        seen.append((role, instruction, payload))
        if instruction.endswith(STAGE1_TRIAGE_PROMPT):
            cids = [payload["untrusted_candidates_block"]]
            assert cids
            first_cid = json.loads(
                payload["untrusted_candidates_block"]
                .replace("<untrusted_candidates_json>", "")
                .replace("</untrusted_candidates_json>", "")
            )[0]["candidate_id"]
            return {
                "selected_candidate_ids": [first_cid],
                "continue_book_ids": [],
                "drop_book_ids": [],
                "skip_candidate_ids": [],
            }
        if instruction.endswith(STAGE2_DEEP_READ_PROMPT):
            return {
                "worth_recording": True,
                "digest": "资料梳理了宋代点茶器物。",
                "reflection": "建盏兔毫纹很有趣。",
                "staged_interest": {"topic": "宋代点茶", "summary": "器物工艺"},
                "discussion_questions": [],
            }
        return {"verdict": "approve", "reason": "通过", "risks": []}

    world = _CustomWorldPlugin(character_name="林知微", character_prompt=long_profile)
    store, eng, clock_box = _make_engine(tmp_path, model, world=world)
    try:
        src = store.create_source(
            identity=IDENT_A, source_kind="manual", name="书摘", config={}, now=clock_box[0]
        )
        store.insert_candidates(
            [
                {
                    "external_id": "item_1",
                    "title": "宋代点茶一",
                    "creator": "作者A",
                    "summary": "摘要一",
                    "full_text": "第一篇关于宋代点茶的正文内容。",
                    "content_ready": True,
                },
                {
                    "external_id": "item_2",
                    "title": "宋代点茶二",
                    "creator": "作者B",
                    "summary": "摘要二",
                    "full_text": "第二篇关于宋代点茶的正文内容。",
                    "content_ready": True,
                },
            ],
            source_id=src["source_id"],
            source_kind="manual",
            identity=IDENT_A,
            now=clock_box[0],
        )
        res = await eng.run_digest_cycle(identity=IDENT_A)
        assert res["approved"] == 1
        assert len(seen) == 3  # Stage 1, Stage 2, Stage 3
        for _role, sys_prompt, payload in seen:
            assert sys_prompt.startswith(expected_prefix)
            assert "core_profile" not in payload["character"]
            assert long_profile[:100] not in encode_json(payload)
    finally:
        store.close()

    # Defensive main.py promotion: even if caller passes character.core_profile in data dict
    eff_sys, eff_data = extract_persona_system_prompt(
        STAGE12_MERGED_DIGEST_PROMPT,
        {
            "character": {"name": "林知微", "core_profile": long_profile, "preferences": ["器物"]},
            "metadata": {"title": "测试"},
        },
    )
    assert eff_sys.startswith(expected_prefix)
    assert eff_sys.endswith(STAGE12_MERGED_DIGEST_PROMPT)
    assert "core_profile" not in eff_data["character"]
    assert eff_data["character"]["preferences"] == ["器物"]

    # Main._model wires promoted system_prompt into both provider.text_chat and cache anchor contexts
    recorded: list[dict] = []

    class CaptureProvider:
        async def text_chat(self, *, prompt: str, system_prompt: str = "", contexts=None):
            recorded.append(
                {
                    "prompt": json.loads(prompt),
                    "system_prompt": system_prompt,
                    "contexts": contexts,
                }
            )
            return SimpleNamespace(completion_text='{"ok": true}')

    cap = CaptureProvider()
    ctx = SimpleNamespace(
        get_all_stars=lambda: {},
        get_provider_by_id=lambda pid: cap if pid == "p_cap" else None,
    )
    plugin = Main(ctx, {"enabled": True, "analysis_provider": "p_cap"})
    await plugin._model(
        "analysis_provider",
        STAGE12_MERGED_DIGEST_PROMPT,
        {"character": {"name": "林知微", "core_profile": long_profile}, "item": 1},
    )
    await plugin._model(
        "analysis_provider",
        STAGE12_MERGED_DIGEST_PROMPT,
        {"character": {"name": "林知微", "core_profile": long_profile}, "item": 2},
    )
    assert len(recorded) == 2
    assert recorded[0]["system_prompt"] == recorded[1]["system_prompt"] == eff_sys
    assert recorded[0]["contexts"] == recorded[1]["contexts"] == build_cache_anchor_contexts(
        eff_sys, task="dynamics_horizons"
    )
    assert "core_profile" not in recorded[0]["prompt"]["character"]


# ---------------------------------------------------------------------------
# 8. Version alignment between main.py, metadata.yaml, and core.contract
# ---------------------------------------------------------------------------


def test_main_version_aligned_with_metadata_yaml():
    repo_root = Path(main_mod.__file__).resolve().parent
    meta_text = (repo_root / "metadata.yaml").read_text(encoding="utf-8")
    meta_match = re.search(r"^version:\s*v?([^\s#]+)", meta_text, re.MULTILINE)
    assert meta_match is not None
    meta_version = meta_match.group(1).strip()

    main_text = (repo_root / "main.py").read_text(encoding="utf-8")
    main_match = re.search(
        r'ensure_current\(.*?version="([^"]+)"', main_text, re.DOTALL
    )
    assert main_match is not None
    main_version = main_match.group(1).strip()

    assert main_version == meta_version
    assert PLUGIN_VERSION == meta_version
