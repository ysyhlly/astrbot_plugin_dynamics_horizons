from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bilibili_api import exceptions as bili_exc

from astrbot_plugin_dynamics_horizons.adapters import registry
from astrbot_plugin_dynamics_horizons.adapters.ssrf import SSRFBlockedError
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore

IDENTITY = "test:horizons:bilibili"
NOW = 1_800_000_000.0


def _video(bvid: str, title: str | None = None) -> dict[str, Any]:
    return {
        "bvid": bvid,
        "aid": 1001,
        "title": title or f"Video {bvid}",
        "author": "测试 UP 主",
        "description": "只用于隔离测试的简介。",
        "created": int(NOW),
    }


def _video_result(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"list": {"vlist": list(rows)}}


class FakeUser:
    video_result: Any = None
    video_error: BaseException | None = None
    dynamics_result: Any = None
    dynamics_error: BaseException | None = None
    calls: list[str] = []

    def __init__(self, uid: int, credential=None):
        self.uid = uid

    async def get_videos(self, **kwargs):
        type(self).calls.append("videos")
        if type(self).video_error:
            raise type(self).video_error
        return type(self).video_result

    async def get_dynamics_new(self, **kwargs):
        type(self).calls.append("dynamics")
        if type(self).dynamics_error:
            raise type(self).dynamics_error
        return type(self).dynamics_result or {"items": []}


def _install(monkeypatch, *, videos=None, video_error=None, dynamics=None, dynamics_error=None):
    import astrbot_plugin_dynamics_horizons.adapters.bilibili as bili_mod

    FakeUser.calls = []
    FakeUser.video_result = videos
    FakeUser.video_error = video_error
    FakeUser.dynamics_result = dynamics
    FakeUser.dynamics_error = dynamics_error
    monkeypatch.setattr(bili_mod.user, "User", FakeUser)
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr(bili_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(bili_mod.time, "time", lambda: NOW)
    return bili_mod


def _source(store: HorizonsStore, *, cursor=None):
    source = store.create_source(
        identity=IDENTITY,
        source_kind="bilibili",
        name="子锌测试 UP 主",
        config={"mode": "up", "uid": 398046943, "limit": 10},
        now=NOW,
    )
    if cursor is not None:
        store.update_source_poll_state(
            source["source_id"],
            next_cursor=cursor,
            now=NOW,
            status="ok",
            cooldown_until=0,
        )
        source = store.get_source(source["source_id"], mask=False)
    return source


def _engine(store):
    return HorizonsEngine(store, {}, lambda *_: None, clock=lambda: NOW)


def _risk_error(code: int):
    return bili_exc.ResponseCodeException(code, f"risk {code}", {"v_voucher": ""})


@pytest.mark.asyncio
async def test_up_video_success_empty_seen_and_success_resets_backoff(tmp_path, monkeypatch):
    _install(monkeypatch, videos=_video_result(_video("BV1xx411c7mD")))
    store = HorizonsStore(tmp_path / "one.sqlite3")
    source = _source(store, cursor={"backoff_step": 3, "seen_ids": []})

    result = await registry.poll_source(source, source["cursor"])

    assert result["outcome"] == "success"
    assert len(result["items"]) == 1
    assert result["next_cursor"]["backoff_step"] == 0
    assert result["next_cursor"]["seen_ids"] == [result["items"][0]["external_id"]]
    store.close()

    _install(monkeypatch, videos=_video_result())
    store = HorizonsStore(tmp_path / "empty.sqlite3")
    source = _source(store, cursor={"backoff_step": 2, "seen_ids": []})
    result = await registry.poll_source(source, source["cursor"])
    assert result["outcome"] == "success"
    assert result["items"] == []
    assert result["next_cursor"]["backoff_step"] == 0
    store.close()

    _install(monkeypatch, videos=_video_result(_video("BV1xx411c7mD")))
    store = HorizonsStore(tmp_path / "seen.sqlite3")
    source = _source(store)
    result = await registry.poll_source(
        source,
        {"backoff_step": 2, "seen_ids": ["bili:bv:BV1xx411c7mD"]},
    )
    assert result["outcome"] == "success"
    assert result["items"] == []
    assert result["next_cursor"]["backoff_step"] == 0
    assert FakeUser.calls == ["videos", "dynamics"]
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_factory", "expected_outcome", "expected_prefix", "wait"),
    [
        (lambda: _risk_error(-412), "partial", "partial_risk_control_backoff", 1800),
        (lambda: _risk_error(-352), "partial", "partial_risk_control_backoff", 1800),
        (lambda: RuntimeError("temporary upstream failure"), "partial", "partial_error", 90),
    ],
)
async def test_dynamics_failure_commits_video_and_persists_stage_backoff(
    tmp_path, monkeypatch, error_factory, expected_outcome, expected_prefix, wait
):
    _install(
        monkeypatch,
        videos=_video_result(_video("BV1xx411c7mD")),
        dynamics_error=error_factory(),
    )
    store = HorizonsStore(tmp_path / f"partial-{wait}.sqlite3")
    source = _source(store, cursor={"backoff_step": 0, "seen_ids": []})
    engine = _engine(store)

    result = await engine.poll_one_source(source["source_id"], force=True)
    updated = store.get_source(source["source_id"], mask=False)
    candidates = store.list_unread_candidates(IDENTITY)

    assert result["outcome"] == expected_outcome
    assert result["polled"] is True
    assert result["added"] == 1
    assert result["status"].startswith(expected_prefix)
    assert updated["status"].startswith(expected_prefix)
    assert updated["last_error"].startswith("dynamics:")
    assert updated["cursor"]["backoff_step"] == 1
    assert result["cooldown_until"] == NOW + wait
    assert updated["cooldown_until"] == NOW + wait
    assert len(candidates) == 1
    assert candidates[0]["external_id"] == "bili:bv:BV1xx411c7mD"
    assert FakeUser.calls == ["videos", "dynamics"]
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("step,wait", [(0, 1800), (1, 2700), (2, 4050), (3, 6075)])
async def test_partial_risk_backoff_escalates_without_clearing_cursor(
    tmp_path, monkeypatch, step, wait
):
    _install(monkeypatch, videos=_video_result(_video("BV1xx411c7mD")), dynamics_error=_risk_error(-412))
    store = HorizonsStore(tmp_path / f"step-{step}.sqlite3")
    source = _source(store, cursor={"backoff_step": step, "seen_ids": []})

    result = await registry.poll_source(source, source["cursor"])

    assert result["outcome"] == "partial"
    assert result["cooldown_until"] == NOW + wait
    assert result["next_cursor"]["backoff_step"] == step + 1
    assert result["next_cursor"]["seen_ids"] == ["bili:bv:BV1xx411c7mD"]
    store.close()


@pytest.mark.asyncio
async def test_first_stage_412_short_circuits_dynamics(tmp_path, monkeypatch):
    _install(monkeypatch, video_error=_risk_error(-412))
    store = HorizonsStore(tmp_path / "video-412.sqlite3")
    source = _source(store)

    result = await registry.poll_source(source, source["cursor"])

    assert result["outcome"] == "cooldown"
    assert result["items"] == []
    assert result["status"].startswith("risk_control_backoff: videos:")
    assert FakeUser.calls == ["videos"]
    store.close()


@pytest.mark.asyncio
async def test_cancellation_and_ssrf_errors_propagate(tmp_path, monkeypatch):
    _install(monkeypatch, video_error=asyncio.CancelledError())
    store = HorizonsStore(tmp_path / "cancel.sqlite3")
    source = _source(store)
    with pytest.raises(asyncio.CancelledError):
        await registry.poll_source(source, source["cursor"])
    store.close()

    _install(monkeypatch, video_error=SSRFBlockedError("blocked"))
    store = HorizonsStore(tmp_path / "ssrf.sqlite3")
    source = _source(store)
    with pytest.raises(SSRFBlockedError):
        await registry.poll_source(source, source["cursor"])
    store.close()
