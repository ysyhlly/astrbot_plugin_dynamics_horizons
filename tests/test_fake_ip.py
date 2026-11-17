"""Fake-IP recovery must never grant permission to connect to private targets."""

import json
import socket
from unittest.mock import Mock

import pytest

from astrbot_plugin_dynamics_horizons.adapters import ssrf


@pytest.fixture(autouse=True)
def reset_network_hooks():
    ssrf.set_dns_resolver_for_testing(None)
    ssrf.set_http_transport_for_testing(None)
    ssrf._PUBLIC_DNS_CACHE.clear()
    yield
    ssrf.set_dns_resolver_for_testing(None)
    ssrf.set_http_transport_for_testing(None)
    ssrf._PUBLIC_DNS_CACHE.clear()


def system_dns(monkeypatch, addresses):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses
        ],
    )


def doh(monkeypatch, answers, **extra):
    payload = {
        "Status": 0,
        "Answer": [{"type": 1, "data": addr, "TTL": 30} for addr in answers],
        **extra,
    }
    opener = Mock()

    def open_request(request, **kwargs):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(payload).encode()
        response.geturl.return_value = request.full_url
        return response

    opener.open.side_effect = open_request
    monkeypatch.setattr(ssrf, "build_opener", lambda *args: opener)
    return opener


def test_fake_ip_recovers_and_caches_public_addresses(monkeypatch):
    system_dns(monkeypatch, ["198.18.0.34"])
    opener = doh(monkeypatch, ["93.184.216.34"])
    assert ssrf.validate_url_sync("https://api.bilibili.com/x", require_dns=True)
    assert ssrf.resolve_host_ips("api.bilibili.com") == ["93.184.216.34"]
    assert opener.open.call_count == 1
    ssrf._PUBLIC_DNS_CACHE["api.bilibili.com"] = (0, ("93.184.216.34",))
    assert ssrf.resolve_host_ips("api.bilibili.com") == ["93.184.216.34"]
    assert opener.open.call_count == 2


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "198.18.0.34"]
)
def test_doh_private_answers_stay_blocked(monkeypatch, address):
    system_dns(monkeypatch, ["198.18.0.34"])
    doh(monkeypatch, ["93.184.216.34", address])
    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.validate_url_sync("https://example.com/", require_dns=True)


def test_literal_fake_ip_is_never_recovered(monkeypatch):
    resolver = Mock(side_effect=AssertionError("must not resolve literal"))
    monkeypatch.setattr(ssrf, "_resolve_public_dns", resolver)
    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.validate_url_sync("http://198.18.0.34/")
    resolver.assert_not_called()


@pytest.mark.parametrize("addresses", [["10.0.0.1"], ["198.18.0.34", "10.0.0.1"]])
def test_private_system_dns_is_not_replaced(monkeypatch, addresses):
    system_dns(monkeypatch, addresses)
    resolver = Mock(side_effect=AssertionError("must not bypass private DNS"))
    monkeypatch.setattr(ssrf, "_resolve_public_dns", resolver)
    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.validate_url_sync("https://example.com/")
    resolver.assert_not_called()


def test_public_dns_needs_no_fallback(monkeypatch):
    system_dns(monkeypatch, ["93.184.216.34"])
    monkeypatch.setattr(ssrf, "_resolve_public_dns", Mock(side_effect=AssertionError()))
    assert ssrf.resolve_host_ips("example.com") == ["93.184.216.34"]


@pytest.mark.parametrize("status,answers", [(3, []), (0, [])])
def test_failed_doh_fails_closed_even_for_optional_dns(monkeypatch, status, answers):
    system_dns(monkeypatch, ["198.18.0.34"])
    opener = doh(monkeypatch, answers, Status=status)
    with pytest.raises(ssrf.SSRFBlockedError, match="Fake-IP"):
        ssrf.validate_url_sync("https://example.com/", require_dns=False)
    assert opener.open.call_count == 2


def test_doh_timeout_fails_closed(monkeypatch):
    system_dns(monkeypatch, ["198.18.0.34"])
    opener = doh(monkeypatch, [])
    opener.open.side_effect = TimeoutError()
    with pytest.raises(ssrf.SSRFBlockedError, match="Fake-IP"):
        ssrf.resolve_host_ips("example.com")


def test_doh_redirect_handler_refuses_redirect():
    assert (
        ssrf._NoDNSRedirect().redirect_request(
            None, None, 302, "", {}, "http://127.0.0.1/"
        )
        is None
    )


@pytest.mark.asyncio
async def test_connector_uses_real_ip_and_keeps_tls_hostname(monkeypatch):
    system_dns(monkeypatch, ["198.18.0.34"])
    doh(monkeypatch, ["93.184.216.34"])
    records = await ssrf._CheckedResolver().resolve("example.com", 443)
    assert records[0]["host"] == "93.184.216.34"
    assert records[0]["hostname"] == "example.com"
    assert records[0]["port"] == 443


@pytest.mark.asyncio
async def test_rebinding_between_precheck_and_connection_is_blocked():
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    ssrf.set_dns_resolver_for_testing(lambda host: next(answers))
    await ssrf.validate_url_async("https://example.com/")
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf._CheckedResolver().resolve("example.com", 443)


@pytest.mark.asyncio
async def test_private_opds_opt_in_keeps_metadata_blocked():
    ssrf.set_dns_resolver_for_testing(lambda host: ["192.168.1.20"])
    assert (await ssrf._CheckedResolver(True).resolve("books.lan", 80))[0][
        "host"
    ] == "192.168.1.20"
    ssrf.set_dns_resolver_for_testing(lambda host: ["169.254.169.254"])
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf._CheckedResolver(True).resolve("books.lan", 80)


@pytest.mark.asyncio
async def test_fake_ip_fetch_does_not_follow_private_redirect(monkeypatch):
    system_dns(monkeypatch, ["198.18.0.34"])
    doh(monkeypatch, ["93.184.216.34"])
    calls = []

    async def transport(url, headers, timeout):
        calls.append(url)
        return ssrf.HttpResponse(
            status=302, body=b"", headers={"Location": "http://127.0.0.1/"}, url=url
        )

    ssrf.set_http_transport_for_testing(transport)
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf.safe_http_get("https://example.com/")
    assert calls == ["https://example.com/"]
