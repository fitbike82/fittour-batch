"""Per-process project key rotation; no secrets in diagnostics."""
import os
import re
import json
import hashlib
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
        self.day = quota_day(now)
        self.state_path = Path(env['GEMINI_KEY_POOL_STATE']) if env.get('GEMINI_KEY_POOL_STATE') else None
        self.fingerprints = [hashlib.sha256(value.encode()).hexdigest() for _, value in self.keys]
        if self.state_path:
            try:
                state = json.loads(self.state_path.read_text(encoding='utf-8'))
                if state.get('version') == 1 and state.get('day') == self.day:
                    self.daily_blocked = {i for i, digest in enumerate(self.fingerprints) if digest in state.get('exhausted', [])}
                    self.blocked.update(self.daily_blocked)
            except (OSError, ValueError, TypeError, AttributeError):
                pass


    def current(self):
        for offset in range(len(self.keys)):
            index = (self.index + offset) % len(self.keys)
            if index not in self.blocked:
                self.index = index
                return self.keys[index]
        return None

    def exhaust(self, daily=False):
        self.blocked.add(self.index)
        if daily:
            self.daily_blocked.add(self.index)
            if self.state_path:
                try:
                    self.state_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.state_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps({'version': 1, 'day': self.day,
                        'exhausted': sorted(self.fingerprints[i] for i in self.daily_blocked)}), encoding='utf-8')
                    temporary.replace(self.state_path)
                except OSError:
                    print('Gemini key state unavailable; using in-process circuit only', flush=True)
        self.index = (self.index + 1) % len(self.keys)
