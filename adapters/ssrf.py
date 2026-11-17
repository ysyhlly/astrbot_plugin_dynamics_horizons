"""SSRF protection, safe async HTTP transport, secret masking, and untrusted text sanitization."""

from __future__ import annotations

import asyncio
import copy
import html
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import aiohttp

MASK_TOKEN = "********"
DEFAULT_USER_AGENT = "DynamicsHorizons/0.1 (AstrBot ReadOnly Source Adapter)"

_SECRET_EXACT_KEYS = frozenset(
    {
        "cookie",
        "cookies",
        "sessdata",
        "bili_jct",
        "ac_time_value",
        "password",
        "passwd",
        "pass",
        "auth_token",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "api_key",
        "private_key",
        "authorization",
        "basic_password",
    }
)
_SECRET_SUFFIXES = ("_cookie", "_token", "_secret", "_password", "_passwd", "_api_key")

_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "169.254.169.254",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".lan", ".home.arpa", ".internal")
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_PUBLIC_DNS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_ALIBABA_META_IP = ipaddress.ip_address("100.100.100.200")
_AWS_IPV6_META_NET = ipaddress.ip_network("fd00:ec2::/32")

_OBFUSCATED_IP_RE = re.compile(
    r"^(?:0x[0-9a-f]+|0[0-7]+|\d+)(?:\.(?:0x[0-9a-f]+|0[0-7]+|\d+)){0,3}$",
    re.IGNORECASE,
)
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_DROP_BLOCK_RE = re.compile(
    r"<(script|style|noscript|iframe|object|embed)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_BR_BLOCK_RE = re.compile(
    r"</?(?:p|div|br|li|tr|h[1-6]|blockquote|section|article|pre|hr)\b[^>]*>",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"disregard\s+(?:all\s+)?(?:previous|prior|system)\s+(?:instructions?|prompts?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:system\s*prompt|developer\s*mode|jailbreak|越狱模式)", re.IGNORECASE
    ),
    re.compile(
        r"(?:忽略|无视|跳过)(?:以上|之前|前面|先前|所有|全部|一切)+(?:的)?(?:指令|提示词|规则|设定|约束)"
    ),
    re.compile(r"你现在(?:是|扮演)(?:一个)?不受(?:任何)?限制"),
    re.compile(
        r"<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>", re.IGNORECASE
    ),
    re.compile(r"\[/?INST\]", re.IGNORECASE),
    re.compile(
        r"</?(?:untrusted_content|external_media|system_instruction|system_prompt)\b[^>]*>",
        re.IGNORECASE,
    ),
)

_COOKIE_KV_REDACT_RE = re.compile(
    r"(?i)\b(SESSDATA|bili_jct|ac_time_value|sid|DedeUserID__ckMd5|auth_token|password|token)=([^;\s\"'&]+)"
)
_AUTH_HEADER_REDACT_RE = re.compile(r"(?i)\b(Authorization|Cookie)\s*:\s*([^\r\n]+)")
_BEARER_BASIC_REDACT_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}")


class SSRFBlockedError(ValueError):
    """Raised when a target URL, redirect hop, or resolved IP violates SSRF policy."""


class AdapterRateLimitError(RuntimeError):
    """Raised or caught when a remote source responds with HTTP 429 or risk-control throttling."""

    def __init__(
        self, message: str, *, retry_after: float = 300.0, status_code: int = 429
    ):
        super().__init__(message)
        self.retry_after = max(5.0, min(float(retry_after), 86400.0))
        self.status_code = int(status_code)


class AdapterHTTPError(RuntimeError):
    """Raised on non-200/304 HTTP responses with redacted message."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = int(status_code)


@dataclass
class HttpResponse:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""

    def header(self, name: str, default: str = "") -> str:
        target = name.lower()
        for k, v in self.headers.items():
            if k.lower() == target:
                return str(v)
        return default

    def text(self, default_encoding: str = "utf-8") -> str:
        ctype = self.header("content-type", "")
        m = re.search(r"charset=([A-Za-z0-9._-]+)", ctype, re.IGNORECASE)
        candidates: list[str] = []
        if m:
            candidates.append(m.group(1).strip("\"'"))
        candidates.extend([default_encoding, "utf-8-sig", "gb18030"])
        seen: set[str] = set()
        for enc in candidates:
            low = enc.lower()
            if low in seen:
                continue
            seen.add(low)
            try:
                return self.body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", errors="replace")


TransportCallable = Callable[[str, Mapping[str, str], float], Awaitable[HttpResponse]]
DnsResolverCallable = Callable[[str], Sequence[str]]

_http_transport: TransportCallable | None = None
_dns_resolver: DnsResolverCallable | None = None


def set_http_transport_for_testing(transport: TransportCallable | None) -> None:
    """Install or clear an offline async HTTP transport for unit tests."""
    global _http_transport
    _http_transport = transport


def set_dns_resolver_for_testing(resolver: DnsResolverCallable | None) -> None:
    """Install or clear a custom hostname->IP list resolver for unit tests."""
    global _dns_resolver
    _dns_resolver = resolver


def is_secret_field(key: str) -> bool:
    low = str(key or "").strip().lower()
    if low in _SECRET_EXACT_KEYS:
        return True
    return any(low.endswith(sfx) for sfx in _SECRET_SUFFIXES)


def is_masked_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    if stripped == MASK_TOKEN:
        return True
    return len(stripped) >= 4 and set(stripped) <= {"*", "•"}


def mask_secrets_in_dict(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a deep copy of `data` with all credential/cookie/password values replaced by `********`."""
    if not isinstance(data, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        key_str = str(k)
        if is_secret_field(key_str):
            if isinstance(v, str):
                out[key_str] = MASK_TOKEN if v.strip() else ""
            elif v:
                out[key_str] = MASK_TOKEN
            else:
                out[key_str] = ""
        elif isinstance(v, Mapping):
            out[key_str] = mask_secrets_in_dict(v)
        elif isinstance(v, list):
            out[key_str] = [
                mask_secrets_in_dict(item)
                if isinstance(item, Mapping)
                else copy.deepcopy(item)
                for item in v
            ]
        elif isinstance(v, str) and (
            "@" in v and (v.startswith("http://") or v.startswith("https://"))
        ):
            try:
                sp = urlsplit(v)
                if sp.password:
                    netloc = f"{sp.username}:{MASK_TOKEN}@{sp.hostname or ''}"
                    if sp.port:
                        netloc += f":{sp.port}"
                    out[key_str] = urlunsplit(
                        (sp.scheme, netloc, sp.path, sp.query, "")
                    )
                else:
                    out[key_str] = v
            except ValueError:
                out[key_str] = v
        else:
            out[key_str] = copy.deepcopy(v)
    return out


def merge_secret_config(
    incoming: Mapping[str, Any] | None,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preserve existing unmasked secrets when incoming config carries `********` or `preserve_secret=True`."""
    result = dict(incoming or {})
    preserve_flag = bool(result.pop("preserve_secret", False))
    old = existing if isinstance(existing, Mapping) else {}
    for k, v in list(result.items()):
        if is_secret_field(k) and is_masked_secret(v):
            prev = old.get(k, "")
            result[k] = (
                prev if isinstance(prev, str) and not is_masked_secret(prev) else ""
            )
        elif isinstance(v, Mapping):
            prev_sub = old.get(k) if isinstance(old.get(k), Mapping) else None
            result[k] = merge_secret_config(v, prev_sub)
    if preserve_flag:
        for k, prev in old.items():
            if (
                is_secret_field(k)
                and not result.get(k)
                and isinstance(prev, str)
                and not is_masked_secret(prev)
            ):
                result[k] = prev
    return result


def redact_error_text(text: Any, secrets: Iterable[str] = ()) -> str:
    """Redact cookies, tokens, Authorization headers, and explicit secret values from status/error text."""
    msg = str(text or "")
    for secret in secrets:
        s = str(secret or "").strip()
        if len(s) >= 4 and not is_masked_secret(s):
            msg = msg.replace(s, "[REDACTED]")
    msg = _AUTH_HEADER_REDACT_RE.sub(r"\1: [REDACTED]", msg)
    msg = _BEARER_BASIC_REDACT_RE.sub(r"\1 [REDACTED]", msg)
    msg = _COOKIE_KV_REDACT_RE.sub(r"\1=[REDACTED]", msg)
    msg = _CONTROL_CHARS_RE.sub(" ", msg)
    return re.sub(r"\s+", " ", msg).strip()[:280]


def _check_ip_address(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool = False,
) -> None:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        _check_ip_address(ip_obj.ipv4_mapped, allow_private=allow_private)
        return

    # Always block unspecified, multicast, link-local (169.254.0.0/16 & fe80::/10), and cloud metadata IPs
    if ip_obj.is_unspecified or ip_obj.is_multicast or ip_obj.is_link_local:
        raise SSRFBlockedError(f"安全策略拦截受限地址：{ip_obj}")
    if ip_obj == _ALIBABA_META_IP:
        raise SSRFBlockedError(f"安全策略拦截云元数据地址：{ip_obj}")
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj in _AWS_IPV6_META_NET:
        raise SSRFBlockedError(f"安全策略拦截云元数据地址：{ip_obj}")

    if allow_private:
        # Admin explicitly enabled allow_private_opds for self-hosted LAN/local Calibre-Web or Talebook.
        # Loopback and RFC1918/ULA/CGNAT (except cloud metadata) are allowed; reserved non-private blocks are not.
        if ip_obj.is_reserved and not (ip_obj.is_loopback or ip_obj.is_private):
            raise SSRFBlockedError(f"安全策略拦截保留地址：{ip_obj}")
        return

    if ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_reserved:
        raise SSRFBlockedError(f"安全策略禁止访问内网或本机地址：{ip_obj}")
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NETWORK:
        raise SSRFBlockedError(f"安全策略禁止访问运营商内网地址：{ip_obj}")
    if not ip_obj.is_global:
        raise SSRFBlockedError(f"安全策略仅允许公网地址：{ip_obj}")


class _NoDNSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _resolve_public_dns(hostname: str) -> list[str]:
    """Recover real IPv4 addresses from Fake-IP DNS, never trusting the fake address.

    Fixed HTTPS IP endpoints avoid the intercepted system DNS. TLS verification
    stays enabled, environment proxies are ignored, and only global answers pass.
    """
    cached = _PUBLIC_DNS_CACHE.get(hostname)
    if cached and cached[0] > time.monotonic():
        return list(cached[1])
    opener = build_opener(ProxyHandler({}), _NoDNSRedirect())
    for endpoint in ("https://1.1.1.1/dns-query", "https://1.0.0.1/dns-query"):
        try:
            request = Request(
                endpoint + "?" + urlencode({"name": hostname, "type": "A"}),
                headers={"Accept": "application/dns-json"},
            )
            with opener.open(request, timeout=4) as response:
                # A resolver redirect must not turn this into a general URL fetch.
                if response.geturl() != request.full_url:
                    raise ValueError("DNS endpoint redirected")
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("DNS response too large")
            payload = json.loads(raw)
            if payload.get("Status") != 0 or payload.get("TC"):
                raise ValueError("DNS query failed")
            ips: list[str] = []
            ttl = 60
            for answer in payload.get("Answer", []):
                ttl = min(ttl, max(0, int(answer.get("TTL", 0))))
                if answer.get("type") != 1:
                    continue
                addr = ipaddress.IPv4Address(answer["data"])
                _check_ip_address(addr)
                if str(addr) not in ips:
                    ips.append(str(addr))
            if not ips:
                raise ValueError("DNS response has no public IPv4 addresses")
            if len(_PUBLIC_DNS_CACHE) >= 256:
                _PUBLIC_DNS_CACHE.clear()
            _PUBLIC_DNS_CACHE[hostname] = (time.monotonic() + ttl, tuple(ips))
            return ips
        except SSRFBlockedError:
            raise
        except Exception:
            continue
    raise SSRFBlockedError(
        f"域名 {hostname} 使用 Fake-IP，但安全公网 DNS 解析失败；请检查加密 DNS 连通性"
    )


def resolve_host_ips(hostname: str) -> list[str]:
    """Resolve hostname to unique IP strings via test resolver or `socket.getaddrinfo`."""
    if _dns_resolver is not None:
        return [str(ip) for ip in _dns_resolver(hostname)]
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    ips: list[str] = []
    for item in infos:
        sockaddr = item[4]
        if sockaddr and isinstance(sockaddr[0], str):
            addr = sockaddr[0].split("%", 1)[0]
            if addr not in ips:
                ips.append(addr)
    if any(ipaddress.ip_address(addr) in _FAKE_IP_NETWORK for addr in ips):
        # A mixed private/Fake-IP answer must not hide an actual private target.
        for addr in ips:
            ip_obj = ipaddress.ip_address(addr)
            if ip_obj not in _FAKE_IP_NETWORK:
                _check_ip_address(ip_obj)
        return _resolve_public_dns(hostname)
    return ips


async def async_resolve_host_ips(hostname: str) -> list[str]:
    if _dns_resolver is not None:
        return [str(ip) for ip in _dns_resolver(hostname)]
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolve_host_ips, hostname)


def _validate_parsed_url(
    url: str,
    *,
    allow_private: bool = False,
) -> tuple[str, str]:
    if not isinstance(url, str):
        raise SSRFBlockedError("URL 必须是字符串")
    cleaned = url.strip()
    if not cleaned:
        raise SSRFBlockedError("URL 不能为空")
    if len(cleaned) > 2048:
        raise SSRFBlockedError("URL 长度超出限制")
    if any(ch in cleaned for ch in ("\r", "\n", "\t", "\x00")):
        raise SSRFBlockedError("URL 含有非法控制字符")

    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise SSRFBlockedError("URL 格式无效") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise SSRFBlockedError(f"仅允许 HTTP/HTTPS 协议，已拦截：{scheme or 'unknown'}")

    if parsed.username is not None or parsed.password is not None:
        raise SSRFBlockedError("URL 不得内嵌账号或密码，请在鉴权字段中配置")

    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFBlockedError("URL 端口无效") from exc
    if port is not None and not (1 <= port <= 65535):
        raise SSRFBlockedError("URL 端口超出范围")

    raw_host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not raw_host:
        raise SSRFBlockedError("URL 缺少有效主机名")

    if raw_host in _METADATA_HOSTS or raw_host.endswith(".metadata.google.internal"):
        raise SSRFBlockedError(f"安全策略拦截云元数据主机：{raw_host}")

    # Check IP literal or obfuscated numeric/octal/hex IP notation
    ip_literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip_literal = ipaddress.ip_address(raw_host)
    except ValueError:
        if raw_host.isdigit() or _OBFUSCATED_IP_RE.match(raw_host):
            raise SSRFBlockedError(
                f"安全策略拦截非标准数值 IP 表达：{raw_host}"
            ) from None

    if ip_literal is not None:
        _check_ip_address(ip_literal, allow_private=allow_private)
    else:
        if raw_host == "localhost" or raw_host.endswith(_LOCAL_HOST_SUFFIXES):
            if not allow_private:
                raise SSRFBlockedError(f"安全策略禁止访问本地或内网主机名：{raw_host}")
        try:
            ascii_host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SSRFBlockedError("主机名 IDNA 编码无效") from exc
        labels = ascii_host.split(".")
        if not allow_private and len(labels) < 2:
            raise SSRFBlockedError(f"安全策略禁止单标签内网主机名：{raw_host}")
        for label in labels:
            if not _DOMAIN_LABEL_RE.match(label):
                raise SSRFBlockedError(f"主机名格式不合法：{raw_host}")

    normalized = urlunsplit(
        (
            scheme,
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )
    return normalized, raw_host


def validate_url_sync(
    url: str,
    *,
    allow_private: bool = False,
    require_dns: bool = False,
) -> str:
    """Validate URL scheme/host/IP and check DNS resolution against private/loopback/metadata ranges."""
    normalized, host = _validate_parsed_url(url, allow_private=allow_private)
    try:
        ipaddress.ip_address(host)
        return normalized
    except ValueError:
        pass

    try:
        ips = resolve_host_ips(host)
    except socket.gaierror as exc:
        if require_dns:
            raise SSRFBlockedError(f"域名无法解析：{host}") from exc
        return normalized

    if not ips and require_dns:
        raise SSRFBlockedError(f"域名解析结果为空：{host}")
    for ip_str in ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise SSRFBlockedError(f"域名解析返回异常地址：{ip_str}") from exc
        _check_ip_address(ip_obj, allow_private=allow_private)
    return normalized


async def validate_url_async(
    url: str,
    *,
    allow_private: bool = False,
    require_dns: bool = True,
) -> str:
    """Async counterpart of `validate_url_sync` used immediately before every HTTP hop."""
    normalized, host = _validate_parsed_url(url, allow_private=allow_private)
    try:
        ipaddress.ip_address(host)
        return normalized
    except ValueError:
        pass

    try:
        ips = await async_resolve_host_ips(host)
    except socket.gaierror as exc:
        if _http_transport is not None and _dns_resolver is None:
            return normalized
        if require_dns:
            raise SSRFBlockedError(f"域名无法解析：{host}") from exc
        return normalized

    if (
        not ips
        and require_dns
        and not (_http_transport is not None and _dns_resolver is None)
    ):
        raise SSRFBlockedError(f"域名解析结果为空：{host}")
    for ip_str in ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise SSRFBlockedError(f"域名解析返回异常地址：{ip_str}") from exc
        _check_ip_address(ip_obj, allow_private=allow_private)
    return normalized


def parse_retry_after(
    header_value: str | None, *, now: float | None = None, default: float = 300.0
) -> float:
    """Parse HTTP `Retry-After` (delta-seconds or HTTP-date) into bounded seconds."""
    if not header_value:
        return default
    raw = str(header_value).strip()
    if not raw:
        return default
    try:
        secs = float(raw)
        if secs >= 0:
            return max(5.0, min(secs, 86400.0))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        ref = time.time() if now is None else now
        delta = dt.timestamp() - ref
        return max(5.0, min(delta, 86400.0))
    except (TypeError, ValueError, OverflowError):
        return default


def _origin_tuple(url: str) -> tuple[str, str, int]:
    sp = urlsplit(url)
    scheme = (sp.scheme or "http").lower()
    host = (sp.hostname or "").lower()
    port = sp.port or (443 if scheme == "https" else 80)
    return (scheme, host, port)


class _CheckedResolver(aiohttp.abc.AbstractResolver):
    """Validate the addresses actually supplied to the connector (no second DNS lookup)."""

    def __init__(self, allow_private: bool = False):
        self.allow_private = allow_private

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        ips = await async_resolve_host_ips(host)
        if not ips:
            raise SSRFBlockedError(f"域名解析结果为空：{host}")
        records = []
        for addr in ips:
            ip_obj = ipaddress.ip_address(addr)
            _check_ip_address(ip_obj, allow_private=self.allow_private)
            ip_family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, ip_family):
                continue
            records.append(
                {
                    "hostname": host,
                    "host": str(ip_obj),
                    "port": port,
                    "family": ip_family,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not records:
            raise SSRFBlockedError(f"域名没有可连接的安全地址：{host}")
        return records

    async def close(self):
        pass


async def _aiohttp_single_get(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int,
    allow_private: bool = False,
) -> HttpResponse:
    client_timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout)))
    connector = aiohttp.TCPConnector(
        resolver=_CheckedResolver(allow_private), use_dns_cache=False
    )
    async with aiohttp.ClientSession(
        timeout=client_timeout, connector=connector
    ) as session:
        async with session.get(
            url, headers=dict(headers), allow_redirects=False
        ) as resp:
            resp_headers = {str(k): str(v) for k, v in resp.headers.items()}
            if resp.status in {301, 302, 303, 307, 308, 304}:
                return HttpResponse(
                    status=resp.status, body=b"", headers=resp_headers, url=url
                )
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(16384):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise ValueError(f"响应体超过大小上限 ({max_bytes} bytes)")
            return HttpResponse(
                status=resp.status, body=bytes(buf), headers=resp_headers, url=url
            )


async def safe_http_get(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 20.0,
    max_bytes: int = 2 * 1024 * 1024,
    allow_private: bool = False,
    max_redirects: int = 5,
    etag: str = "",
    last_modified: str = "",
    secrets_to_redact: Sequence[str] = (),
) -> HttpResponse:
    """Perform SSRF-guarded HTTP GET with manual hop validation, size cap, 304, and 429 Retry-After."""
    target_url = str(url or "").strip()
    if params:
        clean_params = {
            str(k): str(v) for k, v in params.items() if v is not None and str(v) != ""
        }
        if clean_params:
            sp = urlsplit(target_url)
            existing_q = sp.query
            extra_q = urlencode(clean_params)
            merged_q = f"{existing_q}&{extra_q}" if existing_q else extra_q
            target_url = urlunsplit(
                (sp.scheme, sp.netloc, sp.path or "/", merged_q, "")
            )

    req_headers: dict[str, str] = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        for k, v in headers.items():
            if v is not None and str(v) != "":
                req_headers[str(k)] = str(v)
    if etag and "If-None-Match" not in req_headers:
        req_headers["If-None-Match"] = str(etag).strip()
    if last_modified and "If-Modified-Since" not in req_headers:
        req_headers["If-Modified-Since"] = str(last_modified).strip()

    current_url = await validate_url_async(
        target_url, allow_private=allow_private, require_dns=True
    )
    initial_origin = _origin_tuple(current_url)

    for hop in range(max_redirects + 1):
        if hop > 0:
            current_url = await validate_url_async(
                current_url, allow_private=allow_private, require_dns=True
            )
            if _origin_tuple(current_url) != initial_origin:
                for sensitive in ("Authorization", "authorization", "Cookie", "cookie"):
                    req_headers.pop(sensitive, None)

        try:
            if _http_transport is not None:
                resp = await _http_transport(current_url, dict(req_headers), timeout)
                if len(resp.body) > max_bytes:
                    raise ValueError(f"响应体超过大小上限 ({max_bytes} bytes)")
                resp.url = current_url
            else:
                resp = await _aiohttp_single_get(
                    current_url, req_headers, timeout, max_bytes, allow_private
                )
        except (SSRFBlockedError, AdapterRateLimitError, ValueError):
            raise
        except Exception as exc:
            raise RuntimeError(redact_error_text(exc, secrets_to_redact)) from None

        if resp.status in {301, 302, 303, 307, 308}:
            if hop >= max_redirects:
                raise AdapterHTTPError(resp.status, "重定向次数超过安全上限")
            location = resp.header("location", "").strip()
            if not location:
                raise AdapterHTTPError(resp.status, "重定向响应缺少 Location 头")
            current_url = urljoin(current_url, location)
            continue

        if resp.status == 304:
            return resp
        if resp.status == 429:
            wait_s = parse_retry_after(resp.header("retry-after"), default=300.0)
            raise AdapterRateLimitError(
                f"远端站点触发频率限制 (HTTP 429, retry_after={int(wait_s)}s)",
                retry_after=wait_s,
                status_code=429,
            )
        if resp.status >= 400:
            raise AdapterHTTPError(resp.status, f"HTTP 请求失败 (status={resp.status})")
        return resp

    raise AdapterHTTPError(310, "重定向次数超过安全上限")


def strip_html_to_text(raw_html: str) -> str:
    """Convert raw HTML fragment into readable plain text with paragraph breaks."""
    if not raw_html:
        return ""
    text = _HTML_COMMENT_RE.sub("", str(raw_html))
    text = _HTML_DROP_BLOCK_RE.sub(" ", text)
    text = _HTML_BR_BLOCK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    return text


def clean_inline_field(value: Any, *, max_chars: int = 160) -> str:
    """Clean single-line metadata fields (`title`, `creator`, `chapter_label`)."""
    raw = (
        strip_html_to_text(str(value or ""))
        if "<" in str(value or "")
        else html.unescape(str(value or ""))
    )
    raw = _CONTROL_CHARS_RE.sub(" ", raw)
    for pat in _INJECTION_PATTERNS:
        raw = pat.sub(" ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if len(raw) > max_chars:
        return raw[: max(1, max_chars - 1)].rstrip() + "…"
    return raw


def clean_external_text(
    value: Any, *, max_chars: int = 4000, strip_html: bool = False
) -> str:
    """Clean untrusted multi-line external text, stripping control chars, HTML, and prompt-injection markers."""
    text = str(value or "")
    if not text:
        return ""
    if strip_html or ("<" in text and ">" in text):
        text = strip_html_to_text(text)
    else:
        text = html.unescape(text)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS_RE.sub("", text)
    for pat in _INJECTION_PATTERNS:
        text = pat.sub("[已过滤指令片段]", text)

    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    compact_lines: list[str] = []
    blank_run = 0
    for ln in lines:
        if not ln:
            blank_run += 1
            if blank_run <= 1 and compact_lines:
                compact_lines.append("")
        else:
            blank_run = 0
            compact_lines.append(ln)
    cleaned = "\n".join(compact_lines).strip()
    if len(cleaned) <= max_chars:
        return cleaned

    # Truncate cleanly on paragraph or sentence boundary near max_chars
    window = cleaned[:max_chars]
    for sep in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? "):
        idx = window.rfind(sep)
        if idx >= int(max_chars * 0.7):
            return window[: idx + (0 if sep.startswith("\n") else len(sep))].rstrip()
    return window.rstrip()


def safe_public_url(url: Any, *, allow_private: bool = False) -> str:
    """Return normalized safe HTTP(S) URL for candidate citation, or `''` if invalid/unsafe."""
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        return validate_url_sync(s, allow_private=allow_private, require_dns=False)
    except SSRFBlockedError:
        return ""


def normalize_candidate(
    *,
    external_id: str,
    source: Mapping[str, Any],
    title: str,
    creator: str = "",
    bvid: str = "",
    chapter_label: str = "",
    chapter_index: int | None = None,
    total_chapters: int | None = None,
    book_id: str = "",
    summary: str = "",
    raw_url: str = "",
    published_at: float | int | None = None,
    content_ready: bool = False,
    full_text: str = "",
    meta: Mapping[str, Any] | None = None,
    allow_private_url: bool = False,
) -> dict[str, Any]:
    """Build a strictly typed, sanitized `NormalizedCandidate` dictionary."""
    now = time.time()
    try:
        pub_ts = float(published_at) if published_at is not None else now
        if pub_ts <= 0:
            pub_ts = now
    except (TypeError, ValueError):
        pub_ts = now

    cleaned_full = clean_external_text(full_text, max_chars=4000) if full_text else ""
    raw_summary = summary or cleaned_full
    cleaned_summary = clean_inline_field(raw_summary, max_chars=300)
    cleaned_title = (
        clean_inline_field(title, max_chars=160)
        or clean_inline_field(cleaned_summary, max_chars=60)
        or "未命名条目"
    )
    cleaned_creator = clean_inline_field(creator, max_chars=80)
    cleaned_chapter = clean_inline_field(chapter_label, max_chars=120)
    cleaned_bvid = re.sub(r"[^0-9A-Za-z]", "", str(bvid or ""))[:24]
    safe_meta = mask_secrets_in_dict(meta) if isinstance(meta, Mapping) else {}

    return {
        "external_id": str(external_id or "").strip()[:200],
        "source_id": str(source.get("source_id") or "").strip(),
        "source_kind": str(source.get("source_kind") or "").strip(),
        "identity": str(source.get("identity") or ""),
        "title": cleaned_title,
        "creator": cleaned_creator,
        "bvid": cleaned_bvid,
        "chapter_label": cleaned_chapter,
        "chapter_index": int(chapter_index) if chapter_index is not None else None,
        "total_chapters": int(total_chapters) if total_chapters is not None else None,
        "book_id": str(book_id or "").strip()[:160],
        "summary": cleaned_summary,
        "raw_url": safe_public_url(raw_url, allow_private=allow_private_url),
        "published_at": pub_ts,
        "content_ready": bool(content_ready),
        "full_text": cleaned_full,
        "meta": safe_meta,
    }
