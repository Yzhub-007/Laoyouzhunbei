"""Privacy boundary contracts, including transport-level DNS pinning."""
import io
import os
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

import network_policy as net
from engine import ValidationError


def address(ip='10.1.2.3', port=443):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port))


class NetworkPolicyTests(unittest.TestCase):
    def test_loopback_is_default_for_every_sensitive_purpose(self):
        for purpose in ('vision', 'camera', 'monitor', 'llm'):
            for url in ('http://localhost:8000/v1', 'http://127.0.0.1/v1', 'http://[::1]/v1'):
                self.assertEqual(net.validate_url(url, purpose), url)
            for url in ('https://service.example/v1', 'http://10.1.2.3/v1', 'https://10.1.2.3/v1'):
                with self.subTest(purpose=purpose, url=url), self.assertRaises(ValidationError):
                    net.validate_url(url, purpose)

    def test_exact_origin_scope_and_https_are_required(self):
        config = {'VISION_TRUSTED_ORIGINS': 'https://campus.test:443'}
        net.validate_url('https://campus.test/v1', 'vision', config)
        for url, purpose in (('https://campus.test:444/v1', 'vision'), ('http://campus.test/v1', 'vision'),
                             ('https://other.campus.test/v1', 'vision'), ('https://campus.test/v1', 'camera')):
            with self.subTest(url=url, purpose=purpose), self.assertRaises(ValidationError):
                net.validate_url(url, purpose, config)
        for raw in ('https://campus.test', 'https://campus.test:443/', 'https://campus.test:443/path', '*'):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                net.validate_url('https://campus.test/v1', 'vision', {'VISION_TRUSTED_ORIGINS': raw})

    def test_only_rfc1918_ula_loopback_not_is_private_reserved_ranges(self):
        for ip in ('10.1.1.1', '172.16.0.1', '192.168.1.1', 'fd00::1', 'fc00::1', '127.0.0.1', '::1'):
            self.assertTrue(net._allowed_ip(ip), ip)
        for ip in ('8.8.8.8', '169.254.169.254', '169.254.1.1', '0.0.0.0', '::', 'fe80::1', 'ff02::1',
                   '192.0.2.1', '198.51.100.1', '203.0.113.1', '100.64.0.1', '2001:db8::1', '224.0.0.1'):
            self.assertFalse(net._allowed_ip(ip), ip)
            host = '[' + ip + ']' if ':' in ip else ip
            with self.subTest(ip=ip), self.assertRaises(ValidationError):
                net.validate_url('https://' + host + '/v1', 'vision', {'VISION_TRUSTED_ORIGINS': 'https://' + host + ':443'})
        self.assertFalse(net._allowed_ip('::ffff:169.254.169.254'))
        self.assertFalse(net._allowed_ip('::ffff:8.8.8.8'))

    def test_all_dns_answers_must_be_allowed_before_connect(self):
        config = {'VISION_TRUSTED_ORIGINS': 'https://campus.test:443'}
        for answers in ([address(), address('8.8.8.8')], [address('169.254.169.254')], []):
            with patch.object(net.socket, 'getaddrinfo', return_value=answers), patch.object(net, '_connect') as connect:
                with self.assertRaises(ValidationError):
                    with net.NetworkPolicy(config).open('https://campus.test/v1', purpose='vision'):
                        pass
                connect.assert_not_called()
        with patch.object(net.socket, 'getaddrinfo', return_value=[address()]):
            with self.assertRaises(ValidationError):
                net.validate_url('http://localhost/v1', 'vision', resolve=True)

    def test_connection_uses_checked_sockaddr_without_second_dns_lookup(self):
        chosen = address('10.20.30.40', 8443)
        with patch.object(net.socket, 'socket') as make_socket, patch.object(net.socket, 'getaddrinfo') as lookup:
            connection = net._PinnedHTTP('campus.test', 8443, chosen, timeout=7)
            connection.connect()
            make_socket.return_value.connect.assert_called_once_with(('10.20.30.40', 8443))
            make_socket.return_value.settimeout.assert_called_once_with(7)
            lookup.assert_not_called()

    def test_tls_verifies_original_hostname_on_pinned_socket(self):
        raw = MagicMock()
        context = MagicMock()
        with patch.object(net, '_connect', return_value=raw) as connect, patch.object(net.ssl, 'create_default_context', return_value=context) as make_context:
            connection = net._PinnedHTTPS('campus.test', 443, address(), timeout=7)
            connection.connect()
            connect.assert_called_once_with(address(), 7)
            make_context.assert_called_once_with()
            context.wrap_socket.assert_called_once_with(raw, server_hostname='campus.test')
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('certificate hostname mismatch')
        with patch.object(net, '_connect', return_value=raw), patch.object(net.ssl, 'create_default_context', return_value=context):
            with self.assertRaises(ssl.SSLCertVerificationError):
                net._PinnedHTTPS('campus.test', 443, address(), timeout=7).connect()
        raw.close.assert_called_once()

    def test_proxy_environment_and_host_header_cannot_change_destination(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'https://attacker.example:8443', 'ALL_PROXY': 'http://attacker.example:8080'}), \
             patch.object(net.socket, 'getaddrinfo', return_value=[address()]) as lookup, \
             patch.object(net, '_PinnedHTTPS') as connection:
            connection.return_value.getresponse.return_value.status = 200
            request = Request('https://campus.test/v1', data=b'private', headers={'Host': 'attacker.example', 'Proxy-Authorization': 'secret'})
            with net.NetworkPolicy({'VISION_TRUSTED_ORIGINS': 'https://campus.test:443'}).open(request, purpose='vision'):
                pass
            connection.assert_called_once_with('campus.test', 443, address(), 15)
            lookup.assert_called_once_with('campus.test', 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
            call = connection.return_value.request.call_args
            self.assertEqual(call.args, ('POST', '/v1'))
            self.assertEqual(call.kwargs['headers'], {'Connection': 'close'})

    def test_redirect_is_rejected_without_followup(self):
        with patch.object(net.socket, 'getaddrinfo', return_value=[address('127.0.0.1', 80)]), patch.object(net, '_PinnedHTTP') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 302
            response.headers = {'Location': 'https://public.example/private-upload'}
            with self.assertRaises(HTTPError):
                with net.NetworkPolicy().open('http://127.0.0.1/snapshot', purpose='camera'):
                    pass
            connection.return_value.request.assert_called_once()
            response.close.assert_called_once()

    def test_malformed_and_credential_urls_rejected_without_leaking_them(self):
        for url in ('http://localhost:0/a', 'http://localhost:99999/a', 'http://user:private@localhost/a',
                    'http://localhost/a#fragment', 'http://localhost/\r\nInjected: true', 'http://[fe80::1%25eth0]/a'):
            with self.subTest(url=url), self.assertRaises(ValidationError) as caught:
                net.validate_url(url, 'camera')
            self.assertNotIn('private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
