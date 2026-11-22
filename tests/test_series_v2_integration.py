"""Focused tests for Horizons shared ProviderBudget, timezone, provider fallback, exclude_groups & quiet_hours."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_horizons.core.contract import (
    make_identity,
    shanghai_day_bounds,
)
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.feedback import enqueue
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.main import Main as DynamicsHorizonsPlugin
from astrbot_plugin_dynamics_horizons.series_panels import (
    ProviderBudgetBusyError,
    resolve_series_timezone,
    series_llm_call,
)

IDENT = make_identity("alice")


class FakeSharedBudget:
    supports_series_telemetry = True

    def __init__(self, *, busy: bool = False):
        self.busy = busy
        self.calls: list[dict] = []

    async def run(
        self,
        provider: str,
        purpose: str,
        invoke,
        *,
        plugin_id: str = "",
        acquire_timeout: float | None = None,
    ):
        self.calls.append(
            {
                "provider": provider,
                "purpose": purpose,
                "plugin_id": plugin_id,
                "acquire_timeout": acquire_timeout,
            }
        )
        if self.busy:
            err = RuntimeError("queue busy")
            err.is_provider_budget_busy = True
            err.provider = provider
            err.purpose = purpose
            err.reason = "acquire_timeout"
            raise err
        val = invoke()
        return await val if asyncio.iscoroutine(val) else val


def _make_chat_context(
    *,
    activated: bool = True,
    reply_provider: str = "series-reply",
    decision_provider: str = "series-decision",
    rhythm_timezone: str = "America/New_York",
    exclude_groups: list[str] | None = None,
    in_quiet_hours: bool = False,
    budget=None,
    host_provider_id: str = "",
):
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": reply_provider,
                "decision_provider": decision_provider,
                "rhythm_timezone": rhythm_timezone,
                "exclude_groups": exclude_groups or [],
                "takeover_groups": ["aiocqhttp:GroupMessage:1001"],
                "quiet_hours": {
                    "daily_rhythm_enabled": True,
                    "in_quiet_hours": in_quiet_hours,
                },
            }

    stars = (
        {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=activated,
                star_cls=ChatOwner(),
            )
        }
        if activated is not None
        else {}
    )
    host_prov = (
        SimpleNamespace(
            meta=SimpleNamespace(id=host_provider_id),
            text_chat=AsyncMock(
                return_value=SimpleNamespace(completion_text='{"ok":true}')
            ),
        )
        if host_provider_id
        else None
    )
    ctx = SimpleNamespace(
        get_all_stars=lambda: stars,
        get_using_provider=lambda *a, **kw: host_prov,
        get_provider_by_id=lambda pid: (
            host_prov if host_prov and pid == host_provider_id else None
        ),
    )
    if budget is not None:
        ctx._chat_dynamics_provider_budget = budget
    return ctx


def _make_plugin(tmp_path, ctx, config=None):
    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    cfg = {
        "enabled": True,
        "analysis_provider": "",
        "review_provider": "",
        "quiet_hours": "00:00-08:00",
        "sessions": ["aiocqhttp:GroupMessage:1001", "aiocqhttp:GroupMessage:2002"],
        "proactive_enabled": True,
        **(config or {}),
    }
    plugin = DynamicsHorizonsPlugin.__new__(DynamicsHorizonsPlugin)
    plugin.context = ctx
    plugin.config = cfg
    plugin.ready = True
    plugin.closed = False
    plugin.engine = HorizonsEngine(store, cfg, plugin._model, context=ctx)
    return plugin, store


@pytest.mark.asyncio
async def test_horizons_shared_budget_delegation_and_busy_degradation(tmp_path):
    budget = FakeSharedBudget(busy=False)
    ctx = _make_chat_context(budget=budget, host_provider_id="p-analysis")
    plugin, store = _make_plugin(tmp_path, ctx, {"analysis_provider": "p-analysis"})

    res = await plugin._model("analysis_provider", "sys", {"k": "v"})
    assert res == {"ok": True}
    assert len(budget.calls) == 1
    assert budget.calls[0]["plugin_id"] == "astrbot_plugin_dynamics_horizons"
    assert budget.calls[0]["provider"] == "p-analysis"

    # Busy budget raises local ProviderBudgetBusyError and defers review gracefully
    busy_budget = FakeSharedBudget(busy=True)
    ctx._chat_dynamics_provider_budget = busy_budget
    with pytest.raises(ProviderBudgetBusyError):
        await plugin._model("analysis_provider", "sys", {"k": "v"})

    async def fake_inspect(*args, **kwargs):
        return {
            "awake": True,
            "character_prompt": "test persona",
            "scope": {"persona_id": IDENT},
        }

    plugin.engine.world_bridge.inspect_wake_and_profile = fake_inspect
    ctx.get_provider_by_id = lambda pid: SimpleNamespace(
        text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='{"verdict":"approve"}'))
    )
    prop = store.create_proposal(
        identity=IDENT,
        source_kind="manual",
        title="茶道",
        digest="资料介绍了茶道。",
        reflection="挺有意思。",
        staged_interest={"topic": "茶道", "summary": "了解茶道"},
        questions=[],
        now=1700000000.0,
    )
    deferred = await plugin.engine.review_proposal(prop["proposal_id"])
    assert deferred["state"] == "pending_review"
    store.close()


@pytest.mark.asyncio
async def test_horizons_standalone_without_chat_dynamics(tmp_path):
    host_prov = SimpleNamespace(
        meta=SimpleNamespace(id="host-default-p"),
        text_chat=AsyncMock(
            return_value=SimpleNamespace(completion_text='{"standalone":true}')
        ),
    )
    ctx = SimpleNamespace(
        get_all_stars=lambda: {},
        get_using_provider=lambda *a, **kw: host_prov,
        get_provider_by_id=lambda pid: host_prov if pid == "host-default-p" else None,
    )
    plugin, store = _make_plugin(tmp_path, ctx)

    # Provider falls back to host default when Chat Dynamics is absent
    assert plugin.engine.effective_provider("analysis_provider") == "host-default-p"
    res = await plugin._model("analysis_provider", "sys", {"a": 1})
    assert res == {"standalone": True}

    # Explicit config always wins over shared/host defaults
    plugin.config["analysis_provider"] = "explicit-p"
    assert plugin.engine.effective_provider("analysis_provider") == "explicit-p"

    # Direct series_llm_call without budget works transparently
    val = await series_llm_call(
        ctx, "host-default-p", "astrbot_plugin_dynamics_horizons", "test", lambda: "ok"
    )
    assert val == "ok"
    store.close()


def test_horizons_timezone_inheritance_affects_day_bounds(tmp_path):
    ctx = _make_chat_context(rhythm_timezone="America/New_York")
    plugin, store = _make_plugin(tmp_path, ctx)
    tz = resolve_series_timezone(ctx)
    assert str(tz) == "America/New_York"

    # 2026-06-02 02:00 UTC is 2026-06-02 10:00 in Asia/Shanghai, but 2026-06-01 22:00 in NY
    ts = datetime(2026, 6, 2, 2, 0, tzinfo=timezone.utc).timestamp()
    ny_start, ny_end = shanghai_day_bounds(ts, tz=plugin.engine.timezone)
    sh_start, _ = shanghai_day_bounds(ts)
    assert ny_start != sh_start
    assert datetime.fromtimestamp(ny_start, tz).strftime("%Y-%m-%d") == "2026-06-01"
    assert ny_end > ny_start
    store.close()


def test_horizons_exclude_groups_and_quiet_hours_guards(tmp_path):
    excluded_umo = "aiocqhttp:GroupMessage:2002"
    allowed_umo = "aiocqhttp:GroupMessage:1001"
    ctx = _make_chat_context(
        exclude_groups=[excluded_umo],
        in_quiet_hours=True,
    )
    plugin, store = _make_plugin(tmp_path, ctx)

    # Excluded group: scope status reflects 主控排除, outreach & context & feedback blocked
    st_ex = plugin.get_series_scope_status(excluded_umo)
    assert st_ex["enabled"] is False
    assert st_ex["excluded_by_core"] is True
    assert "主控排除" in st_ex["summary"]
    assert plugin.horizons_outreach_allowed(excluded_umo) is False
    assert (
        plugin.get_horizons_context(scope=excluded_umo)["match_reason"]
        == "excluded_by_core"
    )
    fb_res = enqueue(
        plugin.engine,
        {
            "umo": excluded_umo,
            "member": "u1",
            "event_id": "e1",
            "kind": "turn_completed",
            "text": "hello",
            "character_scope": {"persona_id": "alice", "umo": excluded_umo},
        },
    )
    assert fb_res["accepted"] is False
    assert fb_res["reason"] == "excluded_by_core"

    # Allowed group still enabled for non-excluded checks
    st_ok = plugin.get_series_scope_status(allowed_umo)
    assert st_ok["enabled"] is True
    assert not st_ok.get("excluded_by_core", False)

    # Quiet hours suppresses proactive due intents immediately
    assert plugin.list_due_intents(now=1700040000.0) == []
    store.close()
