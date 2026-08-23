"""
Stateful proxy pool with per-proxy health tracking, cooldown/backoff, and
load-spreading selection.

WHY THIS EXISTS
----------------
The reliability suite showed two separate things a naive round-robin
(pydoll_reliability_test.py's `proxies[(i-1) % len(proxies)]`) does not
address:

1. Per-lookup retry. A single proxy attempt succeeds only ~20-30% of the
   time (10-proxy pool run: 2/10 clean, 1/10 cleared-but-empty, rest never
   cleared). One-shot-per-proxy leaves most of that on the table -- retrying
   a FAILED lookup against a DIFFERENT proxy compounds the odds.

2. Burn prevention. The single-proxy comprehensive suite drove one IP from
   "works twice" to "0/16, never clears" by hitting it 16 times in one
   sitting. That is a rate/volume signal on that specific IP, not a global
   verdict on it -- the same proxy worked again minutes later from a
   DIFFERENT (lower-volume) context. So the fix is not "throw away a proxy
   that failed" -- it is "stop asking that specific proxy anything for a
   while and let its reputation reset", which is exactly what web services
   do with rate limits.

STRATEGY
--------
- On failure: exponential-backoff cooldown (15min, 30min, 1h, 2h, capped at
  4h), keyed per proxy. A proxy that just failed is not tried again until
  its cooldown expires, however many other lookups happen in the meantime.
- On success: cooldown and failure streak both reset immediately -- a proxy
  that works is trusted again right away, no artificial throttling of a
  proxy currently in good standing.
- Selection prefers, in order: not currently cooling down, fewest recent
  consecutive failures, then longest idle time (spreads load across the
  whole pool instead of hammering whichever proxy sorts first). If EVERY
  proxy is cooling down, picks the one closest to expiry rather than
  refusing outright -- a lookup happening now needs an answer now.
- State persists to a JSON file across process runs (and, on CI, across
  separate workflow invocations via actions/cache) so a proxy burned in one
  run stays resting in the next, instead of every run starting from a blank
  slate and re-burning it immediately.
"""

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlparse

COOLDOWN_BASE_SECONDS = 15 * 60
COOLDOWN_MAX_SECONDS = 4 * 60 * 60
BACKOFF_MULTIPLIER = 2


@dataclass
class ProxyState:
    url: str
    consecutive_failures: int = 0
    total_success: int = 0
    total_failure: int = 0
    last_used_ts: float = 0.0
    last_result_ts: float = 0.0
    cooldown_until_ts: float = 0.0

    def is_cooling_down(self, now):
        return now < self.cooldown_until_ts

    def redacted(self):
        p = urlparse(self.url)
        return f"{p.hostname}:{p.port}"


class ProxyPool:
    def __init__(self, proxies, state_path=None):
        self.state_path = Path(state_path) if state_path else None
        self.states = {p: ProxyState(url=p) for p in proxies}
        if self.state_path and self.state_path.exists():
            self._load()

    def _load(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return  # corrupt or missing state file -> start fresh, don't crash
        for url, saved in data.items():
            if url in self.states:
                st = self.states[url]
                for k, v in saved.items():
                    if hasattr(st, k):
                        setattr(st, k, v)

    def save(self):
        if not self.state_path:
            return
        data = {url: asdict(st) for url, st in self.states.items()}
        self.state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def pick(self, exclude=None):
        """Best available proxy not already tried this lookup (see `exclude`)."""
        exclude = exclude or set()
        now = time.time()
        candidates = [s for url, s in self.states.items() if url not in exclude]
        if not candidates:
            return None
        available = [s for s in candidates if not s.is_cooling_down(now)]
        pool = available if available else candidates
        if available:
            pool.sort(key=lambda s: (s.consecutive_failures, s.last_used_ts))
        else:
            # Everyone's resting -- pick whoever's closest to being ready.
            pool.sort(key=lambda s: s.cooldown_until_ts)
        chosen = pool[0]
        chosen.last_used_ts = now
        return chosen.url

    def mark_success(self, url):
        st = self.states[url]
        st.consecutive_failures = 0
        st.total_success += 1
        st.cooldown_until_ts = 0.0
        st.last_result_ts = time.time()
        self.save()

    def mark_failure(self, url):
        st = self.states[url]
        st.consecutive_failures += 1
        st.total_failure += 1
        st.last_result_ts = time.time()
        backoff = min(
            COOLDOWN_BASE_SECONDS * (BACKOFF_MULTIPLIER ** (st.consecutive_failures - 1)),
            COOLDOWN_MAX_SECONDS,
        )
        st.cooldown_until_ts = time.time() + backoff
        self.save()

    def summary(self):
        now = time.time()
        lines = []
        for url, st in sorted(self.states.items(), key=lambda kv: -kv[1].total_success):
            status = "COOLING" if st.is_cooling_down(now) else "ready  "
            remaining = f" ({(st.cooldown_until_ts - now) / 60:.0f}m left)" if status == "COOLING" else ""
            lines.append(
                f"  {st.redacted():<24} succ={st.total_success:<3} fail={st.total_failure:<3} "
                f"streak={st.consecutive_failures:<2} {status}{remaining}"
            )
        return "\n".join(lines)
