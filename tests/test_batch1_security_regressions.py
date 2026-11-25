"""Batch 1 security regressions for Dynamics Horizons.

Covers:
- Bilibili `validate_bilibili_config` and `HorizonsStore` strip `bili_jct` / `csrf`
  before persisting into `sources.config_json`.
- Explicit proxy mode SSRF: does not mis-kill explicitly configured local proxy
  (e.g. 127.0.0.1:7890 / localhost), still blocks cloud-metadata proxies and blocks
  private/loopback/metadata target domains & redirect hops.
- `validate_url_sync` does not execute blocking DNS/DoH on a running asyncio event loop.
- `HorizonsWebAPI` unified `require_series_admin` + CSRF enforcement on POST endpoints.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_dynamics_horizons.adapters import bilibili as bili_mod
from astrbot_plugin_dynamics_horizons.adapters import ssrf
from astrbot_plugin_dynamics_horizons.core.store import HorizonsStore


@pytest.fixture(autouse=True)
def _reset_ssrf_hooks():
    ssrf.set_dns_resolver_for_testing(None)
    ssrf.set_http_transport_for_testing(None)
    ssrf._PUBLIC_DNS_CACHE.clear()
    yield
    ssrf.set_dns_resolver_for_testing(None)
    ssrf.set_http_transport_for_testing(None)
    ssrf._PUBLIC_DNS_CACHE.clear()


# ---------------------------------------------------------------------------
# 1. Strip bili_jct before persisting into sources.config_json
# ---------------------------------------------------------------------------


def test_validate_bilibili_config_strips_bili_jct_before_persistence():
    cfg = bili_mod.validate_bilibili_config(
        {
            "mode": "recommend",
            "cookie": "SESSDATA=read_only_123; bili_jct=WRITE_TOKEN_SECRET; buvid3=b3_ok; CSRF_TOKEN=bad",
            "bili_jct": "TOP_LEVEL_SECRET",
        }
    )
    assert "bili_jct" not in cfg
    assert "bili_jct" not in cfg.get("cookie", "")
    assert "WRITE_TOKEN_SECRET" not in cfg.get("cookie", "")
    assert "CSRF_TOKEN" not in cfg.get("cookie", "")
    assert "SESSDATA=read_only_123" in cfg["cookie"]
    assert "buvid3=b3_ok" in cfg["cookie"]


def test_store_persists_sources_config_json_without_bili_jct(tmp_path: Path):
    store = HorizonsStore(tmp_path / "horizons.db")
    try:
        created = store.create_source(
            identity="p1::default",
            source_kind="bilibili",
            name="B站推荐",
            config={
                "mode": "recommend",
                "cookie": "SESSDATA=s1; bili_jct=MUST_NOT_STORE; buvid3=b1",
                "bili_jct": "MUST_NOT_STORE_2",
            },
        )
        raw_row = store.db.execute(
            "SELECT config_json FROM sources WHERE source_id=?",
            (created["source_id"],),
        ).fetchone()
        assert raw_row is not None
        raw_json = raw_row["config_json"]
        assert "bili_jct" not in raw_json
        assert "MUST_NOT_STORE" not in raw_json
        assert "SESSDATA=s1" in raw_json

        store.update_source(
            created["source_id"],
            config={
                "mode": "recommend",
                "cookie": "SESSDATA=s2; BILI_JCT=UPDATED_SECRET; buvid3=b2",
            },
        )
        raw_row2 = store.db.execute(
            "SELECT config_json FROM sources WHERE source_id=?",
            (created["source_id"],),
        ).fetchone()
        assert "bili_jct" not in raw_row2["config_json"].lower()
        assert "UPDATED_SECRET" not in raw_row2["config_json"]
        assert "SESSDATA=s2" in raw_row2["config_json"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. Explicit proxy mode SSRF (allow local proxy, block private/metadata targets)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checked_resolver_allows_explicit_local_proxy_but_blocks_private_targets():
    resolver = ssrf._CheckedResolver(allow_private=False, allowed_proxy_host="127.0.0.1")

    # Explicitly configured IP local proxy 127.0.0.1 is allowed
    proxy_records = await resolver.resolve("127.0.0.1", 7890)
    assert proxy_records and proxy_records[0]["host"] == "127.0.0.1"

    # Explicitly configured hostname proxies (localhost / docker service clash) are also allowed
    ssrf.set_dns_resolver_for_testing(
        lambda host: ["127.0.0.1"] if host == "localhost" else ["172.18.0.2"]
    )
    local_res = ssrf._CheckedResolver(allow_private=False, allowed_proxy_host="localhost")
    assert (await local_res.resolve("localhost", 7890))[0]["host"] == "127.0.0.1"
    docker_res = ssrf._CheckedResolver(allow_private=False, allowed_proxy_host="clash")
    assert (await docker_res.resolve("clash", 7890))[0]["host"] == "172.18.0.2"

    # Non-proxy target resolving to loopback or metadata is STILL blocked
    ssrf.set_dns_resolver_for_testing(lambda host: ["127.0.0.1"])
    with pytest.raises(ssrf.SSRFBlockedError):
        await resolver.resolve("evil-rebind.example.com", 443)

    ssrf.set_dns_resolver_for_testing(lambda host: ["169.254.169.254"])
    with pytest.raises(ssrf.SSRFBlockedError):
        await resolver.resolve("meta-rebind.example.com", 80)


def test_extract_proxy_host_blocks_cloud_metadata_proxies():
    assert ssrf._extract_proxy_host("http://127.0.0.1:7890") == "127.0.0.1"
    assert ssrf._extract_proxy_host("http://localhost:7890") == "localhost"
    for bad_proxy in (
        "http://169.254.169.254:80",
        "http://100.100.100.200:80",
        "http://metadata.google.internal:80",
        "http://0.0.0.0:7890",
    ):
        with pytest.raises(ssrf.SSRFBlockedError):
            ssrf._extract_proxy_host(bad_proxy)


@pytest.mark.asyncio
async def test_safe_http_get_proxy_mode_blocks_private_and_metadata_targets_and_redirects(
    monkeypatch,
):
    called_urls: list[str] = []

    async def fake_single_get(url, headers, timeout, max_bytes, allow_private, *, session=None, proxy=None):
        called_urls.append(url)
        assert proxy == "http://127.0.0.1:7890"
        if "redirect-to-meta.example" in url:
            return ssrf.HttpResponse(
                status=302,
                body=b"",
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                url=url,
            )
        return ssrf.HttpResponse(status=200, body=b"ok", headers={}, url=url)

    monkeypatch.setattr(ssrf, "_aiohttp_single_get", fake_single_get)

    def fake_dns(host: str) -> list[str]:
        if host == "internal-target.example":
            return ["10.0.0.8"]
        if host == "meta-target.example":
            return ["169.254.169.254"]
        return ["93.184.216.34"]

    ssrf.set_dns_resolver_for_testing(fake_dns)

    # 1) Public target via explicit local proxy succeeds
    resp = await ssrf.safe_http_get(
        "https://public.example/feed.xml",
        proxy="http://127.0.0.1:7890",
    )
    assert resp.status == 200
    assert called_urls == ["https://public.example/feed.xml"]

    # 2) Private DNS target blocked BEFORE forwarding to proxy
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf.safe_http_get(
            "https://internal-target.example/secret",
            proxy="http://127.0.0.1:7890",
        )

    # 3) Metadata DNS target blocked BEFORE forwarding to proxy
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf.safe_http_get(
            "https://meta-target.example/latest",
            proxy="http://127.0.0.1:7890",
        )

    # 4) Redirect to cloud metadata blocked on second hop even in proxy mode
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf.safe_http_get(
            "https://redirect-to-meta.example/start",
            proxy="http://127.0.0.1:7890",
        )


# ---------------------------------------------------------------------------
# 3. validate_url_sync never blocks a running event loop with sync DNS/DoH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_url_sync_off_event_loop_and_async_counterpart(monkeypatch):
    def fail_sync_dns(*args, **kwargs):
        raise AssertionError("Blocking socket.getaddrinfo must not run on event loop!")

    monkeypatch.setattr(socket, "getaddrinfo", fail_sync_dns)

    # require_dns=False on running loop skips blocking sync DNS while still blocking unsafe literals/hosts
    assert ssrf.validate_url_sync("https://example.com/path", require_dns=False) == "https://example.com/path"
    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.validate_url_sync("http://127.0.0.1/admin", require_dns=False)
    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.validate_url_sync("http://169.254.169.254/latest", require_dns=False)
    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.validate_url_sync("http://localhost/admin", require_dns=False)

    # require_dns=True on running loop fails fast instead of stalling event loop
    with pytest.raises(RuntimeError, match="validate_url_async"):
        ssrf.validate_url_sync("https://example.com/path", require_dns=True)

    # Async counterpart + asyncio.to_thread run safely off the event loop
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    assert await ssrf.validate_url_async("https://example.com/path", require_dns=True) == "https://example.com/path"
    assert await asyncio.to_thread(ssrf.validate_url_sync, "https://example.com/path", require_dns=True) == "https://example.com/path"


# ---------------------------------------------------------------------------
# 4. HorizonsWebAPI unified require_series_admin + CSRF enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_horizons_web_api_enforces_series_admin_and_csrf(tmp_path: Path, monkeypatch):
    import astrbot.dashboard.api.auth as auth_mod
    import astrbot_plugin_dynamics_horizons.core.web_api as web_mod
    from astrbot_plugin_dynamics_horizons.core.web_api import HorizonsWebAPI

    state = {
        "user": "admin",
        "method": "POST",
        "headers": {},
        "body": {"op": "create", "source_kind": "feed", "name": "t", "config": {"url": "https://example.com/rss"}},
    }

    class FakeRawRequest:
        @property
        def method(self):
            return state["method"]

        @property
        def headers(self):
            return state["headers"]

        @property
        def query_params(self):
            return {}

        async def body(self):
            return json.dumps(state["body"]).encode("utf-8")

    async def fake_require_user(_raw):
        if not state["user"]:
            raise PermissionError("需要 Dashboard 管理员登录")
        return state["user"]

    monkeypatch.setattr(auth_mod, "require_dashboard_user", fake_require_user)
    monkeypatch.setattr(web_mod, "request", SimpleNamespace(_request=FakeRawRequest(), username=None))
    monkeypatch.setattr(
        web_mod,
        "json_response",
        lambda data, status_code=200: (status_code, data),
    )

    store = HorizonsStore(tmp_path / "h.db")
    try:
        fake_engine = SimpleNamespace(
            store=store,
            sessions=[],
            clock=lambda: 1700000000.0,
            world_bridge=SimpleNamespace(
                discover_personas=lambda _s, _sess: [
                    {"identity": "p1::default", "persona_id": "p1", "label": "p1"}
                ]
            ),
            refresh_projection=AsyncMock(),
        )
        fake_plugin = SimpleNamespace(
            closed=False,
            engine=fake_engine,
            context=SimpleNamespace(
                get_config=lambda: {"dashboard": {"username": "admin"}},
            ),
        )
        api = HorizonsWebAPI(fake_plugin)

        # 1) POST without CSRF/Origin headers -> 403
        state["headers"] = {"Host": "dashboard.local"}
        status, _ = await api.sources_post()
        assert status == 403

        # 2) POST with cross-site Sec-Fetch-Site -> 403
        state["headers"] = {
            "Host": "dashboard.local",
            "Origin": "https://dashboard.local",
            "Sec-Fetch-Site": "cross-site",
        }
        status, _ = await api.sources_post()
        assert status == 403

        # 3) api_key: rejected on GET -> 403
        state["method"] = "GET"
        state["user"] = "api_key:secret"
        status, _ = await api.sources_get()
        assert status == 403

        # 4) Empty dashboard.username fails closed -> 403
        state["user"] = "admin"
        fake_plugin.context.get_config = lambda: {"dashboard": {"username": ""}}
        status, _ = await api.sources_get()
        assert status == 403
        fake_plugin.context.get_config = lambda: {"dashboard": {"username": "admin"}}

        # 5) Valid GET succeeds
        status, payload = await api.sources_get()
        assert status == 200 and payload["status"] == "ok"

        # 6) Valid same-origin POST succeeds and strips bili_jct before DB write
        state["method"] = "POST"
        state["headers"] = {
            "Host": "dashboard.local",
            "Origin": "https://dashboard.local",
            "Sec-Fetch-Site": "same-origin",
        }
        state["body"] = {
            "op": "create",
            "identity": "p1::default",
            "source_kind": "bilibili",
            "name": "B站源",
            "config": {
                "mode": "recommend",
                "cookie": "SESSDATA=ok_sess; bili_jct=FORBIDDEN_WRITE_TOKEN; buvid3=b3",
            },
        }
        status, payload = await api.sources_post()
        assert status == 200 and payload["status"] == "ok"
        src_id = payload["data"]["source"]["source_id"]
        row = store.db.execute(
            "SELECT config_json FROM sources WHERE source_id=?", (src_id,)
        ).fetchone()
        assert "FORBIDDEN_WRITE_TOKEN" not in row["config_json"]
        assert "bili_jct" not in row["config_json"]
    finally:
        store.close()
