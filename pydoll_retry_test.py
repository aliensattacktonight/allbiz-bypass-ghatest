"""
Retry-across-pool reliability test: for each lookup, try up to --max-attempts
DIFFERENT proxies from the pool (skipping any currently in cooldown) until one
clears, instead of accepting a single proxy's failure as final.

Built on top of pydoll_test.py (page settling/classification) and
pydoll_reliability_test.py (one_lookup) -- this only adds the retry-with-a-
different-proxy loop and the persistent per-proxy health tracking in
proxy_pool.py. See proxy_pool.py's module docstring for why cooldown/backoff
exists: a proxy hammered repeatedly in one sitting is what burned the single-
proxy positive control from 2/2 clean passes to 0/16 -- the fix is resting a
proxy that just failed, not discarding it.

USAGE
-----
    python pydoll_retry_test.py --proxy-file webshare_proxies.txt --cases cases.txt \
        --state-file proxy_health.json --max-attempts 4

Run this repeatedly (e.g. across separate CI invocations, with
proxy_health.json restored/saved via actions/cache) to see recently-failed
proxies get skipped in later runs instead of being hit again immediately.
"""

import argparse
import asyncio
import sys
import time
from collections import Counter

try:
    import pydoll_test as P
except ImportError:
    sys.exit("Run this from the same folder as pydoll_test.py")

try:
    import pydoll_reliability_test as R
except ImportError:
    sys.exit("Run this from the same folder as pydoll_reliability_test.py")

from pydoll.browser.chromium import Chrome
from proxy_pool import ProxyPool


async def attempt_one(args, proxy_url, case, watcher, tag):
    """One search+detail attempt through a specific proxy. Fresh browser."""
    chrome_proxy, forwarder = P.split_proxy(proxy_url)
    options = P.build_options(args)

    async def run(resolved_proxy):
        if resolved_proxy:
            options.add_argument(f"--proxy-server={resolved_proxy}")
        async with Chrome(options=options) as browser:
            tab = await browser.start()
            await R.arm_tab(tab, args)
            errors_before = len(watcher.bypass_errors)
            return await R.one_lookup(tab, args, case, watcher, errors_before, tag)

    if forwarder is not None:
        async with forwarder as running:
            return await run(f"socks5://127.0.0.1:{running.local_port}")
    return await run(chrome_proxy)


async def lookup_with_retry(args, pool, case, watcher, case_idx):
    """Try up to --max-attempts different proxies for this one lookup."""
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
            result = await attempt_one(args, proxy_url, case, watcher, f"c{case_idx:02d}a{attempt}")
        except Exception as exc:
            result = {"record_ok": False, "error": f"{type(exc).__name__}: {exc}",
                      "search_challenged": None, "detail_challenged": None}
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

    print("=" * 78)
    print(f"Retry-across-pool test -- {args.lookups} lookups, up to {args.max_attempts} "
          f"proxy attempts each")
    print(f"  pool size    : {len(proxies)}")
    print(f"  state file   : {args.state_file or '(none -- no persistence between runs)'}")
    print("=" * 78)
    print("\nPool state at start:")
    print(pool.summary())
    print()

    results = []
    for i in range(1, args.lookups + 1):
        case = cases[(i - 1) % len(cases)]
        print(f"\n[{i}/{args.lookups}] {case['ss']!r} / {case['ia']}")
        r = await lookup_with_retry(args, pool, case, watcher, i)
        results.append(r)
        if i < args.lookups:
            await asyncio.sleep(args.gap)

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
    p = argparse.ArgumentParser(description="Retry-across-proxy-pool reliability test.")
    p.add_argument("lookups", nargs="?", type=int, default=8, help="How many lookups (default 8).")
    p.add_argument("--max-attempts", type=int, default=4,
                   help="Max different proxies to try per lookup before giving up (default 4).")
    p.add_argument("--proxy-file", required=True, help="File of proxy URLs (required).")
    p.add_argument("--state-file", default="proxy_health.json",
                   help="Where per-proxy health persists across runs (default proxy_health.json).")
    p.add_argument("--cases", default=None, help="File of 'name|US_XX' lines to rotate through.")
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
