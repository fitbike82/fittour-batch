"""Manual, bounded MRT connectivity check; never logs payloads or credentials."""
import os
import re
import sys
import tempfile
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from mrt_collector import api_client as api, mcp_client
from mrt_collector.enrichment import decode_mcp, tool_error
from mrt_collector.link_policy import extract


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def report_http(stage, status, headers):
    edge = 'cloudflare' in headers.get('Server', '').lower() or bool(headers.get('CF-Ray'))
    challenge = headers.get('CF-Mitigated', '').lower() == 'challenge'
    print(f'{stage}: HTTP {status}; Cloudflare edge={"yes" if edge else "no evidence"}; '
          f'challenge={"yes" if challenge else "no header evidence"}')


def observed_open(req, timeout):
    # No redirects/retries: each client invocation makes at most one HTTP request.
    stage = 'search' if req.full_url.endswith('/search') else 'MyLink'
    if req.full_url == mcp_client.MCP:
        message = json.loads(req.data)
        stage = 'MCP ' + message['method']
        if message['method'] == 'tools/call':
            stage = 'MCP detail'
    try:
        result = urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        report_http(stage, exc.code, exc.headers)
        raise
    report_http(stage, result.status, result.headers)
    return result


def check(response, stage):
    status = response['http_status']
    print(f'{stage}: HTTP {status}')
    body = response['body']
    if isinstance(body, dict) and any(marker in str(body.get('non_json_body', '')).lower()
                                     for marker in ('cf-chl-', '/cdn-cgi/challenge-platform/', 'cloudflare ray id')):
        print(f'{stage}: Cloudflare block page evidence=yes')
    if not 200 <= status < 300:
        raise RuntimeError('HTTP failure')
    if isinstance(body, dict) and (body.get('error') or body.get('errors')):
        raise RuntimeError('API error')
    return body


def run():
    if not os.environ.get('MRT_API_KEY', '').strip():
        raise RuntimeError('Missing MRT_API_KEY')
    body = check(api.request(api.ENDPOINTS['tna'], {'keyword': '입장권', 'city': '서울'}), 'search')
    _, rows = api.list_candidates(body)
    product = next((row for row in rows if api.product_url(row)
                    and re.fullmatch(r'\d+', api.product_id(row))), None)
    if product is None:
        print('MyLink/MCP: skipped (no eligible search product)')
        return
    url = api.product_url(product)
    link = check(api.request(api.ENDPOINTS['mylink'], {'targetUrl': url}), 'MyLink')
    if not extract(link):
        raise RuntimeError('No verified affiliate link')
    client = mcp_client.MCPClient()
    client.start()
    detail = client.call('getTnaDetail', {'gid': api.product_id(product), 'url': url})
    if tool_error(detail, decode_mcp(detail)):
        raise RuntimeError('MCP tool error')
    print('MCP detail: success')


def main():
    # Reuse the original clients, but keep all response files in an ephemeral
    # directory. Never load .env, attach artifacts, or print exception bodies.
    try:
        with tempfile.TemporaryDirectory(prefix='mrt-smoke-') as temp:
            root = Path(temp)
            with patch.object(api, 'ROOT', root), patch.object(api, 'DATA', root / 'data'), patch.object(mcp_client, 'ROOT', root), patch.object(urllib.request, 'urlopen', observed_open):
                run()
    except Exception:
        print('MRT check stopped; no retries or further calls.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
