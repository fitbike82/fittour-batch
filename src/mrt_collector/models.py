"""Shared policy; no inferred prices or locations."""
import hashlib
import json

RETRYABLE_HTTP = frozenset((429, 500, 502, 503, 504))
ACCESS_DENIED_HTTP = frozenset((401, 403))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def http_state(code):
    return 'SUCCESS' if code == 200 else 'RETRY' if code in RETRYABLE_HTTP else 'ERROR'
