"""
Measure the real Pydoll success rate for allbiz.com lookups.

The Pydoll counterpart to decodo_reliability_test.py, and deliberately the
same shape so the two numbers are comparable. One successful run proves
feasibility; this is what turns "it worked" into a rate you can design around
-- exactly as the Decodo version turned "it's unreliable" into 0/10 and drove
the whole fallback design.

Run from the project root (same folder as pydoll_test.py and app/):

    python pydoll_reliability_test.py 10
    python pydoll_reliability_test.py 10 --reuse-browser
    python pydoll_reliability_test.py 10 --proxy-file webshare_proxies.txt


THE QUESTION THIS EXISTS TO ANSWER
----------------------------------
The first successful Pydoll run showed something worth more than the pass
itself: the search page hit a Turnstile challenge (cleared in ~11s), and the
detail page was then served with **no challenge at all**.

That is not luck. A browser keeps the `cf_clearance` cookie and one IP for the
whole session, so a challenge solved once covers subsequent navigations. This
is precisely the failure mode Decodo could not escape: its per-request random
IP meant the detail page had to clear the challenge from scratch every time,
which is what HANDOFF §9D was chasing when the detail page failed 0/8.

So the architectural question is how far that amortises:

    --reuse-browser OFF (default)  one browser per lookup. Expect a challenge
                                   on nearly every lookup. This is the
                                   conservative number, directly comparable to
                                   Decodo's per-lookup behaviour.

    --reuse-browser ON             one browser for all N lookups. If the
                                   clearance persists, expect a challenge on
                                   lookup 1 and none after. That would make
                                   the cost model "one solve per session"
                                   rather than "one solve per lookup" -- a
                                   large difference in both latency and
                                   detection surface.

Run BOTH. The gap between them is the value of session reuse, and it decides
how the production integration should be shaped: a browser per request, or a
long-lived pooled browser.

Related prior art in this repo: clearance_cookie_test.py was built to measure
how long a hand-solved `cf_clearance` survives. --reuse-browser measures the
same property, but with Pydoll doing the solving and no manual step.


DO NOT TOUCH THE BROWSER
------------------------
Headed mode opens a window. Solving a widget yourself corrupts the run -- let a
failure be a failure.

The "solved by" column is best-effort in headed mode. It infers a Pydoll
failure from a bypass error logged WHILE a challenge was on screen. That
windowing matters: `enable_auto_solve_cloudflare_captcha` registers on every
load event, so the bypass also runs on the ipify and detail-page loads, which
have no widget at all, and errors from those surface asynchronously -- often
during the NEXT lookup. Rows that cannot be called are labelled `unclear`, and
the headline success rate counts them, with a pydoll-only lower bound reported
separately.

For attribution you can fully trust, run --headless: with no visible window,
nobody could have solved anything by hand.


A CORRECTION TO §3 WORTH RECORDING
----------------------------------
§3 states a residential ASN is "never challenged at all, ever." A 10-lookup
run from the office IP produced a Turnstile challenge on 2 of 10 lookups (both
cleared by Pydoll in 4-8s). So residential is *mostly* unchallenged, not
always.

That matters for §9C's plan. Its appeal was that a trusted IP means "the
original code works unchanged: no vendor, no browser, no solving" -- but plain
`requests` cannot clear a challenge at all, so at a ~20% challenge rate it
would silently lose about one lookup in five. Residential IPs plus something
that CAN clear a challenge looks like the real answer, which makes Pydoll a
complement to §9C rather than a competitor to it.


COST NOTE
---------
Unlike the Decodo version, this costs nothing per request -- no vendor is
billed. What it does spend is *reputation*: N lookups from one IP in quick
succession is itself a pattern. The default input is held constant (same as
decodo_reliability_test.py, so the only variable is transport behaviour over
time), which means the same detail page gets hit N times -- the exact
per-resource suspicion §9D warns about. For a clean read on a shared IP, use
--cases to vary the business.
"""

import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter

try:
    import pydoll_test as P
except ImportError:
    sys.exit("Run this from the same folder as pydoll_test.py")

from pydoll.browser.chromium import Chrome
from pydoll.commands.network_commands import NetworkCommands


async def arm_tab(tab, args):
    """Prepare a tab: humanized captcha click, and NO HTTP CACHE.

    Disabling the cache is not hygiene, it is correctness. Every lookup here
    navigates to the SAME urls, so Chrome will happily serve them from its own
    cache on the second visit onward -- no network request, no Cloudflare
    evaluation, no challenge, and a sub-second "lookup" that measures the
    cache rather than allbiz. A --reuse-browser run without this reports 100%
    success and zero challenges no matter what the site is actually doing.
    """
    await tab.enable_auto_solve_cloudflare_captcha(
        time_to_wait_captcha=args.captcha_wait
    )
    if not args.allow_cache:
        await tab.enable_network_events()
        await tab._execute_command(NetworkCommands.set_cache_disabled(True))
        await tab._execute_command(NetworkCommands.clear_browser_cache())


# Known-good case: confirmed to return a full 42-field record when the
# transport cooperates, so any failure here is the transport's, not a
# bad-input or no-such-business case.
DEFAULT_CASE = {
    "ss": "american apparel",
    "ia": "US_FL",
}


def load_cases(path):
    """Read `name|state_code` lines, e.g.  joes hardware|US_TX

    Varying the business avoids hammering one detail page (§9D). Blank lines
    and # comments ignored.
    """
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                sys.exit(f"Bad --cases line (want 'name|US_XX'): {line!r}")
            cases.append({"ss": parts[0], "ia": parts[1]})
    if not cases:
        sys.exit(f"No cases found in {path}")
    return cases


_CB = {"n": 0}


def bust(url, args):
    """Append a unique throwaway query param.

    Belt and braces alongside Network.setCacheDisabled. Disabling the HTTP
    cache did NOT stop repeat lookups to identical URLs returning in ~0.6s with
    byte-identical bodies, so some other layer (memory cache, back/forward
    cache, or Chrome collapsing a navigation to the URL it already has) was
    still serving them. A URL that has never been requested before cannot be
    served by any of those layers, which removes the ambiguity instead of
    reasoning about Chrome internals.

    allbiz is a Django app and ignores unknown query params (§4), so this
    changes what is cached, not what is returned. Turn off with --no-cache-bust
    if you want to compare.
    """
    if not args.cache_bust:
        return url
    _CB["n"] += 1
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_cb={_CB['n']}"


async def net_count(tab):
    """Cumulative count of network requests this tab has made.

    The decisive instrument for 'did a request actually leave the machine'.
    A real two-page lookup fires many requests (document + subresources); a
    lookup served from any cache fires none. Latency alone was ambiguous --
    this is not.
    """
    try:
        return len(await tab.get_network_logs())
    except Exception:
        return None


async def one_lookup(tab, args, case, watcher, errors_before, tag="x"):
    """Search + detail through an existing tab. Returns a result dict.

    Mirrors pydoll_test.run_once's sequence, but against a tab supplied by the
    caller so the same code serves both fresh-browser and reused-browser modes.
    """
    out = {
        "business": case["ss"],
        "search_challenged": None,
        "search_cleared": None,
        "detail_challenged": None,
        "record_ok": False,
        "solved_by": None,
        "error": None,
        "search_bytes": 0,
        "detail_bytes": 0,
        "detail_url": None,
        "requests": None,
    }

    net_before = await net_count(tab)
    query = P.urlencode({"ss": case["ss"], "ia": case["ia"]})
    await tab.go_to(bust(f"{P.SEARCH_URL}?{query}", args), timeout=args.timeout)
    s = await P.settle(tab, "search", lambda h: P.count_result_links(h) > 0,
                       args.challenge_wait)
    out["search_challenged"] = s["challenged"]
    out["search_cleared"] = s["cleared"]

    # Only errors logged WHILE the challenge was on screen say anything about
    # this challenge. The bypass also runs on the ipify and detail-page loads,
    # which have no widget, and those errors surface asynchronously -- often
    # during the NEXT lookup. Counting them misattributed Pydoll solves.
    in_window = watcher.errors_between(s.get("chal_t0"), s.get("chal_t1"))
    if not s["challenged"]:
        out["solved_by"] = "n/a"
    elif s["cleared"] and in_window:
        out["solved_by"] = "unclear"
    elif s["cleared"]:
        out["solved_by"] = "pydoll"
    else:
        out["solved_by"] = "nobody"

    if not s["cleared"]:
        out["error"] = "search never cleared the challenge"
        return out

    out["search_bytes"] = len(s["html"])
    if s.get("blocked"):
        out["error"] = ("HARD BLOCKED by Cloudflare (error 1020) -- refused outright, "
                        "not challenged")
        out["blocked"] = True
        P.save(f"pydoll_rel_{tag}_search_BLOCKED.html", s["html"])
        out["requests"] = (await net_count(tab) or 0) - (net_before or 0)
        return out
    links = P.count_result_links(s["html"])
    if links == 0:
        out["error"] = "search returned zero listings"
        out["requests"] = (await net_count(tab) or 0) - (net_before or 0)
        # Save it: "unchallenged but empty" is the one failure mode that cannot
        # be diagnosed from the console line alone. It looks identical whether
        # the query genuinely has no matches or the site served a results-less
        # shell page because it did not like the client (headless, say).
        P.save(f"pydoll_rel_{tag}_search_EMPTY.html", s["html"])
        return out

    detail_url, why = P.first_detail_url(s["html"])
    if not detail_url:
        out["error"] = why
        return out

    out["detail_url"] = detail_url
    await tab.go_to(bust(detail_url, args), timeout=args.timeout)
    # Stricter than JSON-LD alone on purpose -- see P.has_full_record. Success
    # here should mean "production would have got a complete record", not
    # "something parseable arrived".
    gate = P.has_jsonld if args.loose_gate else P.has_full_record
    d = await P.settle(tab, "detail", gate, args.challenge_wait)
    out["detail_challenged"] = d["challenged"]
    out["detail_bytes"] = len(d["html"])
    if not d["cleared"]:
        out["error"] = "detail never cleared the challenge"
        return out

    data, problem = P.parse_record(d["html"])
    if problem:
        out["error"] = problem
        P.save(f"pydoll_rel_{tag}_detail_BAD.html", d["html"])
        return out

    # The readiness gate has to be ENFORCED, not just waited on. A page whose
    # JSON-LD parsed but whose contact block never arrived yields a record that
    # looks fine and is silently missing email, website, fax and every social
    # link. Counting that as a success would hide exactly the failure mode this
    # harness exists to catch.
    if not d["ready"]:
        out["error"] = ("detail page incomplete -- JSON-LD parsed but the #tc contact "
                        "block never arrived, so email/website/socials would be empty")
        out["partial"] = True
        P.save(f"pydoll_rel_{tag}_detail_PARTIAL.html", d["html"])
        return out

    out["record_ok"] = True
    out["record_name"] = data.get("name")
    out["record"] = data  # full raw JSON-LD -- callers needing more than the
    # name (the production API does) read structured fields straight out of
    # this instead of the harness having to know every field a caller wants.
    net_after = await net_count(tab)
    if net_before is not None and net_after is not None:
        out["requests"] = net_after - net_before
    return out


async def with_fresh_browser(args, cases, watcher, proxies):
    """One browser per lookup -- the conservative, Decodo-comparable number."""
    results = []
    for i in range(1, args.runs + 1):
        case = cases[(i - 1) % len(cases)]
        raw = proxies[(i - 1) % len(proxies)] if proxies else args.proxy
        print(f"\n[{i}/{args.runs}] {case['ss']!r} via {P.redact(raw)}")
        chrome_proxy, forwarder = P.split_proxy(raw)
        errors_before = len(watcher.bypass_errors)
        started = time.time()
        try:
            if forwarder is not None:
                async with forwarder as running:
                    chrome_proxy = f"socks5://127.0.0.1:{running.local_port}"
                    r = await _fresh(args, chrome_proxy, case, watcher, errors_before, f"{i:02d}")
            else:
                r = await _fresh(args, chrome_proxy, case, watcher, errors_before, f"{i:02d}")
        except Exception as exc:
            r = {"business": case["ss"], "record_ok": False, "solved_by": None,
                 "search_challenged": None, "search_cleared": None,
                 "detail_challenged": None, "error": f"{type(exc).__name__}: {exc}"}
        r["seconds"] = round(time.time() - started, 1)
        r["proxy"] = P.redact(raw)
        _line(i, r)
        results.append(r)
        if i < args.runs:
            await asyncio.sleep(args.gap)
    return results


async def _fresh(args, chrome_proxy, case, watcher, errors_before, tag="x"):
    options = P.build_options(args)
    if chrome_proxy:
        options.add_argument(f"--proxy-server={chrome_proxy}")
    async with Chrome(options=options) as browser:
        tab = await browser.start()
        await arm_tab(tab, args)
        return await one_lookup(tab, args, case, watcher, errors_before, tag)


async def with_reused_browser(args, cases, watcher, proxies):
    """One browser for all N lookups -- measures how far one solve amortises.

    The proxy is a browser launch argument, so a reused browser cannot rotate
    proxies. That is the point: this mode holds the IP fixed to see whether the
    clearance persists on it.
    """
    if proxies and len(proxies) > 1:
        print("note: --reuse-browser keeps ONE browser, so only the first proxy "
              "is used (a proxy is a launch argument).")
    raw = proxies[0] if proxies else args.proxy
    chrome_proxy, forwarder = P.split_proxy(raw)

    results = []

    async def loop(resolved_proxy):
        options = P.build_options(args)
        if resolved_proxy:
            options.add_argument(f"--proxy-server={resolved_proxy}")
        async with Chrome(options=options) as browser:
            tab = await browser.start()
            await arm_tab(tab, args)
            for i in range(1, args.runs + 1):
                case = cases[(i - 1) % len(cases)]
                print(f"\n[{i}/{args.runs}] {case['ss']!r} (same browser)")
                errors_before = len(watcher.bypass_errors)
                started = time.time()
                try:
                    r = await one_lookup(tab, args, case, watcher, errors_before,
                                         tag=f"reuse{i:02d}")
                except Exception as exc:
                    r = {"business": case["ss"], "record_ok": False, "solved_by": None,
                         "search_challenged": None, "search_cleared": None,
                         "detail_challenged": None,
                         "error": f"{type(exc).__name__}: {exc}"}
                r["seconds"] = round(time.time() - started, 1)
                r["proxy"] = P.redact(raw)
                _line(i, r)
                results.append(r)
                if i < args.runs:
                    await asyncio.sleep(args.gap)

    if forwarder is not None:
        async with forwarder as running:
            await loop(f"socks5://127.0.0.1:{running.local_port}")
    else:
        await loop(chrome_proxy)
    return results


def _line(i, r):
    status = "ok     " if r["record_ok"] else "FAIL   "
    chal = "chal" if r.get("search_challenged") else "----"
    dchal = "chal" if r.get("detail_challenged") else "----"
    tail = "" if r["record_ok"] else f"  {r.get('error')}"
    kb = f"{r.get('search_bytes', 0) // 1024}k/{r.get('detail_bytes', 0) // 1024}k"
    req = r.get("requests")
    rq = f"req:{req:<3}" if req is not None else "req:?  "
    print(f"  => {i:>3}. {status} {r['seconds']:>6.1f}s  {kb:>9} {rq} search:{chal} "
          f"detail:{dchal}  solved:{(r.get('solved_by') or '-'):<7}{tail}")


def summarize(results, args):
    runs = len(results)
    wins = [r for r in results if r["record_ok"]]
    unclear = [r for r in results if r.get("solved_by") == "unclear"]
    clean_wins = [r for r in wins if r.get("solved_by") != "unclear"]
    search_chal = [r for r in results if r.get("search_challenged")]
    detail_chal = [r for r in results if r.get("detail_challenged")]
    durations = [r["seconds"] for r in results]

    print("\n" + "=" * 72)
    print(f"Success rate:            {len(wins)}/{runs}  ({len(wins) / runs * 100:.0f}%)")
    if unclear:
        print(f"  confidently pydoll-only:{len(clean_wins)}/{runs}  "
              f"({len(clean_wins) / runs * 100:.0f}%)  (lower bound, not the real rate)")
    print(f"Search page challenged:  {len(search_chal)}/{runs}")
    print(f"Detail page challenged:  {len(detail_chal)}/{runs}")
    met = len(search_chal) + len(detail_chal)
    if met:
        survived = len([r for r in results
                        if r["record_ok"] and (r.get("search_challenged")
                                               or r.get("detail_challenged"))])
        print(f"Challenges met:          {met} (across {runs * 2} page loads)")
        print(f"  ...lookups still won:  {survived}/{met}")
    if durations:
        print(f"Latency:                 min {min(durations):.1f}s / "
              f"avg {statistics.mean(durations):.1f}s / max {max(durations):.1f}s")
    if len(durations) > 1:
        print(f"  first lookup:          {durations[0]:.1f}s")
        rest = durations[1:]
        print(f"  subsequent avg:        {statistics.mean(rest):.1f}s")

    reasons = Counter(r["error"] for r in results if not r["record_ok"])
    if reasons:
        print("\nFailures:")
        for reason, count in reasons.most_common():
            print(f"  {count:>3}x  {reason}")
    print("=" * 72)

    print("\nWHAT THIS MEANS")
    rate = len(wins) / runs if runs else 0

    # A live lookup is two page loads across the internet. Anything near a
    # second means no request left the machine -- almost always Chrome's HTTP
    # cache serving identical URLs, which silently turns this into a test of
    # the cache instead of the site. Flag it rather than report a flattering
    # number.
    no_req = [r for r in results if r.get("requests") == 0]
    fast = [r for r in results if r.get("seconds", 99) < 2.5]
    if no_req:
        print(f"  ** {len(no_req)}/{runs} lookups made ZERO network requests. Those rows are")
        print("     not measurements of anything -- the pages came from a local cache, so")
        print("     allbiz was never contacted and no challenge could be evaluated.")
        print("     Run again; if it repeats, the cache-busting param is being stripped.")
        print()
    elif fast and any(r.get("requests") in (None, 0) or r.get("requests", 0) < 10
                      for r in fast):
        print(f"  ** SUSPICIOUS LATENCY: {len(fast)}/{runs} lookups finished under 2.5s with")
        print("     few or no network requests. Something local is likely serving the")
        print("     pages, which would make those rows meaningless. Compare the req: and")
        print("     k/k columns across rows.")
        print()

    # Deterministic "loaded fine, zero listings" on every lookup is not a
    # no-such-business case -- it is the site declining to serve results.
    partials = [r for r in results if r.get("partial")]
    if partials:
        print(f"  ** {len(partials)}/{runs} detail pages arrived INCOMPLETE: JSON-LD present but")
        print("     the #tc contact block missing. Those parse cleanly while silently")
        print("     dropping email, website, fax and all social links -- 8+ fields of")
        print("     the production record. Compare the k/k byte column: a short detail")
        print("     page next to the usual size is the tell. If this happens at all,")
        print("     production must gate on the contact block too, not just JSON-LD,")
        print("     and retry when it is absent.")
        print()

    blocked = [r for r in results if r.get("blocked")]
    if blocked:
        print(f"  ** HARD BLOCKED on {len(blocked)}/{runs} lookups -- Cloudflare error 1020")
        print("     (\"Sorry, you have been blocked. You are unable to access")
        print("     allbiz.com\"). This is NOT a challenge: nothing is offered to")
        print("     solve, so a longer --captcha-wait cannot help and neither can")
        print("     Pydoll's click. The client itself was refused.")
        if args.headless:
            print()
            print("     You ran --headless. If a HEADED run of the same query on the same")
            print("     IP succeeds, the block is a response to the headless client, and")
            print("     headless is unusable for this target regardless of what any")
            print("     fingerprint documentation claims. Use xvfb-run + headed.")
        print()

    empty_all = [r for r in results
                 if r.get("error", "").startswith("search returned zero")]
    if runs > 1 and len(empty_all) == runs and not blocked:
        print("  ** EVERY lookup loaded unchallenged and returned ZERO listings, with")
        print("     no block page. Check a saved *_search_EMPTY.html <title>: a real")
        print("     allbiz page titles itself 'Search for \"<term>\" in <State>', which")
        print("     would mean this business genuinely has no listings in that state --")
        print("     a query problem, not an access problem. Try a different business.")
        print()

    if not search_chal and not detail_chal:
        print("  NOTHING was challenged in any lookup. That says nothing about Pydoll's")
        print("  captcha handling -- only that this IP isn't being challenged, which per")
        print("  §3 is what a trusted/residential ASN looks like. If that's the case, you")
        print("  don't need a browser at all: plain `requests` would work (tunnel_test.py).")
        print("  If you expected a challenge here, check the exit IP -- traffic may not")
        print("  be going through the proxy you think it is.")
    elif args.reuse_browser and runs > 1:
        met = len(search_chal) + len(detail_chal)
        first_only = len(search_chal) == 1 and search_chal[0] is results[0]
        if first_only and not detail_chal:
            print("  Only the FIRST lookup was challenged; every later one was served")
            print("  directly. One solve covered the whole session.")
        else:
            pct = met / (runs * 2) * 100
            print(f"  Challenges kept appearing on one browser: {met} across {runs * 2} page")
            print(f"  loads ({pct:.0f}%), not just on the first lookup. So a cleared challenge")
            print("  does NOT buy lasting immunity -- Cloudflare re-checks periodically")
            print("  regardless of the session. Reuse is worth having for a different")
            print("  reason: it removes the browser-launch cost from every lookup. Compare")
            print("  'subsequent avg' here against the fresh-browser run -- that gap is")
            print("  pure startup overhead, and it is usually the larger number.")
            print("  Budget for challenges at roughly the fresh-browser rate either way.")

    if unclear:
        print(f"\n  {len(unclear)} row(s) marked `unclear`: a bypass error landed while a")
        print("  challenge was on screen, so Pydoll may not be what cleared it. Do NOT")
        print("  read that as 'a human did it' -- the bypass also runs on pages with no")
        print("  widget and errors there harmlessly. The lower bound above assumes the")
        print("  worst case; the true rate is somewhere between it and the headline.")
        print("  Rerun --headless to remove the ambiguity entirely.")

    if search_chal and len(search_chal) < runs:
        pct = len(search_chal) / runs * 100
        print(f"\n  Challenged on {len(search_chal)}/{runs} lookups ({pct:.0f}%). If this ran over a")
        print("  RESIDENTIAL IP, that contradicts §3's 'never challenged at all, ever'")
        print("  and undermines §9C's premise -- its appeal was that a trusted IP lets")
        print("  the ORIGINAL code work unchanged, but plain `requests` cannot clear a")
        print("  challenge, so it would silently lose those lookups. Residential IP PLUS")
        print("  something that can solve looks like the real answer, which makes Pydoll")
        print("  a complement to §9C rather than a competitor.")

    if rate >= 0.95:
        print(f"\n  {rate * 100:.0f}% is production-viable on its own. Compare against Decodo's")
        print("  measured rate; if it holds, Pydoll can be primary and the paid")
        print("  vendors become the fallback rather than the other way round.")
    elif rate >= 0.7:
        print(f"\n  {rate * 100:.0f}% is usable but NOT alone. Keep a fallback -- the existing")
        print("  layered _fetch() in app/scrapers/allbiz_scraper.py is the right shape")
        print("  for it; add Pydoll as a tier rather than replacing the ladder.")
    elif wins:
        print(f"\n  {rate * 100:.0f}% is too low to lead with, but it WORKS sometimes, which is")
        print("  more than most options here managed. Retry logic may carry it -- §6")
        print("  concluded retrying was what did the work for Decodo too. Measure again")
        print("  with more runs before deciding.")
    else:
        print("\n  No successes. If earlier single runs passed, something changed --")
        print("  check whether the IP has since been burned by this very test (N rapid")
        print("  lookups from one IP is its own signal), and retry later or elsewhere.")

    if not args.reuse_browser and search_chal and not detail_chal and wins:
        print("\n  Note: the detail page was never challenged in any successful lookup.")
        print("  That is the browser session carrying its clearance across both page")
        print("  loads -- the single biggest structural advantage over a per-request")
        print("  proxy API, and the direct answer to §9D.")
    print()


async def main_async(args):
    watcher = P.install_bypass_watcher()
    cases = load_cases(args.cases) if args.cases else [DEFAULT_CASE]
    proxies = P.load_proxies(args.proxy_file) if args.proxy_file else None

    print("=" * 72)
    print(f"Pydoll reliability -- {args.runs} lookups")
    print(f"  mode         : {'ONE reused browser' if args.reuse_browser else 'fresh browser per lookup'}")
    print(f"  cases        : {len(cases)} ({'constant' if len(cases) == 1 else 'rotating'})")
    print(f"  routing      : {args.proxy_file or P.redact(args.proxy)}")
    print(f"  captcha wait : {args.captcha_wait}s")
    print("=" * 72)
    if not args.headless:
        print("\n  !! DO NOT interact with the browser. Let a failure be a failure.")
        print("     For attribution you can fully trust, rerun with --headless.\n")

    if args.reuse_browser:
        results = await with_reused_browser(args, cases, watcher, proxies)
    else:
        results = await with_fresh_browser(args, cases, watcher, proxies)

    summarize(results, args)
    return any(r["record_ok"] for r in results)


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Measure Pydoll's real success rate against allbiz.com.")
    p.add_argument("runs", nargs="?", type=int, default=10,
                   help="How many lookups to perform (default 10).")
    p.add_argument("--reuse-browser", action="store_true",
                   help="Use ONE browser for all lookups, to measure whether a single "
                        "cleared challenge amortises across lookups.")
    p.add_argument("--cases", default=None,
                   help="File of 'business name|US_XX' lines to rotate through, so one "
                        "detail page isn't hit N times (§9D).")
    p.add_argument("--proxy", default=None, help="Single proxy URL.")
    p.add_argument("--proxy-file", default=None,
                   help="File of proxies; rotated per lookup in fresh-browser mode.")
    p.add_argument("--headless", action="store_true", help="Run headless.")
    p.add_argument("--binary", default=None, help="Path to the Chrome/Chromium executable.")
    p.add_argument("--user-data-dir", default=None, help="Pin a specific Chrome profile.")
    p.add_argument("--no-sandbox", action="store_true", help="Add --no-sandbox.")
    p.add_argument("--captcha-wait", type=float, default=30,
                   help="Seconds Pydoll polls for the Turnstile widget (default 30).")
    p.add_argument("--challenge-wait", type=float, default=60,
                   help="Seconds to wait for a challenge to clear (default 60).")
    p.add_argument("--timeout", type=int, default=120, help="Per-navigation timeout.")
    p.add_argument("--loose-gate", action="store_true",
                   help="Count a detail page as ready on JSON-LD alone, instead of also "
                        "requiring the #tc contact block. Looser, and will count "
                        "partially-rendered pages as successes.")
    p.add_argument("--no-cache-bust", dest="cache_bust", action="store_false",
                   help="Do not append a unique _cb= param to each URL. Without busting, "
                        "repeat lookups to identical URLs can be served from a local cache "
                        "and the run measures nothing.")
    p.add_argument("--allow-cache", action="store_true",
                   help="Do NOT disable Chrome's HTTP cache. Only for debugging -- with "
                        "the cache on, repeat lookups to the same URL are served locally "
                        "and the run measures the cache instead of the site.")
    p.add_argument("--gap", type=float, default=5,
                   help="Seconds between lookups (default 5). Back-to-back bursts from "
                        "one IP are their own signal.")
    args = p.parse_args(argv)
    if args.proxy and args.proxy_file:
        p.error("use --proxy or --proxy-file, not both")
    # pydoll_test helpers read args.ss/args.ia; the per-case values are passed
    # explicitly instead, but build_options and settle expect the attributes.
    args.ss, args.ia = DEFAULT_CASE["ss"], DEFAULT_CASE["ia"]
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
