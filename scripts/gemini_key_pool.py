"""Per-process project key rotation; no secrets in diagnostics."""
import os
import re
import json
import hashlib
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

KEY_NAMES = tuple(f"GEMINI_API_KEY_{i:02}" for i in range(1, 4))


def discover_keys(environ=None):
    env = os.environ if environ is None else environ
    keys, seen = [], set()
    for name in KEY_NAMES:
        value = env.get(name, "").strip()
        if value and value not in seen:
            keys.append(("Key-" + name[-2:], value))
            seen.add(value)
    if not keys and env.get("GEMINI_API_KEY", "").strip():
        keys.append(("Key-legacy", env["GEMINI_API_KEY"].strip()))
    return keys


def batch_slot(now=None):
    # Twenty hourly runs: UTC 00..16,21..23. Anchor to Pacific midnight,
    # the provider's actual daily reset (KST 16:00 or 17:00 with DST).
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    reset = datetime(local.year, local.month, local.day,
                     tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(timezone.utc)
    slots = [reset + timedelta(hours=i) for i in range(24)]
    slots = [t for t in slots if t.hour not in (17,18,19,20)]
    return max(1, sum(t <= now for t in slots))


def quota_day(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()


def mask_keys(text, environ=None):
    text = str(text)
    env = os.environ if environ is None else environ
    for name in (*KEY_NAMES, "GEMINI_API_KEY"):
        key = env.get(name, "").strip()
        if key:
            text = text.replace(key, "AIzaSy****" if key.startswith("AIza") else "****")
    return re.sub(r"AIza[0-9A-Za-z_-]{20,}", "AIzaSy****", text)


class KeyPool:
    def __init__(self, environ=None, now=None):
        env = os.environ if environ is None else environ
        self.keys = discover_keys(env)
        if not self.keys:
            raise ValueError("Missing Gemini key configuration")
        ordinal = int(env.get("GEMINI_BATCH_SLOT") or batch_slot(now))
        if not 1 <= ordinal <= 20:
            raise ValueError("GEMINI_BATCH_SLOT must be 1..20")
        preferred = "Key-%02d" % (1 if ordinal <= 7 else 2 if ordinal <= 14 else 3)
        self.index = next((i for i, (label, _) in enumerate(self.keys) if label == preferred), (ordinal-1)%len(self.keys))
        self.blocked = set()
        self.daily_blocked = set()
        self.blocked_at = {}
        self.now = (lambda: now) if now is not None else lambda: datetime.now(timezone.utc)
        self.ttl_seconds = float(env.get('GEMINI_QUOTA_BLOCK_TTL_SECONDS', '14400'))
        if not math.isfinite(self.ttl_seconds) or not 60 <= self.ttl_seconds <= 86400:
            raise ValueError('GEMINI_QUOTA_BLOCK_TTL_SECONDS must be 60..86400')
        self.day = quota_day(now)
        self.state_path = Path(env['GEMINI_KEY_POOL_STATE']) if env.get('GEMINI_KEY_POOL_STATE') else None
        self.fingerprints = [hashlib.sha256(value.encode()).hexdigest() for _, value in self.keys]
        if self.state_path:
            try:
                state = json.loads(self.state_path.read_text(encoding='utf-8'))
                if state.get('version') in (1, 2) and state.get('day') == self.day:
                    timestamps = state.get('blocked_at', {})
                    # v1 lacked timestamps: use cache file age, never an infinite lock.
                    legacy_time = self.state_path.stat().st_mtime
                    self.blocked_at = {i: float(timestamps.get(digest, legacy_time))
                                       for i, digest in enumerate(self.fingerprints)
                                       if digest in state.get('exhausted', [])}
                    self.daily_blocked = set(self.blocked_at)
                    self.blocked.update(self.daily_blocked)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self._refresh()

    def _save(self):
        if self.state_path:
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.state_path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'version': 2, 'day': self.day,
                    'exhausted': sorted(self.fingerprints[i] for i in self.daily_blocked),
                    'blocked_at': {self.fingerprints[i]: self.blocked_at[i] for i in self.daily_blocked}}), encoding='utf-8')
                temporary.replace(self.state_path)
            except OSError:
                print('Gemini key state unavailable; using in-process circuit only', flush=True)

    def _refresh(self):
        now = self.now()
        if quota_day(now) != self.day:
            self.day = quota_day(now)
            self.blocked.clear()
            self.daily_blocked.clear()
            self.blocked_at.clear()
            self._save()
            return
        expired = {i for i, timestamp in self.blocked_at.items()
                   if not math.isfinite(timestamp) or timestamp > now.timestamp()
                   or now.timestamp() - timestamp >= self.ttl_seconds}
        if expired:
            self.blocked.difference_update(expired)
            self.daily_blocked.difference_update(expired)
            for i in expired:
                self.blocked_at.pop(i, None)
            self._save()

    def current(self):
        self._refresh()
        for offset in range(len(self.keys)):
            index = (self.index + offset) % len(self.keys)
            if index not in self.blocked:
                self.index = index
                return self.keys[index]
        return None

    def exhaust(self, daily=False):
        self._refresh()
        self.blocked.add(self.index)
        if daily:
            self.daily_blocked.add(self.index)
            self.blocked_at[self.index] = self.now().timestamp()
            self._save()
        self.index = (self.index + 1) % len(self.keys)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Reset only the local Gemini quota circuit state; no API calls.')
    parser.add_argument('--reset-state', type=Path, required=True)
    args = parser.parse_args()
    # Persist an empty current-day state so Actions saves a replacement cache.
    args.reset_state.parent.mkdir(parents=True, exist_ok=True)
    args.reset_state.write_text(json.dumps({'version': 2, 'day': quota_day(),
        'exhausted': [], 'blocked_at': {}}), encoding='utf-8')
    print('Gemini quota circuit reset; provider quota itself is unchanged.')
