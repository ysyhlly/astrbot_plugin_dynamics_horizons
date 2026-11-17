"""Tests for read-only series shared defaults inheritance in Dynamics Horizons."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore

IDENT = make_identity("alice")


def _chat_context(
    *,
    activated: bool = True,
    reply_provider: str = "series-reply",
    decision_provider: str = "series-decision",
    quiet_hours: object = "01:00-07:00",
    takeover_groups: list[str] | None = None,
):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": reply_provider,
                "decision_provider": decision_provider,
                "takeover_groups": takeover_groups
                if takeover_groups is not None
                else ["qq:GroupMessage:501"],
                "quiet_hours": quiet_hours,
            }

    return SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=activated,
                star_cls=ChatOwner(),
            )
        }
    )


def test_horizons_inherits_series_defaults_read_only(tmp_path):
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    ctx = _chat_context()
    cfg = {
        "analysis_provider": "",
        "review_provider": "",
        "quiet_hours": "",
        "sessions": [],
    }
    engine = HorizonsEngine(store, cfg, lambda *a: None, context=ctx)

    assert engine.effective_provider("analysis_provider") == "series-reply"
    assert engine.effective_provider("review_provider") == "series-decision"
    assert engine.quiet_hours == "01:00-07:00"
    assert engine.sessions == ["qq:GroupMessage:501"]
    status = engine.health(IDENT)
    assert status["analysis_provider"] == "series-reply"
    assert status["review_provider"] == "series-decision"
    assert "analysis_provider_missing" not in status["blockers"]
    assert "review_provider_missing" not in status["blockers"]
    assert cfg["analysis_provider"] == ""
    store.close()


def test_horizons_explicit_config_priority_and_dict_quiet_hours_guard(tmp_path):
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    ctx = _chat_context(quiet_hours={"daily_rhythm_enabled": True})
    cfg = {
        "analysis_provider": "own-a",
        "review_provider": "own-r",
        "quiet_hours": "02:00-06:00",
        "sessions": ["qq:GroupMessage:888"],
    }
    engine = HorizonsEngine(store, cfg, lambda *a: None, context=ctx)

    assert engine.effective_provider("analysis_provider") == "own-a"
    assert engine.effective_provider("review_provider") == "own-r"
    assert engine.quiet_hours == "02:00-06:00"
    assert engine.sessions == ["qq:GroupMessage:888"]

    # Non-string quiet_hours dict from chat_dynamics safely falls back to 00:00-08:00.
    cfg_empty_qh = {"quiet_hours": ""}
    engine_empty_qh = HorizonsEngine(store, cfg_empty_qh, lambda *a: None, context=ctx)
    assert engine_empty_qh.quiet_hours == "00:00-08:00"
    store.close()


def test_horizons_safe_fallback_when_chat_dynamics_missing_or_disabled(tmp_path):
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    cfg = {"analysis_provider": "", "review_provider": "", "quiet_hours": "", "sessions": []}
    for ctx in (
        SimpleNamespace(get_all_stars=lambda: {}),
        _chat_context(activated=False),
    ):
        engine = HorizonsEngine(store, cfg, lambda *a: None, context=ctx)
        assert engine.effective_provider("analysis_provider") == ""
        assert engine.effective_provider("review_provider") == ""
        assert engine.quiet_hours == "00:00-08:00"
        assert engine.sessions == []
    store.close()


def test_horizons_review_proposal_uses_inherited_review_provider(tmp_path):
    store = HorizonsStore(tmp_path / "horizons-review.sqlite3")
    ctx = _chat_context()
    cfg = {"analysis_provider": "", "review_provider": "", "quiet_hours": "", "sessions": []}
    calls = []

    async def model(role, instruction, data):
        calls.append(role)
        return {"verdict": "approve", "reason": "ok", "risks": []}

    async def fake_inspect(*args, **kwargs):
        return {
            "awake": True,
            "character_prompt": "test persona",
            "scope": {"persona_id": IDENT},
        }

    engine = HorizonsEngine(store, cfg, model, context=ctx)
    engine.world_bridge.inspect_wake_and_profile = fake_inspect
    p = store.create_proposal(
        identity=IDENT,
        source_kind="manual",
        title="茶文化记录",
        digest="资料介绍了宋代点茶。",
        reflection="对点茶很感兴趣。",
        staged_interest={"topic": "茶文化", "summary": "想了解点茶"},
        questions=[{"topic": "茶文化", "question": "你喜欢哪种茶？"}],
        now=1700000000.0,
    )
    res = asyncio.run(engine.review_proposal(p["proposal_id"]))
    assert calls == ["review_provider"]
    assert res["state"] == "approved"
    store.close()
