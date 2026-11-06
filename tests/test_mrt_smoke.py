import contextlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('mrt_smoke', Path(__file__).resolve().parents[1] / 'scripts/mrt_api_smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def response(status=200, body=None):
    return {'http_status': status, 'body': body or {}}


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'MRT_API_KEY': 'secret-test-value'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))
        self.search = response(body={'data': {'items': [
            {'itemId': 123, 'productUrl': 'https://experiences.myrealtrip.com/products/123'},
            {'itemId': 456, 'productUrl': 'https://experiences.myrealtrip.com/products/456'}]}})
        self.link = response(body={'url': 'https://myrealt.rip/test'})

    def test_success_has_strict_call_caps(self):
        client = Mock()
        client.call.return_value = {'structuredContent': {'name': 'ticket'}}
        with patch.object(smoke.api, 'request', side_effect=[self.search, self.link]) as call, patch.object(smoke.mcp_client, 'MCPClient', return_value=client):
            self.assertEqual(smoke.main(), 0)
        self.assertEqual(call.call_count, 2)
        client.start.assert_called_once_with()
        client.call.assert_called_once_with('getTnaDetail', {'gid': '123', 'url': 'https://experiences.myrealtrip.com/products/123'})

    def test_http_failures_stop_at_each_partner_stage(self):
        for status in (401, 403, 429, 500):
            for after_search in (False, True):
                with self.subTest(status=status, after_search=after_search):
                    replies = [self.search, response(status)] if after_search else [response(status)]
                    with patch.object(smoke.api, 'request', side_effect=replies) as call, patch.object(smoke.mcp_client, 'MCPClient') as factory:
                        self.assertEqual(smoke.main(), 1)
                    self.assertEqual(call.call_count, len(replies))
                    factory.assert_not_called()

    def test_no_product_skips_followups(self):
        with patch.object(smoke.api, 'request', return_value=response()) as call, patch.object(smoke.mcp_client, 'MCPClient') as factory:
            self.assertEqual(smoke.main(), 0)
        call.assert_called_once()
        factory.assert_not_called()

    def test_mcp_handshake_failure_stops_before_detail(self):
        for status in (401, 403, 429):
            client = Mock()
            client.start.side_effect = urllib.error.HTTPError('https://example.com', status, 'secret-test-value', {}, None)
            with patch.object(smoke.api, 'request', side_effect=[self.search, self.link]), patch.object(smoke.mcp_client, 'MCPClient', return_value=client):
                self.assertEqual(smoke.main(), 1)
            client.call.assert_not_called()
        self.assertNotIn('secret-test-value', self.output.getvalue())

    def test_tool_failure_is_not_retried(self):
        client = Mock()
        client.call.return_value = {'isError': True, 'content': [{'type': 'text', 'text': '429 secret-test-value'}]}
        with patch.object(smoke.api, 'request', side_effect=[self.search, self.link]), patch.object(smoke.mcp_client, 'MCPClient', return_value=client):
            self.assertEqual(smoke.main(), 1)
        client.call.assert_called_once()
        self.assertNotIn('secret-test-value', self.output.getvalue())

    def test_missing_secret_never_calls_network(self):
        with patch.dict(os.environ, {'MRT_API_KEY': ''}), patch.object(smoke.api, 'request') as call:
            self.assertEqual(smoke.main(), 1)
        call.assert_not_called()

    def test_transport_logs_status_without_credentials_and_never_retries(self):
        request = smoke.urllib.request.Request(smoke.api.API_BASE + '/v1/products/tna/search',
                                             headers={'Authorization': 'Bearer secret-test-value'})
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(request.full_url, 403, 'secret-test-value',
                                                        {'Server': 'cloudflare', 'CF-Mitigated': 'challenge',
                                                         'Content-Type': 'text/html; charset=UTF-8',
                                                         'CF-Ray': '0123456789abcdef-ICN',
                                                         'Set-Cookie': 'secret-test-value'},
                                                        io.BytesIO(b'<h1>Cloudflare Error code: 1020</h1>secret-test-value'))
        with patch.object(smoke.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                smoke.observed_open(request, 25)
        self.assertIn(b'secret-test-value', caught.exception.read())
        opener.open.assert_called_once()
        self.assertIn('HTTP 403; Cloudflare edge=yes; challenge=yes', self.output.getvalue())
        self.assertNotIn('secret-test-value', self.output.getvalue())
        self.assertIn('Content-Type=text/html; CF-Ray=0123456789abcdef-ICN; Cloudflare error code=1020', self.output.getvalue())
        self.assertRegex(self.output.getvalue(), r'UTC request=\d{4}-\d\d-\d\dT.*\+00:00; path=/v1/products/tna/search')

    def test_metadata_rejects_unexpected_header_values(self):
        smoke.report_http('search', 403, {'Content-Type': 'secret-test-value', 'CF-Ray': 'secret-test-value'},
                          '/v1/products/tna/search', '2026-11-06T00:00:00+00:00')
        self.assertNotIn('secret-test-value', self.output.getvalue())
        self.assertIn('Cloudflare error code=unknown', self.output.getvalue())
        self.assertIsNone(smoke.cloudflare_code(b'Error code: 1020', {}))

    def test_cloudflare_body_detection_does_not_log_body(self):
        with self.assertRaises(RuntimeError):
            smoke.check(response(403, {'non_json_body': 'secret-test-value /cdn-cgi/challenge-platform/'}), 'search')
        self.assertIn('Cloudflare block page evidence=yes', self.output.getvalue())
        self.assertNotIn('secret-test-value', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
