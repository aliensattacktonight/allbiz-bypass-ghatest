"""
Full-system reliability test: retry across the proxy pool + persistent
per-proxy health/cooldown + LONG-LIVED browser sessions per proxy, all
together.

Three pillars, each addressing a distinct failure mode measured earlier:

1. RETRY ACROSS THE POOL (proxy_pool.py + lookup_with_retry below). A single
   proxy attempt clears only ~20-30% of the time -- retrying a failed lookup
   against a DIFFERENT proxy compounds those odds instead of accepting one
   proxy's bad luck as final. Measured effect: 20-30% -> 60-70%.

2. PERSISTENT HEALTH / COOLDOWN (proxy_pool.py). A proxy hammered repeatedly
   in one sitting is what burned the single-proxy positive control from 2/2
   clean passes to 0/16. Exponential backoff rests a proxy that just failed
   instead of asking it again immediately, and resets to full trust on any
   success.

3. LONG-LIVED BROWSER SESSIONS (BrowserPool below, new). Directly measured:
   Cloudflare's actual clearance cookie on this target is `cg_pass`
   (httpOnly, ~5h lifetime) -- not the generic `cf_clearance` name. It exists
   live in the browser right after a clean solve, which is WHY a lookup's
   detail page almost never gets separately challenged after its search page
   clears. That win was already happening WITHIN one lookup; it was being
   thrown away BETWEEN lookups because attempt_one() launched a fresh Chrome
   process (and therefore a fresh, cookie-less session) every single time.
   Keeping one Chrome instance alive per proxy across MANY lookups lets
   cg_pass -- and the geo-matching applied once at launch -- carry forward
   for as long as that proxy stays in rotation, instead of being discarded
   after one use. This also removes the ~7-10s browser-launch cost from
   every attempt after the first for a given proxy.

USAGE
-----
    python pydoll_retry_test.py 10 --proxy-file webshare_proxies.txt --cases cases.txt \
        --state-file proxy_health.json --max-attempts 4 --profile-dir chrome_profiles

Run repeatedly (proxy_health.json persisted across invocations, e.g. via a
git commit-back step in CI) to see recently-failed proxies get skipped in
later runs instead of being hit again immediately.
"""

import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
from collections import Counter


def kill_process_tree(pid):
    """Kill a process AND all its descendants.

    pydoll's own BrowserProcessManager.stop_process() only calls
    terminate()/kill() on the single main Chrome process object it holds a
    handle to. On Windows that does NOT cascade to the renderer/GPU/utility/
    crashpad-handler processes Chrome's own multi-process architecture
    spawns as separate OS processes -- confirmed directly: a run using only
    that path went to 147 live chrome.exe processes and started crashing
    with "Chrome is unresponsive" dialogs. This is the actual backstop:
    called in addition to (not instead of) browser.stop(), so the full tree
    dies regardless of whether pydoll's own graceful shutdown completed.
    """
    if not pid:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        else:
            # Linux CI runners: kill children first (pkill -P), then the
            # parent itself, in case it's not already gone.
            subprocess.run(["pkill", "-9", "-P", str(pid)], capture_output=True, timeout=10)
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=10)
    except Exception:
        pass

try:
    import pydoll_test as P
except ImportError:
    sys.exit("Run this from the same folder as pydoll_test.py")

try:
    import pydoll_reliability_test as R
except ImportError:
    sys.exit("Run this from the same folder as pydoll_reliability_test.py")

from pydoll.browser.chromium import Chrome
from pydoll.commands.emulation_commands import EmulationCommands
from proxy_pool import ProxyPool
from proxy_geo import geo_for


def profile_dir_for(proxy_url, base_dir):
    """Stable, filesystem-safe directory per proxy. Secondary to the
    BrowserPool's in-memory session reuse (that's the primary mechanism now
    -- see module docstring #3) but kept as a fallback: if a proxy's live
    browser ever needs to be relaunched mid-run (crash, connection drop),
    whatever DID make it to disk from the prior session is still there."""
    from urllib.parse import urlparse
    p = urlparse(proxy_url)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"{p.hostname}_{p.port}")
    path = os.path.join(base_dir, safe)
    os.makedirs(path, exist_ok=True)
    return path


async def apply_geo(tab, proxy_url):
    """Match the browser's JS-visible timezone/locale/geolocation to the
    proxy's actual country -- see proxy_geo.py's docstring."""
    geo = geo_for(proxy_url)
    await tab._execute_command(EmulationCommands.set_timezone_override(geo["timezone"]))
    await tab._execute_command(EmulationCommands.set_locale_override(geo["locale"].replace("-", "_")))
    await tab._execute_command(
        EmulationCommands.set_geolocation_override(latitude=geo["lat"], longitude=geo["lon"], accuracy=100)
    )
    return geo


class BrowserPool:
    """One long-lived Chrome instance per proxy, launched on first use and
    kept alive for the rest of the run so its session (cookies, incl.
    cg_pass; geo overrides applied once) carries across every subsequent
    lookup that picks the same proxy -- see module docstring #3.
    """

    def __init__(self, args, max_concurrent=4):
        self.args = args
        # Bounds how many live Chrome instances this pool holds open at
        # once. Without this, a rough patch where lookup after lookup
        # fails and retries onto a NEW proxy each time accumulates one
        # live browser per distinct proxy tried -- observed directly: a
        # single local run ballooned to 128 chrome.exe processes and every
        # subsequent launch started timing out from resource starvation,
        # which then LOOKED like a Cloudflare failure but was actually us
        # running out of memory/handles. LRU eviction keeps at most
        # `max_concurrent` sessions alive; the rest get closed and
        # relaunched fresh if picked again later.
        self.max_concurrent = max_concurrent
        self._entries = {}  # proxy_url -> {"browser": Chrome, "tab": Tab, "last_used": float}

    def _pid_of(self, browser):
        """The real OS PID behind a Chrome object. Lives on the process
        manager, not the Browser instance itself -- see
        browser_process_manager.py: BrowserProcessManager._process.pid."""
        mgr = getattr(browser, "_browser_process_manager", None)
        proc = getattr(mgr, "_process", None) if mgr else None
        return getattr(proc, "pid", None)

    async def _kill_orphan(self, browser):
        """Hard-kill the FULL process tree behind a Chrome object -- not
        just the main process (see kill_process_tree's docstring for why
        that alone isn't enough). Covers both a browser that failed
        partway through start() (never reached self._entries, so
        discard() has nothing to find) and a normal close where pydoll's
        own graceful shutdown didn't fully clean up its children."""
        kill_process_tree(self._pid_of(browser))

    async def _evict_lru(self):
        """Close the least-recently-used session to stay within
        max_concurrent. Only called right before opening a new one."""
        if len(self._entries) < self.max_concurrent:
            return
        lru_url = min(self._entries, key=lambda u: self._entries[u]["last_used"])
        print(f"    [pool] at capacity ({self.max_concurrent}) -- closing LRU session "
              f"{P.redact(lru_url)} to make room")
        await self.discard(lru_url)

    async def get_tab(self, proxy_url):
        entry = self._entries.get(proxy_url)
        if entry is not None:
            entry["last_used"] = time.time()
            return entry["tab"]

        chrome_proxy, forwarder = P.split_proxy(proxy_url)
        if forwarder is not None:
            # None of the current pool's proxies are authenticated SOCKS5,
            # so this path is untested here -- fail loud rather than
            # silently mismanage the forwarder's lifecycle across reuse.
            raise NotImplementedError(
                "BrowserPool doesn't yet support authenticated SOCKS5 proxies "
                "(needs a kept-alive SOCKS5Forwarder per proxy, not just per attempt)"
            )

        await self._evict_lru()

        options = P.build_options(self.args)
        if self.args.profile_dir:
            options.add_argument(f"--user-data-dir={profile_dir_for(proxy_url, self.args.profile_dir)}")
        geo = geo_for(proxy_url)
        options.add_argument(f"--lang={geo['locale']}")
        if chrome_proxy:
            options.add_argument(f"--proxy-server={chrome_proxy}")

        browser = Chrome(options=options)
        try:
            tab = await browser.start()
            applied = await apply_geo(tab, proxy_url)
            await tab.enable_auto_solve_cloudflare_captcha(time_to_wait_captcha=self.args.captcha_wait)
        except Exception:
            await self._kill_orphan(browser)
            raise
        print(f"    [pool] launched persistent session for {P.redact(proxy_url)} "
              f"({applied['city']}, {applied['country']}) -- {len(self._entries) + 1}/{self.max_concurrent} slots used")
        self._entries[proxy_url] = {"browser": browser, "tab": tab, "last_used": time.time()}
        return tab

    async def discard(self, proxy_url):
        """Drop a proxy's session (e.g. after a crash, or LRU eviction) so
        the next get_tab() call for it launches fresh instead of reusing a
        dead browser.

        Tree-kill runs UNCONDITIONALLY, after the graceful attempt --
        not just as an except-branch fallback. browser.stop() can return
        successfully while still leaving child renderer/GPU/utility
        processes behind (it only confirms the MAIN process exited); the
        only way to guarantee nothing survives is to always sweep the
        tree by PID afterward.
        """
        entry = self._entries.pop(proxy_url, None)
        if entry is None:
            return
        try:
            await entry["browser"].stop()
        except Exception:
            pass
        await self._kill_orphan(entry["browser"])

    async def close_all(self):
        for proxy_url in list(self._entries.keys()):
            await self.discard(proxy_url)


async def lookup_with_retry(args, pool, browser_pool, case, watcher, case_idx):
    """Try up to --max-attempts different proxies for this one lookup,
    reusing each proxy's long-lived browser session rather than launching
    a fresh one per attempt."""
    tried = set()
    last_result = None
    for attempt in range(1, args.max_attempts + 1):
        proxy_url = pool.pick(exclude=tried)
        if proxy_url is None:
            print(f"    (pool exhausted -- only {len(tried)} proxies exist)")
            break
        tried.add(proxy_url)
        redacted = P.redact(proxy_url)
        print(f"  attempt {attempt}/{args.max_attempts} via {redacted}")
        started = time.time()
        try:
            tab = await browser_pool.get_tab(proxy_url)
            errors_before = len(watcher.bypass_errors)
            result = await R.one_lookup(
                tab, args, case, watcher, errors_before, f"c{case_idx:02d}a{attempt}"
            )
        except Exception as exc:
            result = {"record_ok": False, "error": f"{type(exc).__name__}: {exc}",
                      "search_challenged": None, "detail_challenged": None}
            # The session may be in a bad state (crashed proxy, dead
            # process) -- drop it so a future pick relaunches clean rather
            # than repeatedly erroring against a broken browser.
            await browser_pool.discard(proxy_url)
        elapsed = time.time() - started
        result["proxy"] = redacted
        result["attempt"] = attempt
        result["seconds"] = round(elapsed, 1)

        if result.get("record_ok"):
            pool.mark_success(proxy_url)
            print(f"    -> OK in {elapsed:.1f}s (attempt {attempt}) : {result.get('record_name')}")
            return result
        else:
            pool.mark_failure(proxy_url)
            print(f"    -> FAIL in {elapsed:.1f}s : {result.get('error')}")
            last_result = result
        if attempt < args.max_attempts:
            await asyncio.sleep(args.retry_gap)
    return last_result or {"record_ok": False, "error": "no proxies available", "attempt": 0}


async def main_async(args):
    watcher = P.install_bypass_watcher()
    cases = R.load_cases(args.cases) if args.cases else [R.DEFAULT_CASE]
    proxies = P.load_proxies(args.proxy_file)
    pool = ProxyPool(proxies, state_path=args.state_file)
    browser_pool = BrowserPool(args, max_concurrent=args.max_concurrent_browsers)

    print("=" * 78)
    print(f"Full-system test -- {args.lookups} lookups, up to {args.max_attempts} "
          f"proxy attempts each")
    print(f"  pool size       : {len(proxies)}")
    print(f"  state file      : {args.state_file or '(none -- no persistence between runs)'}")
    print(f"  profile dir     : {args.profile_dir or '(none)'}")
    print(f"  browser sessions: persistent per proxy (this run)")
    print("=" * 78)
    print("\nPool state at start:")
    print(pool.summary())
    print()

    results = []
    try:
        for i in range(1, args.lookups + 1):
            case = cases[(args.case_offset + i - 1) % len(cases)]
            print(f"\n[{i}/{args.lookups}] {case['ss']!r} / {case['ia']}")
            r = await lookup_with_retry(args, pool, browser_pool, case, watcher, i)
            results.append(r)
            if i < args.lookups:
                await asyncio.sleep(args.gap)
    finally:
        await browser_pool.close_all()

    print("\n" + "=" * 78)
    print("RESULTS")
    print("=" * 78)
    wins = [r for r in results if r.get("record_ok")]
    print(f"Success rate (with retry): {len(wins)}/{len(results)} "
          f"({len(wins) / len(results) * 100:.0f}%)")
    attempts_used = [r.get("attempt", 0) for r in wins]
    if attempts_used:
        first_try = len([a for a in attempts_used if a == 1])
        print(f"  solved on attempt 1:      {first_try}/{len(wins)}")
        print(f"  needed a retry:           {len(wins) - first_try}/{len(wins)}")
    durations = [r["seconds"] for r in results]
    if durations:
        import statistics
        print(f"  latency: min {min(durations):.1f}s / avg {statistics.mean(durations):.1f}s "
              f"/ max {max(durations):.1f}s")
    failures = [r for r in results if not r.get("record_ok")]
    if failures:
        reasons = Counter(r.get("error") for r in failures)
        print("\nStill-failed lookups (exhausted all attempts):")
        for reason, count in reasons.most_common():
            print(f"  {count:>2}x  {reason}")

    print("\nPool state at end:")
    print(pool.summary())
    print("=" * 78)
    return len(wins) == len(results)


def parse_args(argv):
    p = argparse.ArgumentParser(description="Full-system (retry + pool + persistent sessions) test.")
    p.add_argument("lookups", nargs="?", type=int, default=8, help="How many lookups (default 8).")
    p.add_argument("--max-attempts", type=int, default=4,
                   help="Max different proxies to try per lookup before giving up (default 4).")
    p.add_argument("--proxy-file", required=True, help="File of proxy URLs (required).")
    p.add_argument("--state-file", default="proxy_health.json",
                   help="Where per-proxy health persists across runs (default proxy_health.json).")
    p.add_argument("--max-concurrent-browsers", type=int, default=4,
                   help="Cap on simultaneously-live persistent browser sessions (LRU-evicted "
                        "beyond this). Prevents a bad patch from ballooning into dozens of live "
                        "Chrome processes -- default 4.")
    p.add_argument("--profile-dir", default=None,
                   help="Base directory for per-proxy on-disk Chrome profiles (fallback if a "
                        "session is relaunched mid-run). Omit for temp profiles.")
    p.add_argument("--cases", default=None, help="File of 'name|US_XX' lines to rotate through.")
    p.add_argument("--case-offset", type=int, default=0,
                   help="Start the case rotation at this index instead of 0. Lets a batched "
                        "sequence of separate process invocations (see the long-running-process "
                        "degradation this works around) continue through the case list instead "
                        "of every batch restarting at case 0.")
    p.add_argument("--no-sandbox", action="store_true")
    p.add_argument("--binary", default=None)
    p.add_argument("--user-data-dir", default=None)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--captcha-wait", type=float, default=45)
    p.add_argument("--challenge-wait", type=float, default=60)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--loose-gate", action="store_true")
    p.add_argument("--cache-bust", dest="cache_bust", action="store_true", default=True)
    p.add_argument("--allow-cache", action="store_true")
    p.add_argument("--gap", type=float, default=6, help="Seconds between different lookups.")
    p.add_argument("--retry-gap", type=float, default=8,
                   help="Seconds between retry attempts within the same lookup.")
    args = p.parse_args(argv)
    args.ss, args.ia = "", ""  # unused attrs P.build_options doesn't actually read
    return args


def main():
    args = parse_args(sys.argv[1:])
    try:
        ok = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
