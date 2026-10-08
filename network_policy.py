"""Private-network HTTP transport with DNS pinning and verified TLS.

There is deliberately no public-cloud or insecure-TLS bypass. Ambient proxy
variables are never read. Only loopback works without an exact origin allowlist.
"""
import http.client
import ipaddress
import json
import socket
import ssl
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from engine import ValidationError

_SCOPES = {'vision': 'VISION', 'camera': 'CAMERA', 'monitor': 'MONITOR', 'llm': 'LLM'}
_LAN = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))


def _address(host):
    try:
        value = ipaddress.ip_address(host)
        # IPv4-mapped IPv6 must pass exactly the same boundary as IPv4.
        return value.ipv4_mapped if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped else value
    except ValueError:
        return None


def _allowed_ip(host):
    value = _address(host)
    return value is not None and (value.is_loopback or any(value.version == n.version and value in n for n in _LAN))


def _local_host(host):
    value = _address(host)
    return host == 'localhost' or value is not None and value.is_loopback


def _parse(url):
    if not isinstance(url, str) or not url or len(url) > 4000 or any(ord(c) <= 32 or ord(c) == 127 for c in url) or '\\' in url:
        raise ValidationError('服务地址含无效字符或过长。')
    try:
        parsed = urlsplit(url)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        host = parsed.hostname
        if not host or '%' in host or not host.isascii() or host.endswith('.'):
            raise ValueError()
    except ValueError:
        raise ValidationError('服务主机或端口无效。') from None
    if parsed.scheme not in ('http', 'https') or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValidationError('只允许不含账号密码或片段的HTTP(S)地址。')
    if not 1 <= port <= 65535:
        raise ValidationError('服务端口无效。')
    authority = '[' + host + ']' if ':' in host else host
    origin = f'{parsed.scheme}://{authority}:{port}'
    return parsed, host, port, origin


def is_loopback_url(url):
    return _local_host(_parse(url)[1])


def _origins(config, purpose):
    key = _SCOPES[purpose] + '_TRUSTED_ORIGINS'
    raw = (config or {}).get(key, '')
    if isinstance(raw, str):
        if raw.lstrip().startswith('['):
            try:
                raw = json.loads(raw)
            except ValueError:
                raise ValidationError(key + '必须为origin数组或逗号分隔列表。') from None
        else:
            raw = [v.strip() for v in raw.split(',') if v.strip()]
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValidationError(key + '必须为最多100个origin的列表。')
    result = set()
    for item in raw:
        parsed, _, _, origin = _parse(item)
        if parsed.path or parsed.query or item != origin:
            raise ValidationError(key + '必须使用完整且精确的scheme://host:port，不含路径。')
        result.add(origin)
    return result


def _target(url, purpose, config, resolve):
    if purpose not in _SCOPES:
        raise ValidationError('未知网络访问用途。')
    parsed, host, port, origin = _parse(url)
    if purpose in ('vision', 'llm') and parsed.query:
        raise ValidationError('模型地址不能包含查询参数。')
    local = _local_host(host)
    literal = _address(host)
    if literal is not None and not _allowed_ip(host):
        raise ValidationError('禁止连接公网、链路本地、元数据或保留地址。')
    if not local and (parsed.scheme != 'https' or origin not in _origins(config, purpose)):
        raise ValidationError('仅默认允许本机服务；校内服务须使用HTTPS并在对应TRUSTED_ORIGINS中明确授权。')
    addresses = []
    if resolve:
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except OSError:
            raise ValidationError('服务地址无法解析。') from None
        if not addresses or any(family not in (socket.AF_INET, socket.AF_INET6) or not _allowed_ip(sockaddr[0])
                                or (local and not _address(sockaddr[0]).is_loopback)
                                for family, _, _, _, sockaddr in addresses):
            raise ValidationError('DNS结果包含非授权地址，已阻止网络连接。')
    return parsed, host, port, addresses


def validate_url(url, purpose, config=None, resolve=False):
    """Validate saved URL; fetching must always repeat resolution and pin it."""
    _target(url, purpose, config, resolve)
    return url


def _connect(address, timeout):
    family, socktype, proto, _, sockaddr = address
    connection = socket.socket(family, socktype, proto)
    try:
        connection.settimeout(timeout)
        connection.connect(sockaddr)
        return connection
    except BaseException:
        connection.close()
        raise


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, timeout):
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self):
        self.sock = _connect(self._address, self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    def connect(self):
        raw = _connect(self._address, self.timeout)
        try:
            # Connect to a previously checked IP, but verify the original hostname.
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class NetworkPolicy:
    def __init__(self, config=None):
        self.config = config or {}

    @contextmanager
    def open(self, request, *, purpose, timeout=15):
        if isinstance(request, str):
            request = Request(request)
        parsed, host, port, addresses = _target(request.full_url, purpose, self.config, True)
        cls = _PinnedHTTPS if parsed.scheme == 'https' else _PinnedHTTP
        connection = cls(host, port, addresses[0], timeout)
        response = None
        try:
            # Never permit a caller-supplied authority/proxy header or tunneling.
            headers = {k: v for k, v in request.header_items()
                       if k.lower() not in ('host', 'proxy-authorization', 'proxy-connection', 'connection')}
            headers['Connection'] = 'close'
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            connection.request(request.get_method(), target, body=request.data, headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise HTTPError('', response.status, '服务响应被拒绝（不跟随重定向）', response.headers, None)
            yield response
        except http.client.HTTPException:
            raise ValidationError('服务未返回有效HTTP响应，已停止访问。') from None
        finally:
            if response is not None:
                response.close()
            connection.close()
