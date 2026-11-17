from __future__ import annotations

import pytest

from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore

NOW = 1_800_000_000.0
IDENTITY = "test:horizons:bili-interval"


def _source(store, kind="bilibili", *, last_poll_at=NOW - 180, status="ok", cooldown=0):
    source = store.create_source(
        identity=IDENTITY,
        source_kind=kind,
        name=f"{kind} source",
        config={"mode": "up", "uid": 398046943} if kind == "bilibili" else {},
        now=NOW - 3600,
    )
    store.update_source_poll_state(
        source["source_id"],
        next_cursor={"backoff_step": 0},
        cooldown_until=cooldown,
        status=status,
        last_error="",
        now=last_poll_at,
    )
    return store.get_source(source["source_id"], mask=False)


def _engine(store, calls, *, interval=900):
    engine = HorizonsEngine(
        store,
        {"poll_interval_seconds": interval},
        lambda *_: None,
        clock=lambda: NOW,
    )

    async def poll_adapter(source, cursor, *, timeout=20):
        calls.append(source["source_id"])
        return {
            "items": [],
            "next_cursor": dict(cursor or {}),
            "cooldown_until": 0,
            "status": "ok",
            "outcome": "success",
        }

    engine.poll_source_adapter = poll_adapter
    return engine


@pytest.mark.asyncio
async def test_recent_successful_bilibili_poll_is_skipped_after_engine_restart(tmp_path):
    store = HorizonsStore(tmp_path / "restart.sqlite3")
    source = _source(store, last_poll_at=NOW - 180)
    calls = []
    engine = _engine(store, calls)

    result = await engine.poll_all_due_sources()

    assert result["polled_sources"] == 0
    assert result["cooldown_sources"] == 1
    assert calls == []
    assert store.get_source(source["source_id"], mask=False)["last_poll_at"] == NOW - 180
    store.close()


@pytest.mark.asyncio
async def test_bilibili_source_is_polled_after_configured_interval(tmp_path):
    store = HorizonsStore(tmp_path / "elapsed.sqlite3")
    source = _source(store, last_poll_at=NOW - 901)
    calls = []
    engine = _engine(store, calls, interval=900)

    result = await engine.poll_all_due_sources()

    assert result["polled_sources"] == 1
    assert result["cooldown_sources"] == 0
    assert calls == [source["source_id"]]
    store.close()


@pytest.mark.asyncio
async def test_force_allows_recent_healthy_bilibili_source(tmp_path):
    store = HorizonsStore(tmp_path / "force-healthy.sqlite3")
    source = _source(store, last_poll_at=NOW - 30)
    calls = []
    engine = _engine(store, calls)

    result = await engine.poll_all_due_sources(force=True)

    assert result["polled_sources"] == 1
    assert calls == [source["source_id"]]
    store.close()


@pytest.mark.asyncio
async def test_force_still_respects_active_bilibili_risk_cooldown(tmp_path):
    store = HorizonsStore(tmp_path / "force-risk.sqlite3")
    source = _source(
        store,
        last_poll_at=NOW - 30,
        status="risk_control_backoff: risk_control:http=412",
        cooldown=NOW + 1800,
    )
    calls = []
    engine = _engine(store, calls)

    result = await engine.poll_all_due_sources(force=True)

    assert result["polled_sources"] == 0
    assert result["cooldown_sources"] == 1
    assert calls == []
    updated = store.get_source(source["source_id"], mask=False)
    assert updated["last_poll_at"] == NOW - 30
    assert updated["cooldown_until"] == NOW + 1800
    store.close()


@pytest.mark.asyncio
async def test_interval_gate_is_scoped_to_bilibili(tmp_path):
    store = HorizonsStore(tmp_path / "scope.sqlite3")
    source = _source(store, kind="feed", last_poll_at=NOW - 30)
    calls = []
    engine = _engine(store, calls)

    result = await engine.poll_all_due_sources()

    assert result["polled_sources"] == 1
    assert calls == [source["source_id"]]
    store.close()
