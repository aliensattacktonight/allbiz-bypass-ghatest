"""
One business, one state, one JSON result file. This is the production path --
pydoll_retry_test.py's batch/report machinery is for measuring the system,
this is for USING it. Same ProxyPool, same BrowserPool, same lookup_with_retry
retry loop; the only difference is there's exactly one case and the outcome
is written to disk as JSON instead of printed as a report.

Deliberately does NOT talk HTTP to anything. The calling GitHub Actions
workflow is responsible for POSTing result.json to the FastAPI callback --
keeping that here would mean a network failure inside this script silently
swallows a lookup that actually succeeded. Separate steps, separate failure
modes, both visible in the Actions log.

USAGE
-----
    python single_lookup.py "Rock's Moving Company" US_FL \
        --proxy-file webshare_proxies.txt --state-file proxy_health.json \
        --out result.json
"""

import argparse
import asyncio
import html
import json
import sys
import time

try:
    import pydoll_test as P
except ImportError:
    sys.exit("Run this from the same folder as pydoll_test.py")

try:
    import pydoll_reliability_test as R
except ImportError:
    sys.exit("Run this from the same folder as pydoll_reliability_test.py")

from proxy_pool import ProxyPool
from pydoll_retry_test import BrowserPool, lookup_with_retry


def normalize_record(raw):
    """Map the JSON-LD's schema.org shape onto the field names the rest of
    the pipeline (SBFE / the old Worker) already expects, decoding HTML
    entities along the way (AllBiz's markup ships names like
    "Rock&#x27;s Moving Company" as literal text)."""
    if not raw:
        return None
    addr = raw.get("address") or {}
    employee = raw.get("employee") or {}

    def clean(v):
        return html.unescape(v) if isinstance(v, str) else v

    return {
        "businessName": clean(raw.get("name")),
        "businessContactName": clean(employee.get("name")),
        "businessContactJobTitle": clean(employee.get("jobTitle")),
        "streetAddress": clean(addr.get("streetAddress")),
        "cityName": clean(addr.get("addressLocality")),
        "stateName": clean(addr.get("addressRegion")),
        "country": clean(addr.get("addressCountry")),
        "postalCode": clean(addr.get("postalCode")),
        "phone": clean(raw.get("telephone")),
    }


async def run(args):
    watcher = P.install_bypass_watcher()
    proxies = P.load_proxies(args.proxy_file)
    pool = ProxyPool(proxies, state_path=args.state_file)
    browser_pool = BrowserPool(args, max_concurrent=args.max_concurrent_browsers)

    case = {"ss": args.business_name, "ia": args.state}
    started = time.time()
    try:
        result = await lookup_with_retry(args, pool, browser_pool, case, watcher, case_idx=1)
    finally:
        await browser_pool.close_all()
    elapsed = round(time.time() - started, 1)

    if result.get("record_ok"):
        payload = {
            "request_id": args.request_id,
            "status": "ok",
            "business_query": args.business_name,
            "state_query": args.state,
            "record": normalize_record(result.get("record")),
            "raw_record": result.get("record"),
            "proxy_used": result.get("proxy"),
            "attempts": result.get("attempt"),
            "seconds": elapsed,
        }
    else:
        payload = {
            "request_id": args.request_id,
            "status": "not_found" if result.get("error") == "search returned zero listings" else "error",
            "business_query": args.business_name,
            "state_query": args.state,
            "error": result.get("error"),
            "proxy_used": result.get("proxy"),
            "attempts": result.get("attempt"),
            "seconds": elapsed,
        }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {args.out}: status={payload['status']}")
    return payload["status"] == "ok"


def parse_args(argv):
    p = argparse.ArgumentParser(description="One business, one state, one JSON result.")
    p.add_argument("business_name")
    p.add_argument("state", help="e.g. US_FL")
    p.add_argument("--request-id", default="", help="Opaque id echoed back in the output JSON "
                   "so the caller can match this result to its own pending request.")
    p.add_argument("--out", default="result.json")
    p.add_argument("--max-attempts", type=int, default=4)
    p.add_argument("--proxy-file", required=True)
    p.add_argument("--state-file", default="proxy_health.json")
    p.add_argument("--max-concurrent-browsers", type=int, default=1,
                   help="Only one lookup happens per invocation, so there is never more than "
                        "one live session to hold open (default 1, vs. 4 in the batch harness).")
    p.add_argument("--profile-dir", default=None)
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
    p.add_argument("--retry-gap", type=float, default=8)
    args = p.parse_args(argv)
    args.ss, args.ia = "", ""  # unused attrs P.build_options doesn't actually read
    return args


def main():
    args = parse_args(sys.argv[1:])
    try:
        ok = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        # Still write a result file on an unexpected crash -- the calling
        # workflow's callback step runs unconditionally and needs something
        # to send, or the FastAPI side waits out its full timeout for nothing.
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({
                "request_id": args.request_id,
                "status": "error",
                "business_query": args.business_name,
                "state_query": args.state,
                "error": f"{type(exc).__name__}: {exc}",
            }, f, indent=2)
        raise
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
