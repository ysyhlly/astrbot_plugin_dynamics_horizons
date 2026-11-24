"""Offline SDK, persistence and manual refresh polling regressions."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bilibili_api.exceptions import NetworkException

from astrbot_plugin_dynamics_horizons.adapters import bilibili, feed
from astrbot_plugin_dynamics_horizons.adapters.ssrf import (
    HttpResponse,
    set_dns_resolver_for_testing,
)
from astrbot_plugin_dynamics_horizons.core.contract import make_identity
from astrbot_plugin_dynamics_horizons.core.engine import HorizonsEngine
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore
from astrbot_plugin_dynamics_horizons.core.web_api import HorizonsWebAPI


@pytest.fixture
def source_poll(tmp_path, monkeypatch):
    now = SimpleNamespace(value=1_800_000_000)

    def clock():
        return now.value

    monkeypatch.setattr(bilibili, "time", SimpleNamespace(time=clock))
    sdk = AsyncMock(return_value=[])
    monkeypatch.setattr(bilibili.hot, "get_hot_videos", sdk)

    def no_model(*_args):
        pytest.fail("Source polling should never call a model")

    store = HorizonsStore(tmp_path / "horizons.sqlite3")
    engine = HorizonsEngine(store, {}, no_model, clock=clock)
    identity = make_identity("synthetic")
    source = store.create_source(
        identity=identity,
        source_kind="bilibili",
        name="Synthetic Bilibili",
        config={"mode": "popular", "popular_kind": "hot"},
        now=now.value,
    )
    source_id = source["source_id"]
    store.update_source_poll_state(
        source_id, next_cursor={"seen_ids": ["already_read"]}, now=now.value
    )
    set_dns_resolver_for_testing(lambda host: ["93.184.216.34"])
    try:
        yield engine, identity, source_id, sdk, now
    finally:
        set_dns_resolver_for_testing(None)
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code, wait", [(403, 1800), (412, 1800), (429, 300)])
async def test_sdk_http_backoff_reports_cooldown_until_recovered(source_poll, code, wait):
    engine, identity, source_id, sdk, now = source_poll
    sdk.side_effect = [NetworkException(code, "synthetic SDK failure"), {"list": []}]
    first = await engine.poll_one_source(source_id)
    deadline = now.value + wait
    assert (first["polled"], first["outcome"], first["added"]) == (False, "cooldown", 0)
    assert first["status"].startswith("risk_control_backoff:")
    assert f"http={code}" in first["error"]
    stored = engine.store.get_source(source_id)
    assert stored["cooldown_until"] == deadline == first["cooldown_until"]
    assert stored["last_error"] == first["error"]
    assert stored["cursor"] == {"seen_ids": ["already_read"], "backoff_step": 1}
    now.value = deadline - 1
    skipped = await engine.poll_all_due_sources(identity=identity)
    assert skipped == {
        "polled_sources": 0,
        "added_candidates": 0,
        "failed_sources": 0,
        "cooldown_sources": 1,
    }
    assert sdk.await_count == 1
    assert engine.store.get_source(source_id) == stored

    # Recovery obeys both upstream backoff and the default 900 s poll floor.
    now.value = max(deadline, stored["last_poll_at"] + 900)
    recovered = await engine.poll_all_due_sources(identity=identity)
    assert recovered == {
        "polled_sources": 1,
        "added_candidates": 0,
        "failed_sources": 0,
        "cooldown_sources": 0,
    }
    assert sdk.await_count == 2
    stored = engine.store.get_source(source_id)
    assert (stored["status"], stored["last_error"], stored["cooldown_until"]) == (
        "ok", "", 0
    )
    assert stored["cursor"] == {"seen_ids": ["already_read"], "backoff_step": 0}


@pytest.mark.asyncio
async def test_manual_refresh_reports_plain_error_and_forced_retry(source_poll, monkeypatch):
    engine, identity, source_id, sdk, now = source_poll
    sdk.side_effect = [
        RuntimeError("synthetic source failure"),
        NetworkException(412, "blocked"),
        {"list": []},
    ]
    api = HorizonsWebAPI(SimpleNamespace(engine=engine))
    body = {"op": "poll_now", "identity": identity, "source_id": source_id}
    monkeypatch.setattr(api, "_serve", lambda handler, **_kwargs: handler(body, "admin"))
    monkeypatch.setattr(api, "_resolve_active_identity", lambda _requested: (identity, []))

    result = await api.sources_post()
    assert (result["polled"], result["outcome"], result["added"]) == (False, "error", 0)
    assert result["error"] == "synthetic source failure"
    assert result["cooldown_until"] == now.value + 90
    stored = engine.store.get_source(source_id)
    assert stored["last_error"] == result["error"]
    assert stored["cursor"]["backoff_step"] == 1

    del body["source_id"]
    bulk_backoff = await api.sources_post()
    assert bulk_backoff == {
        "polled": False,
        "polled_sources": 0,
        "added_candidates": 0,
        "failed_sources": 0,
        "cooldown_sources": 1,
    }
    stored = engine.store.get_source(source_id)
    assert stored["cooldown_until"] == now.value + 2700
    assert stored["cursor"]["backoff_step"] == 2
    assert (await api.sources_post())["polled"] is False
    assert sdk.await_count == 2  # Manual refresh cannot bypass Bilibili risk-control cooldown.
    now.value = stored["cooldown_until"]
    assert (await api.sources_post())["polled"] is True
    assert sdk.await_count == 3


def test_failed_poll_commit_rolls_back_backoff_cursor_and_error(source_poll, monkeypatch):
    engine, _identity, source_id, _sdk, now = source_poll
    store = engine.store
    previous = store.get_source(source_id, mask=False)
    real_update = store.update_source_poll_state

    def fail_after_update(*args, **kwargs):
        real_update(*args, **kwargs)
        raise sqlite3.IntegrityError("injected failure")

    monkeypatch.setattr(store, "update_source_poll_state", fail_after_update)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.commit_poll(
            previous,
            [],
            {"seen_ids": ["already_read"], "backoff_step": 1},
            now.value + 1800,
            "risk_control_backoff: risk_control:http=412",
            now.value,
            last_error="risk_control:http=412",
        )
    assert store.get_source(source_id, mask=False) == previous


@pytest.mark.asyncio
async def test_unparsed_feed_response_cannot_advance_http_validators(
    source_poll, monkeypatch
):
    engine, identity, _bili, _sdk, now = source_poll
    store = engine.store
    url = "https://feed.example.org/items.json"
    source = store.create_source(
        identity=identity,
        source_kind="feed",
        name="Synthetic feed",
        config={"url": url},
        now=now.value,
    )
    source_id = source["source_id"]
    consumed = {
        "etag": '"parsed-v1"',
        "last_modified": "Fri, 13 Nov 2026 06:05:00 GMT",
        "seen_ids": ["already_read"],
        "backoff_step": 2,
    }
    store.update_source_poll_state(source_id, next_cursor=consumed, now=now.value)
    headers = {
        "ETag": '"new-v2"',
        "Last-Modified": "Sat, 14 Nov 2026 06:05:00 GMT",
        "Content-Type": "application/feed+json",
    }
    requests = []

    async def get_feed(request_url, *, etag, last_modified, **_kwargs):
        assert request_url == url
        requests.append((etag, last_modified))
        if len(requests) == 1:
            return HttpResponse(status=200, body=b"{malformed", headers=headers)
        if etag == headers["ETag"] or last_modified == headers["Last-Modified"]:
            # A poisoned validator would make this version invisible.
            return HttpResponse(status=304, headers=headers)
        payload = {
            "version": "https://jsonfeed.org/version/1.1",
            "items": [{"id": "new", "title": "New article", "content_text": "New text"}],
        }
        return HttpResponse(status=200, body=json.dumps(payload).encode(), headers=headers)

    monkeypatch.setattr(feed, "safe_http_get", get_feed)
    monkeypatch.setattr(feed, "time", SimpleNamespace(time=engine.clock))

    failure = await engine.poll_one_source(source_id)
    assert failure["status"].startswith("parse_error:")
    assert failure["polled"] is False and failure["outcome"] == "error"
    failed_source = store.get_source(source_id)
    assert failed_source["last_error"] == failure["error"]
    assert failed_source["cursor"] == dict(consumed, backoff_step=3)

    now.value = failure["cooldown_until"]
    retry = await engine.poll_one_source(source_id)
    assert requests == [(consumed["etag"], consumed["last_modified"])] * 2
    assert retry["polled"] is True and retry["added"] == 1
    recovered = store.get_source(source_id)
    assert recovered["last_error"] == ""
    assert recovered["cursor"] == {
        "etag": headers["ETag"],
        "last_modified": headers["Last-Modified"],
        "seen_ids": ["already_read", "feed:new"],
        "backoff_step": 0,
        "pending_items": [],
    }
