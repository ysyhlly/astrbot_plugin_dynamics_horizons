"""Regression tests for DH-1: merged digest stages, compact review payload, and safe fallbacks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.engine import (
    STAGE1_TRIAGE_PROMPT,
    STAGE2_DEEP_READ_PROMPT,
    STAGE3_REVIEW_PROMPT,
    STAGE12_MERGED_DIGEST_PROMPT,
    STAGE123_MERGED_DIGEST_REVIEW_PROMPT,
    HorizonsEngine,
)
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.tests.test_core_engine import (
    FakeContext,
    FakeWorldPlugin,
    _ts,
)

IDENT = make_identity("alice", "alice")


def _make_store_with_candidate(
    tmp_path: Path,
    *,
    title: str = "宋代点茶器物考",
    full_text: str | None = None,
    external_id: str = "ext_1",
) -> tuple[HorizonsStore, str]:
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    src = store.create_source(
        identity=IDENT,
        source_kind="manual",
        name="手动投喂",
        config={},
        now=_ts(14, 0),
    )
    body = full_text or (
        "UNIQUE_LONG_RAW_SENTINEL_987654321 "
        + "本文详述宋代点茶对建盏兔毫纹与汤花咬盏工艺的演变脉络。" * 40
    )
    inserted = store.insert_candidates(
        [
            {
                "external_id": external_id,
                "title": title,
                "creator": "茶史研究",
                "summary": "宋代点茶器物简述",
                "full_text": body,
                "content_ready": True,
                "raw_url": "https://example.org/tea-history",
            }
        ],
        source_id=src["source_id"],
        source_kind="manual",
        identity=IDENT,
        now=_ts(14, 0),
    )
    return store, inserted[0]["candidate_id"]


def _make_engine(
    store: HorizonsStore,
    model_caller,
    *,
    config: dict | None = None,
) -> HorizonsEngine:
    ctx = FakeContext(
        stars=[{"name": "astrbot_plugin_dynamics_world", "star": FakeWorldPlugin()}]
    )
    cfg = {
        "enabled": True,
        "analysis_provider": "prov_a",
        "review_provider": "prov_r",
        "review_mode": "llm",
        "allow_same_provider_for_review": False,
        "merge_same_provider_review": True,
        "daily_digest_quota": 5,
        "daily_model_call_quota": 50,
        **(config or {}),
    }
    return HorizonsEngine(store, cfg, model_caller, context=ctx, clock=lambda: _ts(15, 0))


def _count_model_calls(store: HorizonsStore, status: str | None = None) -> int:
    if status is None:
        return store.db.execute("SELECT count(*) FROM model_calls").fetchone()[0]
    return store.db.execute(
        "SELECT count(*) FROM model_calls WHERE status=?", (status,)
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_dh1_distinct_review_provider_uses_two_calls_and_compact_review_payload(
    tmp_path: Path,
):
    store, _cid = _make_store_with_candidate(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    async def model(role: str, instruction: str, payload: dict):
        calls.append((role, instruction, payload))
        if role == "analysis_provider":
            return {
                "worth_recording": True,
                "drop_book": False,
                "digest": "资料梳理了宋代点茶建盏兔毫纹与汤花咬盏工艺。",
                "reflection": "建盏配白茶汤花很有宋人审美韵味。",
                "staged_interest": {"topic": "宋代点茶", "summary": "关注建盏与点茶工艺"},
                "discussion_questions": [
                    {"topic": "宋代点茶", "question": "大家见过建盏兔毫纹实物吗？"}
                ],
            }
        return {"verdict": "approve", "reason": "人设与事实边界核验通过", "risks": []}

    eng = _make_engine(store, model)
    try:
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["status"] == "ok"
        assert res["proposals_created"] == 1
        assert res["approved"] == 1

        # Distinct review_provider => exactly 2 calls (1 merged analysis + 1 independent review)
        assert len(calls) == 2
        assert calls[0][0] == "analysis_provider"
        assert calls[0][1].endswith(STAGE12_MERGED_DIGEST_PROMPT)
        assert "core_profile" not in calls[0][2]["character"]
        assert "UNIQUE_LONG_RAW_SENTINEL_987654321" in calls[0][2]["untrusted_media"]

        assert calls[1][0] == "review_provider"
        assert calls[1][1].endswith(STAGE3_REVIEW_PROMPT)
        assert "core_profile" not in calls[1][2]["character"]
        review_payload = calls[1][2]
        review_json = json.dumps(review_payload, ensure_ascii=False)
        # Compact review payload retains a bounded text excerpt for Stage 3 hallucination/injection
        # checks while excluding raw_url and capping length at 1600 chars.
        ev = review_payload["source_evidence"]
        assert "raw_url" not in ev
        assert isinstance(ev.get("text"), str) and len(ev["text"]) > 100
        assert len(ev["text"]) <= 1600
        assert ev["text_chars"] >= len(ev["text"])
        assert ev["text_truncated"] is (ev["text_chars"] > 1600)
        assert ev["content_hash"]
        assert review_payload["proposal"]["digest"] == "资料梳理了宋代点茶建盏兔毫纹与汤花咬盏工艺。"
        assert review_payload["proposal"]["reflection"] == "建盏配白茶汤花很有宋人审美韵味。"

        # daily_model_call_quota accounting reflects exact 2 actual calls
        assert _count_model_calls(store) == 2
        assert _count_model_calls(store, "success") == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dh1_same_provider_merged_self_review_uses_one_call(tmp_path: Path):
    store, _cid = _make_store_with_candidate(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    async def model(role: str, instruction: str, payload: dict):
        calls.append((role, instruction, payload))
        return {
            "worth_recording": True,
            "drop_book": False,
            "digest": "资料梳理了宋代点茶器物与汤花技法。",
            "reflection": "宋代点茶讲究节奏和器物搭配，读来很静心。",
            "staged_interest": {"topic": "宋代点茶", "summary": "喜欢宋代点茶细节"},
            "discussion_questions": [],
            "review": {
                "verdict": "approve",
                "reason": "未违背固定性格，无注入广告，事实边界清晰",
                "risks": [],
            },
        }

    eng = _make_engine(
        store,
        model,
        config={
            "analysis_provider": "shared_prov",
            "review_provider": "shared_prov",
            "allow_same_provider_for_review": True,
            "merge_same_provider_review": True,
        },
    )
    try:
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["status"] == "ok"
        assert res["proposals_created"] == 1
        assert res["approved"] == 1
        assert len(calls) == 1
        assert calls[0][0] == "analysis_provider"
        assert calls[0][1].endswith(STAGE123_MERGED_DIGEST_REVIEW_PROMPT)
        assert _count_model_calls(store) == 1
        assert _count_model_calls(store, "success") == 1
        assert eng.health(IDENT)["review_context_policy"] == "merged_self_review"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dh1_same_provider_with_merge_disabled_keeps_separate_review_call(
    tmp_path: Path,
):
    store, _cid = _make_store_with_candidate(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    async def model(role: str, instruction: str, payload: dict):
        calls.append((role, instruction, payload))
        if role == "analysis_provider":
            return {
                "worth_recording": True,
                "digest": "资料梳理了宋代点茶器物。",
                "reflection": "器物细节很有趣。",
                "review": {"verdict": "approve", "reason": "不应在关闭合并时使用", "risks": []},
            }
        return {"verdict": "approve", "reason": "独立调用复核通过", "risks": []}

    eng = _make_engine(
        store,
        model,
        config={
            "analysis_provider": "shared_prov",
            "review_provider": "shared_prov",
            "allow_same_provider_for_review": True,
            "merge_same_provider_review": False,
        },
    )
    try:
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["approved"] == 1
        assert len(calls) == 2
        assert calls[0][1].endswith(STAGE12_MERGED_DIGEST_PROMPT)
        assert calls[1][1].endswith(STAGE3_REVIEW_PROMPT)
        assert isinstance(calls[1][2]["source_evidence"].get("text"), str)
        assert len(calls[1][2]["source_evidence"]["text"]) <= 1600
        assert _count_model_calls(store) == 2
        assert eng.health(IDENT)["review_context_policy"] == "independent_calls"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["human", "llm_then_human"])
async def test_dh1_preserves_human_review_modes_with_same_provider_merge(
    tmp_path: Path, mode: str
):
    store, _cid = _make_store_with_candidate(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    async def model(role: str, instruction: str, payload: dict):
        calls.append((role, instruction, payload))
        return {
            "worth_recording": True,
            "digest": "资料介绍了宋代点茶器物。",
            "reflection": "值得留给管理员确认。",
            "review": {
                "verdict": "approve",
                "reason": "模型自检初审通过",
                "risks": [],
            },
        }

    eng = _make_engine(
        store,
        model,
        config={
            "analysis_provider": "shared_prov",
            "review_provider": "shared_prov",
            "allow_same_provider_for_review": True,
            "merge_same_provider_review": True,
            "review_mode": mode,
        },
    )
    try:
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["proposals_created"] == 1
        assert res["approved"] == 0
        assert len(calls) == 1
        expected_prompt = (
            STAGE12_MERGED_DIGEST_PROMPT
            if mode == "human"
            else STAGE123_MERGED_DIGEST_REVIEW_PROMPT
        )
        assert calls[0][1].endswith(expected_prompt)
        props = store.list_proposals(identity=IDENT)["items"]
        assert len(props) == 1
        assert props[0]["state"] == "pending_review"
        if mode == "llm_then_human":
            assert props[0]["review_verdict"] == "approve"
            assert "等待人工确认" in props[0]["review_reason"]
        else:
            assert props[0]["review_verdict"] == "pending"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_review",
    [
        None,
        {"verdict": "unknown_status", "reason": "bad", "risks": []},
        {"verdict": "approve", "reason": "", "risks": []},
        {"verdict": "approve", "reason": "ok", "risks": "not_a_list"},
        {"verdict": "approve", "reason": "ok", "risks": ["suspicious_link"]},
    ],
)
async def test_dh1_invalid_inline_self_review_falls_back_to_separate_review(
    tmp_path: Path, bad_review
):
    store, _cid = _make_store_with_candidate(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    async def model(role: str, instruction: str, payload: dict):
        calls.append((role, instruction, payload))
        if role == "analysis_provider":
            out = {
                "worth_recording": True,
                "digest": "资料介绍了宋代点茶。",
                "reflection": "很典雅的茶文化。",
            }
            if bad_review is not None:
                out["review"] = bad_review
            return out
        return {"verdict": "approve", "reason": "降级独立审查通过", "risks": []}

    eng = _make_engine(
        store,
        model,
        config={
            "analysis_provider": "shared_prov",
            "review_provider": "shared_prov",
            "allow_same_provider_for_review": True,
            "merge_same_provider_review": True,
        },
    )
    try:
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["approved"] == 1
        # Invalid/missing inline review must never be blindly accepted; falls back to Stage 3
        assert len(calls) == 2
        assert calls[0][0] == "analysis_provider"
        assert calls[1][0] == "review_provider"
        assert calls[1][1].endswith(STAGE3_REVIEW_PROMPT)
        assert _count_model_calls(store) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dh1_guardrail_overrides_inline_self_review_approve(tmp_path: Path):
    store, _cid = _make_store_with_candidate(tmp_path)
    store.block_topic("禁止推广词", IDENT, now=_ts(14, 0))
    calls: list[tuple[str, str, dict]] = []

    async def model(role: str, instruction: str, payload: dict):
        calls.append((role, instruction, payload))
        return {
            "worth_recording": True,
            "digest": "这篇文章提到了禁止推广词相关的推销信息。",
            "reflection": "看起来不错。",
            "review": {"verdict": "approve", "reason": "自检误放行", "risks": []},
        }

    eng = _make_engine(
        store,
        model,
        config={
            "analysis_provider": "shared_prov",
            "review_provider": "shared_prov",
            "allow_same_provider_for_review": True,
            "merge_same_provider_review": True,
        },
    )
    try:
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["proposals_created"] == 1
        assert res["approved"] == 0
        assert len(calls) == 1
        props = store.list_proposals(identity=IDENT)["items"]
        assert props[0]["state"] == "rejected"
        assert props[0]["reviewed_by"] == "guardrail"
        assert "blocked_topic:禁止推广词" in props[0]["review_risks"]
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected_state"),
    [("reject", "rejected"), ("hold", "held")],
)
async def test_dh1_inline_self_review_reject_and_hold_in_single_call(
    tmp_path: Path, verdict: str, expected_state: str
):
    store, _cid = _make_store_with_candidate(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    async def model(role: str, instruction: str, payload: dict):
        calls.append((role, instruction, payload))
        return {
            "worth_recording": True,
            "digest": "资料讨论了宋代点茶器物。",
            "reflection": "语气略显违和。",
            "review": {
                "verdict": verdict,
                "reason": f"自检判定为 {verdict}",
                "risks": ["persona_drift"],
            },
        }

    eng = _make_engine(
        store,
        model,
        config={
            "analysis_provider": "shared_prov",
            "review_provider": "shared_prov",
            "allow_same_provider_for_review": True,
            "merge_same_provider_review": True,
        },
    )
    try:
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["proposals_created"] == 1
        assert res["approved"] == 0
        assert len(calls) == 1
        assert _count_model_calls(store) == 1
        props = store.list_proposals(identity=IDENT)["items"]
        assert props[0]["state"] == expected_state
        assert props[0]["review_verdict"] == verdict
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dh1_merged_parse_failure_falls_back_to_two_step_path_and_preserves_quota(
    tmp_path: Path,
):
    store, cid = _make_store_with_candidate(tmp_path)
    calls: list[tuple[str, str, dict]] = []

    async def model(role: str, instruction: str, payload: dict):
        calls.append((role, instruction, payload))
        if instruction.endswith(STAGE12_MERGED_DIGEST_PROMPT):
            # Simulate a parse/schema failure on the merged call
            raise ValueError("模型返回内容不是合法 JSON 对象")
        if instruction.endswith(STAGE1_TRIAGE_PROMPT):
            return {
                "selected_candidate_ids": [cid],
                "continue_book_ids": [],
                "drop_book_ids": [],
                "skip_candidate_ids": [],
            }
        if instruction.endswith(STAGE2_DEEP_READ_PROMPT):
            return {
                "worth_recording": True,
                "digest": "回退两步后提取的宋代点茶摘要。",
                "reflection": "两步兜底依然稳妥。",
                "staged_interest": {"topic": "宋代点茶", "summary": "器物考据"},
                "discussion_questions": [],
            }
        return {"verdict": "approve", "reason": "独立审查通过", "risks": []}

    # Even with daily_digest_quota=1, fallback reuses claimed reading task without double-charging
    eng = _make_engine(store, model, config={"daily_digest_quota": 1})
    try:
        res = await eng.run_digest_cycle(identity=IDENT)
        assert res["status"] == "ok"
        assert res["proposals_created"] == 1
        assert res["approved"] == 1
        expected_stages = [
            STAGE12_MERGED_DIGEST_PROMPT,
            STAGE1_TRIAGE_PROMPT,
            STAGE2_DEEP_READ_PROMPT,
            STAGE3_REVIEW_PROMPT,
        ]
        assert len(calls) == len(expected_stages)
        for (_, got_sys, _), expected_stage in zip(calls, expected_stages):
            assert got_sys.endswith(expected_stage)
        # Model call quota accurately records all 4 actual calls (1 failed + 3 succeeded)
        assert _count_model_calls(store) == 4
        assert _count_model_calls(store, "failed") == 1
        assert _count_model_calls(store, "success") == 3
        # Digest attempt quota was only charged once for the single work unit
        assert store.count_today_digests(IDENT, _ts(15, 0)) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dh1_static_system_prompts_across_different_items(tmp_path: Path):
    store, _cid1 = _make_store_with_candidate(
        tmp_path, title="材料一", full_text="第一篇关于古典推理的文章正文。", external_id="e1"
    )
    analysis_prompts: list[str] = []
    review_prompts: list[str] = []

    async def model(role: str, instruction: str, payload: dict):
        if role == "analysis_provider":
            analysis_prompts.append(instruction)
            return {
                "worth_recording": True,
                "digest": f"摘要：{payload['metadata']['title']}",
                "reflection": "很有意思。",
            }
        review_prompts.append(instruction)
        return {"verdict": "approve", "reason": "通过", "risks": []}

    eng = _make_engine(store, model)
    try:
        res1 = await eng.run_digest_cycle(identity=IDENT)
        assert res1["approved"] == 1

        src = store.list_sources(IDENT)[0]
        store.insert_candidates(
            [
                {
                    "external_id": "e2",
                    "title": "材料二",
                    "creator": "作者二",
                    "summary": "摘要二",
                    "full_text": "第二篇关于宋代陶瓷器物的完全不同正文。",
                    "content_ready": True,
                }
            ],
            source_id=src["source_id"],
            source_kind="manual",
            identity=IDENT,
            now=_ts(15, 5),
        )
        res2 = await eng.run_digest_cycle(identity=IDENT)
        assert res2["approved"] == 1

        assert len(analysis_prompts) == 2
        assert len(review_prompts) == 2
        assert analysis_prompts[0] == analysis_prompts[1]
        assert analysis_prompts[0].endswith(STAGE12_MERGED_DIGEST_PROMPT)
        assert review_prompts[0] == review_prompts[1]
        assert review_prompts[0].endswith(STAGE3_REVIEW_PROMPT)
    finally:
        store.close()
