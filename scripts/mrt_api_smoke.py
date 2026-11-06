"""Manual, bounded MRT connectivity check; never logs payloads or credentials."""
import os
import re
import sys
import tempfile
import json
import datetime as dt
import html
import io
import urllib.error
import urllib.parse
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


def cloudflare_code(raw, headers):
    text = raw.decode('utf-8', 'replace')
    if 'cloudflare' not in headers.get('Server', '').lower() and 'cloudflare' not in text.lower():
        return None
    plain = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    match = re.search(r'\berror(?:\s+code)?\s*[:#]?\s*(1\d{3})\b', plain, re.I)
    return match.group(1) if match else None


def report_http(stage, status, headers, path, requested_at, code=None):
    edge = 'cloudflare' in headers.get('Server', '').lower() or bool(headers.get('CF-Ray'))
    challenge = headers.get('CF-Mitigated', '').lower() == 'challenge'
    print(f'{stage}: HTTP {status}; Cloudflare edge={"yes" if edge else "no evidence"}; '
          f'challenge={"yes" if challenge else "no header evidence"}')
    # Log only validated metadata. Never echo arbitrary header/body strings.
    content_type = headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    if not re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+', content_type):
        content_type = 'unknown'
    ray = headers.get('CF-Ray', '')
    if not re.fullmatch(r'[0-9a-fA-F]{16,32}(?:-[A-Za-z]{3})?', ray):
        ray = 'unknown'
    print(f'UTC request={requested_at}; path={path}; Content-Type={content_type}; '
          f'CF-Ray={ray}; Cloudflare error code={code or "unknown"}')


def observed_open(req, timeout):
    # No redirects/retries: each client invocation makes at most one HTTP request.
    stage = 'search' if req.full_url.endswith('/search') else 'MyLink'
    if req.full_url == mcp_client.MCP:
        message = json.loads(req.data)
        stage = 'MCP ' + message['method']
        if message['method'] == 'tools/call':
            stage = 'MCP detail'
    requested_at = dt.datetime.now(dt.timezone.utc).isoformat()
    path = urllib.parse.urlsplit(req.full_url).path
    if path not in (*api.ENDPOINTS.values(), '/mcp'):
        raise ValueError('Unexpected diagnostic path')
    try:
        result = urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        report_http(stage, exc.code, exc.headers, path, requested_at, cloudflare_code(raw, exc.headers))
        # Keep the original client error handling/body parsing intact.
        raise urllib.error.HTTPError(exc.url, exc.code, exc.reason, exc.headers, io.BytesIO(raw)) from None
    report_http(stage, result.status, result.headers, path, requested_at)
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
