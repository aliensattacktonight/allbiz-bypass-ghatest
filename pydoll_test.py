"""
Test whether Pydoll can fetch allbiz.com where Playwright+stealth could not.

WHY THIS IS A DIFFERENT BET (and not just "another headless browser")
---------------------------------------------------------------------
The single strongest result in HANDOFF.md is the critical experiment in §5:
same flagged proxy IP, same URL, same moment --

    Playwright-launched Chromium -> Turnstile widget CRASHED
                                    (TurnstileError 300030/300031),
                                    never became solvable
    Normal Chrome (--user-data-dir) -> widget rendered, solved by hand,
                                    landed on real allbiz data

Conclusion recorded there: the AUTOMATION FINGERPRINT, not the IP, is what
made the widget unsolvable. Pydoll targets exactly that gap -- no driver
binary, no `navigator.webdriver`, straight CDP to a real Chrome -- and
`enable_auto_solve_cloudflare_captcha()` automates the humanized click that
a person performed successfully in that experiment.


DO NOT TOUCH THE BROWSER WHILE THIS RUNS
----------------------------------------
In headed mode a window opens and, on a challenged IP, a Turnstile widget may
appear. Solving it yourself INVALIDATES the measurement -- the whole question
is whether *Pydoll* can clear it unattended. Let it fail if it fails; a
failure is a useful result.

The "solved by" column reports attribution, but read it as best-effort in
headed mode. It infers a Pydoll failure from a bypass error logged WHILE a
challenge was on screen -- and because the bypass also runs on pages that have
no widget at all (it is registered on every load event), a stray error is not
proof of failure. Rows it cannot call are labelled `uncertain` rather than
blamed on you.

For attribution you can fully trust, run --headless: with no visible window,
nobody could have solved anything by hand.


READING PYDOLL'S BYPASS ERRORS
------------------------------
    Error in cloudflare bypass: The specified element was not found

means Pydoll polled for the Turnstile widget, never located it, and gave up
WITHOUT CLICKING. Pydoll's own `_bypass_cloudflare` docstring explains why
this is timing-sensitive: "Cloudflare injects the widget after the load event
and re-renders the challenge iframe during its proof-of-work". Its default
detection window is only **5 seconds**, which is short for a flagged
datacenter IP where the proof-of-work runs long before the interactive widget
even mounts.

So that error usually means "looked too early / not long enough", not
"Pydoll is detected". Raise it with --captcha-wait (default here is 30s, not
Pydoll's 5s) before drawing conclusions. The genuinely bad signal is
different: a widget that CRASHES (TurnstileError 300030/300031, per §5) means
the browser is being fingerprinted -- that would be a real negative answer.


RUNNING THIS LOCALLY THROUGH THE WEBSHARE PROXIES
-------------------------------------------------
Use `--proxy-file webshare_proxies.txt` from the Windows machine. §5 already
established a positive control on those IPs: a real Chrome rendered a
solvable widget and, once solved by hand, reached real allbiz data. So
Cloudflare is *willing* to serve that IP to a browser it believes is genuine,
which isolates one single variable:

    can Pydoll's humanized click substitute for a human's click,
    on an IP where a human's click is already known to work?

INTERPRET THE RESULT ASYMMETRICALLY. Per §3, free Webshare datacenter proxies
are treated *worse* than the AWS server, not the same:

    AWS server        -> soft "Just a moment" JS challenge, escalating
    Webshare (free)   -> hard Turnstile CAPTCHA on EVERY search

It is a strictly HARDER case, not a faithful simulation. So:

    PASSES on Webshare -> strong signal it will pass on AWS.
    FAILS on Webshare  -> tells you little about AWS. Do NOT discard Pydoll
                          on this alone; retry on the server's own IP.

A good positive test and a poor negative test.


BE HONEST ABOUT THE CEILING
---------------------------
Pydoll's README refuses to promise this: "this isn't a magic bypass ...
whether it passes depends on your environment (browser fingerprint and IP
reputation)", and on headless: "Cloudflare Turnstile in headless is still
under study."

Pydoll addresses the FINGERPRINT axis only. §3 is unambiguous that IP
reputation dominates:

    residential ASN  -> never challenged at all
    AWS datacenter   -> soft challenge, escalating to hard Turnstile
    datacenter proxy -> Turnstile every time

Nothing here makes an AWS IP look residential. Which is why this also takes a
proxy -- Pydoll composes with both untested options in §9:

    §9B  SSH reverse SOCKS tunnel    ->  --proxy socks5://127.0.0.1:1080
    §9C  Webshare static residential ->  --proxy http://user:pass@host:port

On the ordering: §3 predicts plain `requests` is enough over a residential IP,
which would make a browser unnecessary. Treat that as unconfirmed. A 10-lookup
run from a residential IP produced a Turnstile challenge on 2 of 10 lookups --
so "residential ASN is never challenged" is not quite right, and plain
`requests` (which cannot clear a challenge at all) would simply lose those
lookups. Measure your own rate before betting the design on it.


WHAT COUNTS AS SUCCESS
----------------------
Both page types, because only the pair is a win: the search page has been the
reliably-fetchable one, while the detail page -- where the value is -- kept
failing. Success is parsing the detail page's JSON-LD, the same block the
production scraper depends on.

Note on counting results: this counts real `<a class="res-link">` ANCHORS via
BeautifulSoup after stripping <style>/<script>. A plain substring count of
"res-link" is wrong -- allbiz ships a `.res-link{...}` CSS rule, so an empty
zero-result page still "contains res-link" twice. (tunnel_test.py's
`resp.text.count("res-link")` has this same flaw; treat its counts of 1-2
with suspicion.)


USAGE
-----
    pip install pydoll-python beautifulsoup4     # needs Python >= 3.10

    # sweep every Webshare proxy, headed. Do not touch the browser.
    python pydoll_test.py --proxy-file webshare_proxies.txt

    # one proxy, with a longer captcha detection window
    python pydoll_test.py --proxy http://user:pass@198.23.243.226:6361 --captcha-wait 45

    # no proxy: this machine's own (residential) IP. §3 predicts "no
    # challenge at all", so this is how you confirm the SCRIPT works before
    # blaming proxies.
    python pydoll_test.py

    # on the server (see PYDOLL_SERVER_SETUP.md)
    xvfb-run -a python3 pydoll_test.py --binary ~/chrome/.../google-chrome

SEARCH TERM
-----------
`ss` is a business-NAME search, not a category browse: `--ss plumbing`
returns a page titled 'Search for "plumbing" in Texas' with NO results
container at all. The default below is the known-good case from
decodo_reliability_test.py, so a failure is the transport's fault rather than
a no-such-business case. Once the pipeline is proven, switch to a business
not hit recently for the clean §9D re-test.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from urllib.parse import urlencode, urlparse

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: pip install beautifulsoup4")

try:
    from pydoll.browser.chromium import Chrome
    from pydoll.browser.options import ChromiumOptions
    from pydoll.utils import SOCKS5Forwarder
except ImportError:
    sys.exit("Missing dependency: pip install pydoll-python  (requires Python >= 3.10)")


BASE = "https://www.allbiz.com"
SEARCH_URL = f"{BASE}/search"
IP_ECHO_URL = "https://api.ipify.org"
# api64 returns IPv6 when the client has it. Worth checking both: a Cloudflare
# block page reported "Your IP: 2401:4900:..." -- an IPv6 address -- while
# api.ipify.org reported an IPv4 one. The site was seeing the v6 address, so
# reporting only v4 describes the wrong identity, and v4 and v6 reputations are
# tracked separately.
IP_ECHO_URL6 = "https://api64.ipify.org"

# Budget for a challenge to clear once one is detected.
CHALLENGE_SETTLE_TIMEOUT = 60
# Separate budget for content to appear AFTER the page is unchallenged. Kept
# distinct so "challenge never cleared" and "cleared but the page has no
# results" stay different findings rather than one ambiguous timeout.
#
# Was 15s, which was TOO SHORT and produced false "zero listings" failures. A
# server run showed a lookup where the challenge cleared in 3.0s and then
# "content present after 12.1s" -- so results can legitimately take ~12s to
# appear after a challenge. Anything at or just past 15s was being called an
# empty page when it was still rendering. Raised well clear of the observed
# worst case; a slow success costs seconds, a false negative costs a wrong
# conclusion about whether the site is blocking you.
CONTENT_READY_TIMEOUT = 45
POLL_INTERVAL = 1.5

# Pause between proxies in a sweep. Not politeness -- back-to-back bursts from
# one machine across many IPs is itself a pattern worth not creating.
BETWEEN_PROXIES_SECONDS = 5


class BypassWatcher(logging.Handler):
    """Capture Pydoll's own verdict on its Turnstile click, with timestamps.

    `Tab._bypass_cloudflare` logs `Error in cloudflare bypass: ...` when it
    polls out without finding the widget, and logs NOTHING on success.

    TIMESTAMPS ARE ESSENTIAL, not a nicety. `enable_auto_solve_cloudflare_captcha`
    registers on `PageEvent.LOAD_EVENT_FIRED`, so the bypass runs on EVERY
    navigation -- including api.ipify.org and the detail page, neither of which
    has a Turnstile widget. Each of those polls for the full --captcha-wait
    window and then logs an error, purely because there was nothing to find.

    Those benign errors are asynchronous: with a 30s poll and ~8s lookups, an
    error from one lookup's detail-page load surfaces during the NEXT lookup.
    An earlier version counted any error seen during a lookup, which
    misattributed a Pydoll solve to a human and understated the success rate.
    So attribution must be scoped to the exact interval in which a challenge
    was on screen -- see errors_between().
    """

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.bypass_errors = []  # list of (monotonic_time, message)

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return
        if "cloudflare bypass" in message.lower():
            self.bypass_errors.append((time.monotonic(), message))

    def errors_between(self, start, end):
        """Errors logged while a challenge was actually on screen."""
        if start is None or end is None:
            return []
        return [m for t, m in self.bypass_errors if start <= t <= end]


def install_bypass_watcher():
    watcher = BypassWatcher()
    logging.getLogger("pydoll").addHandler(watcher)
    return watcher


# Identical to tunnel_test.py's classify() on purpose, so a verdict here means
# the same thing as a verdict there.
def classify(html):
    """Tell a real page apart from a Cloudflare interstitial or a hard block.

    The block case was missing originally and it cost real time: a headless run
    returned pages titled "Attention Required! | Cloudflare" -- Cloudflare's
    error-1020 firewall block, 4KB, reading "Sorry, you have been blocked. You
    are unable to access allbiz.com." Because none of the challenge markers
    matched, classify() called it `looks_real`, the harness waited the full
    content timeout, then reported "zero listings -- not a block", and blamed
    the search term. It was a block, and it is checked FIRST here because it is
    terminal: there is nothing to solve and nothing to wait for.
    """
    lowered = html.lower()
    if ("attention required" in lowered
            or "you have been blocked" in lowered
            or "cf-error-details" in lowered
            or "error 1020" in lowered):
        return "cloudflare_block"
    if "just a moment" in lowered or "cf-mitigated" in lowered:
        return "cloudflare_js_challenge"
    if "turnstile" in lowered or "quick security check" in lowered:
        return "cloudflare_captcha"
    return "looks_real"


def is_block(verdict):
    """A hard block cannot be waited out or solved -- only avoided."""
    return verdict == "cloudflare_block"


def is_challenge(verdict):
    return verdict != "looks_real"


def _strip_code(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script", "noscript"]):
        tag.decompose()
    return soup


def count_result_links(html):
    """Real result anchors only.

    NOT a substring count: allbiz ships a `.res-link{...}` CSS rule, so a
    zero-result page contains the string "res-link" twice while having no
    results whatsoever. That false positive is what made an earlier run report
    'looks_real | res-link occurrences: 2' and then fail to find any link.
    """
    return len(_strip_code(html).find_all("a", class_="res-link"))


def has_jsonld(html):
    return BeautifulSoup(html, "html.parser").find(
        "script", {"type": "application/ld+json"}
    ) is not None


def has_full_record(html):
    """JSON-LD *and* the contact block -- what production actually needs.

    JSON-LD alone is too weak a readiness gate. A run captured a 44KB detail
    page where every other lookup returned 77KB: the JSON-LD had arrived, so it
    parsed and counted as a success, but the page was still rendering. The
    contact block (`#tc .mt16.ats`) is where email, website, fax and every
    social link live -- eight-plus fields of app/scrapers/allbiz_scraper.py's
    output. Gating on JSON-LD only would let production return a record that
    parses cleanly and is quietly missing all of them, which is exactly the
    kind of silent degradation that is hardest to notice downstream.
    """
    soup = BeautifulSoup(html, "html.parser")
    if soup.find("script", {"type": "application/ld+json"}) is None:
        return False
    contact = soup.find("div", {"id": "tc"})
    return bool(contact and contact.find("div", {"class": "mt16 ats"}))


def page_title(html):
    soup = BeautifulSoup(html, "html.parser")
    return soup.title.get_text(strip=True) if soup.title else ""


def build_options(args):
    """Chrome options. Kept deliberately minimal.

    §5's lesson was that every added stealth/patching layer made treatment
    WORSE, so this does not pile on flags. In particular it does NOT call
    apply_fingerprint(): Pydoll's docs warn a fingerprint whose
    locale/timezone contradicts the egress IP's geography is *more* suspicious
    than an untouched browser. Bring a geo-matched profile first.

    No --user-data-dir by default either: Pydoll always launches Chrome with
    its own temp profile, so §5's "Chrome silently ignores --proxy-server if
    Chrome is already running" trap does not apply here.
    """
    options = ChromiumOptions()

    if args.binary:
        options.binary_location = args.binary
    if args.headless:
        options.headless = True
    if args.user_data_dir:
        options.add_argument(f"--user-data-dir={args.user_data_dir}")
    if args.no_sandbox:
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    return options


def split_proxy(raw):
    """Return (chrome_proxy_arg, forwarder_or_None).

    Chrome cannot do authenticated SOCKS5 at all (Chromium #40323993), so for
    socks5://user:pass@host:port we start Pydoll's bundled SOCKS5Forwarder --
    a local no-auth listener that performs the upstream authenticated
    handshake -- and point Chrome at that.

    Authenticated HTTP proxies (the Webshare ones) need no such trick:
    Pydoll's ProxyManager strips the credentials and answers Chrome's CDP auth
    challenge with them, so there is no dialog to click through.
    """
    if not raw:
        return None, None

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "http").lower()

    if scheme.startswith("socks") and parsed.username:
        forwarder = SOCKS5Forwarder(
            remote_host=parsed.hostname,
            remote_port=parsed.port or 1080,
            username=parsed.username,
            password=parsed.password or "",
            local_port=0,
        )
        return None, forwarder

    return raw, None


def redact(proxy):
    """Proxy string safe to print -- keeps host:port, drops credentials."""
    if not proxy:
        return "DIRECT (own IP)"
    parsed = urlparse(proxy)
    if parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    return proxy


def slug(proxy, index):
    """Filename-safe tag so a sweep doesn't overwrite its own saved HTML."""
    if not proxy:
        return "direct"
    host = (urlparse(proxy).hostname or "proxy").replace(".", "-")
    return f"{index:02d}_{host}"


async def settle(tab, label, ready, challenge_timeout, ready_timeout=None):
    """Wait out a challenge, then wait for content. Reports the two separately.

    Returns a dict: html, verdict, challenged, cleared, ready, seconds.

    The two phases are deliberately distinct budgets. "The challenge never
    cleared" and "the challenge cleared but the page has no results" are
    completely different findings -- the first is an access problem, the
    second is a query or markup problem -- and collapsing them into one
    timeout is how you end up chasing the wrong bug.
    """
    started = time.time()
    html = await tab.page_source
    verdict = classify(html)
    challenged = is_challenge(verdict)
    cleared = not challenged
    # Monotonic bounds of the interval a challenge was actually on screen.
    # Only bypass errors inside this window say anything about THIS challenge
    # (see BypassWatcher).
    chal_t0 = chal_t1 = None

    if is_block(verdict):
        # Terminal. Waiting is pointless -- Cloudflare has refused the client
        # outright rather than offering a challenge.
        print(f"    [{label}] HARD BLOCK (Cloudflare error 1020) -- refused outright, "
              f"no challenge offered")
        return {"html": html, "verdict": verdict, "challenged": True,
                "cleared": False, "ready": False, "blocked": True,
                "chal_t0": None, "chal_t1": None,
                "seconds": round(time.time() - started, 1)}

    if challenged:
        chal_t0 = time.monotonic()
        print(f"    [{label}] challenge present ({verdict}) -- waiting up to "
              f"{challenge_timeout:.0f}s for it to clear...")
        while time.time() - started < challenge_timeout:
            await asyncio.sleep(POLL_INTERVAL)
            html = await tab.page_source
            verdict = classify(html)
            if not is_challenge(verdict):
                cleared = True
                chal_t1 = time.monotonic()
                print(f"    [{label}] challenge cleared after {time.time() - started:.1f}s")
                break
        if not cleared:
            chal_t1 = time.monotonic()
            print(f"    [{label}] STILL CHALLENGED after {time.time() - started:.1f}s")
            return {"html": html, "verdict": verdict, "challenged": True,
                    "cleared": False, "ready": False, "blocked": False,
                    "chal_t0": chal_t0, "chal_t1": chal_t1,
                    "seconds": round(time.time() - started, 1)}
    else:
        print(f"    [{label}] no challenge at all -- page served directly")

    # Phase 2: the page is unchallenged. Is the content actually there yet?
    budget = ready_timeout or CONTENT_READY_TIMEOUT
    ready_started = time.time()
    is_ready = ready(html)
    while not is_ready and time.time() - ready_started < budget:
        await asyncio.sleep(POLL_INTERVAL)
        html = await tab.page_source
        is_ready = ready(html)
    if is_ready:
        print(f"    [{label}] content present after {time.time() - ready_started:.1f}s")
    else:
        print(f"    [{label}] unchallenged, but expected content never appeared "
              f"({budget:.0f}s) -- either a genuinely empty page or a "
              f"results-less shell served to a client the site dislikes")

    return {"html": html, "verdict": verdict, "challenged": challenged,
            "cleared": True, "ready": is_ready, "blocked": False,
            "chal_t0": chal_t0, "chal_t1": chal_t1,
            "seconds": round(time.time() - started, 1)}


def save(name, html):
    with open(name, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"    saved -> {name}")


def first_detail_url(html):
    """First business listing link on a search results page.

    Same selectors the production scraper uses, so if these stop matching that
    is itself a finding about allbiz's markup rather than a bug here. Falls
    back to a document-wide search if the container id is absent, and reports
    which case it hit.
    """
    soup = _strip_code(html)
    container = soup.find("div", id="containerRenderBody")
    scope = container or soup
    if container is None:
        anywhere = soup.find_all("a", class_="res-link")
        if not anywhere:
            return None, "no #containerRenderBody and no a.res-link anywhere"
        print("    note: #containerRenderBody absent but res-link anchors exist "
              "-- markup may have changed; production selectors would break")
    link = scope.find("a", class_="res-link")
    if not link:
        return None, "results present but no a.res-link inside #containerRenderBody"
    href = link.get("href", "").strip()
    if href.startswith("/"):
        href = BASE + href
    return (href, None) if href else (None, "res-link anchor has no href")


def parse_record(html):
    """Parse the detail page's JSON-LD -- the real definition of success."""
    ld = BeautifulSoup(html, "html.parser").find("script", {"type": "application/ld+json"})
    if not ld:
        return None, "no JSON-LD block -- detail page did not render properly"
    try:
        return json.loads(ld.text), None
    except json.JSONDecodeError as exc:
        return None, f"JSON-LD present but unparseable: {exc}"


def print_record(data):
    addr = data.get("address") or {}
    employee = data.get("employee") or {}
    print("    " + "-" * 62)
    print(f"    Business : {data.get('name')}")
    print(f"    Phone    : {data.get('telephone')}")
    print(f"    Address  : {addr.get('streetAddress')}, "
          f"{addr.get('addressLocality')} {addr.get('postalCode')}")
    print(f"    Contact  : {employee.get('name')} ({employee.get('jobTitle')})")
    print("    " + "-" * 62)


async def run_once(args, chrome_proxy, tag, watcher):
    """One full search+detail attempt. Returns a structured result dict."""
    result = {
        "proxy": redact(chrome_proxy),
        "exit_ip": None,
        "search_verdict": None,
        "search_challenged": None,
        "search_cleared": None,
        "search_links": 0,
        "detail_verdict": None,
        "detail_challenged": None,
        "record_ok": False,
        "solved_by": None,
        "error": None,
    }
    started_all = time.time()
    errors_before = len(watcher.bypass_errors)

    options = build_options(args)
    if chrome_proxy:
        options.add_argument(f"--proxy-server={chrome_proxy}")

    def note_solver(settled):
        """Attribute the clear, using only errors logged while the challenge
        was on screen (see BypassWatcher for why the window matters).
        """
        if not result["search_challenged"]:
            result["solved_by"] = "n/a (never challenged)"
            return
        in_window = watcher.errors_between(settled.get("chal_t0"), settled.get("chal_t1"))
        if result.get("search_cleared") and in_window:
            result["solved_by"] = "uncertain (bypass errored)"
        elif result.get("search_cleared"):
            result["solved_by"] = "pydoll"
        else:
            result["solved_by"] = "nobody (never cleared)"

    try:
        async with Chrome(options=options) as browser:
            tab = await browser.start()

            # Enable the humanized Turnstile click for the WHOLE session, not
            # just one navigation: a lookup is two page loads and either can be
            # challenged independently. Pydoll's default detection window is
            # 5s, which is short for a flagged IP -- see --captcha-wait.
            await tab.enable_auto_solve_cloudflare_captcha(
                time_to_wait_captcha=args.captcha_wait
            )

            # --- 0. Which IP does allbiz actually see? --------------------
            # Asked through the browser so it reflects the real egress path.
            # A wrong exit IP invalidates everything below it.
            try:
                await tab.go_to(IP_ECHO_URL, timeout=60)
                found = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", await tab.page_source)
                result["exit_ip"] = found.group(0) if found else "(unparsed)"
                print(f"    exit IP: {result['exit_ip']}")
            except Exception as exc:
                result["error"] = f"proxy/exit-IP check failed: {exc}"
                print(f"    exit IP check FAILED: {exc}")
                if chrome_proxy:
                    print("    proxy looks unreachable -- skipping this one")
                    return result

            # --- 1. Search page ------------------------------------------
            query = urlencode({"ss": args.ss, "ia": args.ia})
            print(f"    [1/2] search: ?{query}")
            await tab.go_to(f"{SEARCH_URL}?{query}", timeout=args.timeout)
            s = await settle(tab, "search", lambda h: count_result_links(h) > 0,
                             args.challenge_wait)
            result["search_verdict"] = s["verdict"]
            result["search_challenged"] = s["challenged"]
            result["search_cleared"] = s["cleared"]
            result["search_links"] = count_result_links(s["html"])
            note_solver(s)
            print(f"    verdict: {s['verdict']} | result anchors: {result['search_links']} "
                  f"| title: {page_title(s['html'])!r}")
            print(f"    solved by: {result['solved_by']}")
            save(f"pydoll_{tag}_search.html", s["html"])

            if not s["cleared"]:
                result["error"] = "search page never cleared the challenge"
                return result
            if not s["cleared"] and s.get("blocked"):
                result["error"] = ("HARD BLOCKED by Cloudflare (error 1020) -- the client "
                                   "was refused outright, not challenged")
                return result
            if result["search_links"] == 0:
                result["error"] = (
                    "search page loaded and returned ZERO listings. Check the saved "
                    "HTML's <title>: a real allbiz page titles itself 'Search for "
                    "\"<term>\" in <State>' and genuinely has no matches; anything "
                    "mentioning Cloudflare is a block, not a query problem."
                )
                return result

            detail_url, why = first_detail_url(s["html"])
            if not detail_url:
                result["error"] = why
                return result

            # --- 2. Detail page (the one that kept failing via Decodo) ----
            print(f"    [2/2] detail: {detail_url}")
            await tab.go_to(detail_url, timeout=args.timeout)
            d = await settle(tab, "detail", has_jsonld, args.challenge_wait)
            result["detail_verdict"] = d["verdict"]
            result["detail_challenged"] = d["challenged"]
            print(f"    verdict: {d['verdict']} | {len(d['html'])} chars")
            if d["challenged"]:
                detail_errors = watcher.errors_between(d.get("chal_t0"), d.get("chal_t1"))
                if detail_errors:
                    print(f"    detail bypass errors ({len(detail_errors)}) in window:")
                    for _, msg in detail_errors:
                        print(f"      {msg}")
                else:
                    print("    detail bypass: no ERROR-level messages logged in this window "
                          "-- either the click succeeded but the page didn't advance, or "
                          "Pydoll never logged a failure (use --debug for more detail)")
            save(f"pydoll_{tag}_detail.html", d["html"])

            if not d["cleared"]:
                result["error"] = "detail page never cleared the challenge"
                return result

            data, problem = parse_record(d["html"])
            if problem:
                result["error"] = problem
                return result

            result["record_ok"] = True
            result["business"] = data.get("name")
            print_record(data)
            return result

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"    FAILED: {result['error']}")
        return result
    finally:
        result["seconds"] = round(time.time() - started_all, 1)


async def attempt(args, raw_proxy, tag, watcher):
    """Resolve the proxy (starting a SOCKS5 shim if needed) and run once."""
    chrome_proxy, forwarder = split_proxy(raw_proxy)

    if forwarder is None:
        return await run_once(args, chrome_proxy, tag, watcher)

    # SOCKS5Forwarder.start() rewrites .local_port with the port it bound.
    async with forwarder as running:
        local = f"socks5://127.0.0.1:{running.local_port}"
        print(f"    [socks5] {local} -> {forwarder.remote_host}:"
              f"{forwarder.remote_port} (authenticated)")
        return await run_once(args, local, tag, watcher)


def load_proxies(path):
    """One proxy per line. Blank lines and # comments ignored."""
    proxies = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)
    if not proxies:
        sys.exit(f"No proxies found in {path}")
    return proxies


_SHORT_VERDICT = {
    "looks_real": "real",
    "cloudflare_js_challenge": "js_challenge",
    "cloudflare_captcha": "captcha",
}


def _cell(verdict, challenged):
    label = _SHORT_VERDICT.get(verdict, verdict) if verdict else "-"
    return f"{label} (chal)" if challenged else label


def summarize(results):
    """Print the table, then say what it means."""
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"{'#':>3}  {'proxy':<28} {'search':<18} {'detail':<18} {'rec':<4} solved by")
    print("-" * 78)
    for i, r in enumerate(results, start=1):
        print(f"{i:>3}  {r['proxy'][:28]:<28} "
              f"{_cell(r['search_verdict'], r['search_challenged']):<18} "
              f"{_cell(r['detail_verdict'], r['detail_challenged']):<18} "
              f"{('YES' if r['record_ok'] else 'no'):<4} {r['solved_by'] or '-'}")
        if r["error"] and not r["record_ok"]:
            print(f"     └─ {r['error']}")
    print("-" * 78)

    wins = [r for r in results if r["record_ok"]]
    pydoll_solved = [r for r in results if r["solved_by"] == "pydoll"]
    human_solved = [r for r in results if r["solved_by"]
                    and r["solved_by"].startswith("uncertain")]
    never_cleared = [r for r in results if r["search_cleared"] is False]
    empty = [r for r in results if r["search_cleared"] and r["search_links"] == 0]

    print(f"Full records retrieved:      {len(wins)}/{len(results)}")
    print(f"Challenge cleared by pydoll: {len(pydoll_solved)}")
    print(f"Cleared, attribution unclear: {len(human_solved)}  <- see note below")
    print(f"Never cleared:               {len(never_cleared)}")
    print(f"Cleared but zero listings:   {len(empty)}")
    print("=" * 78)

    print("\nWHAT THIS MEANS")
    if pydoll_solved and wins:
        print("  Pydoll cleared a real challenge on its own AND the record came back.")
        print("  That is the result that justifies adopting it: on these IPs a real")
        print("  Chrome needed a HUMAN click (§5) -- Pydoll's click did it unattended.")
        print("  Next question is reliability, not feasibility. Run it 10x the way")
        print("  decodo_reliability_test.py did for Decodo before designing around it.")
    elif human_solved:
        print("  A bypass error landed while a challenge was on screen, so this row's")
        print("  attribution is unclear: Pydoll may have failed and something else")
        print("  cleared it. Note the bypass also runs on pages with NO widget (it is")
        print("  registered on every load event), so a stray error is not proof of")
        print("  failure. For an attribution you can fully trust, rerun --headless:")
        print("  with no visible window a human cannot have solved anything.")
        print("  Before concluding Pydoll can't do it, raise --captcha-wait: its")
        print("  default detection window is 5s, and Cloudflare mounts the widget")
        print("  after the load event, mid proof-of-work. 45-60s is not unreasonable")
        print("  on a flagged datacenter IP.")
    elif empty and not never_cleared:
        print("  Access is FINE -- pages loaded unchallenged or cleared -- but the")
        print("  search returned no listings. That is a query problem, not a blocking")
        print("  problem: `ss` matches business NAMES, so a generic keyword finds")
        print("  nothing. Rerun with a real business name.")
    elif never_cleared:
        print("  Challenges never cleared. Read this ASYMMETRICALLY: per §3 these")
        print("  datacenter IPs get a HARDER tier (Turnstile every search) than the")
        print("  AWS server (soft JS challenge). Failing the harder tier does not")
        print("  predict failing the easier one -- run on the server's own IP next")
        print("  (PYDOLL_SERVER_SETUP.md), the §5 arm never tried programmatically.")
        print("  Check the saved HTML too: a widget that CRASHES (300030/300031) means")
        print("  Pydoll is being fingerprinted -- a real negative -- whereas a widget")
        print("  that just sits there means try a longer --captcha-wait.")
    else:
        print("  Mixed. Compare the per-proxy rows: if some IPs pass and others don't")
        print("  on identical settings, that is IP reputation varying, not the browser")
        print("  -- the same 'IP luck' §6 concluded was doing the work in Decodo's")
        print("  retry ladder.")
    print()


def install_debug_logging():
    """Surface Pydoll's own bypass logging live, not just the ERROR-level
    lines BypassWatcher captures for attribution. The success case is
    documented as silent (see BypassWatcher), but any INFO/DEBUG breadcrumbs
    Pydoll emits along the way -- finding the element, attempting a click,
    waiting on it -- only show up if the 'pydoll' logger's own level is
    lowered. Its default level is above DEBUG, so a handler alone is not
    enough; the logger itself has to be turned down too.
    """
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("    [pydoll %(levelname)s] %(message)s"))
    pydoll_logger = logging.getLogger("pydoll")
    pydoll_logger.setLevel(logging.DEBUG)
    pydoll_logger.addHandler(handler)


async def main_async(args):
    watcher = install_bypass_watcher()
    if args.debug:
        install_debug_logging()

    if args.proxy_file:
        proxies = load_proxies(args.proxy_file)
        print(f"Sweeping {len(proxies)} prox{'y' if len(proxies) == 1 else 'ies'} "
              f"from {args.proxy_file}")
    else:
        proxies = [args.proxy]

    print("=" * 78)
    print("Pydoll test -- allbiz.com")
    print(f"  mode         : {'headless' if args.headless else 'headed'}")
    print(f"  binary       : {args.binary or 'pydoll default'}")
    print(f"  search       : ss={args.ss!r} ia={args.ia!r}")
    print(f"  captcha wait : {args.captcha_wait}s (pydoll default is 5s)")
    print("=" * 78)
    if not args.headless:
        print("\n  !! DO NOT interact with the browser window. Solving a captcha")
        print("     yourself invalidates the measurement -- the question is whether")
        print("     PYDOLL can clear it unattended. A failure is a useful result.\n")

    results = []
    for index, raw in enumerate(proxies, start=1):
        print(f"\n[{index}/{len(proxies)}] {redact(raw)}")
        results.append(await attempt(args, raw, slug(raw, index), watcher))
        if index < len(proxies):
            await asyncio.sleep(BETWEEN_PROXIES_SECONDS)

    summarize(results)
    return any(r["record_ok"] for r in results)


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Test allbiz.com access via Pydoll (real Chrome over CDP, no WebDriver).",
    )
    p.add_argument("--proxy", default=None,
                   help="Single proxy URL: http://user:pass@host:port, "
                        "socks5://127.0.0.1:1080 (SSH tunnel), or socks5://user:pass@host:port.")
    p.add_argument("--proxy-file", default=None,
                   help="File of proxy URLs, one per line (# comments ok). Runs the full "
                        "flow through each and prints a comparison table.")
    p.add_argument("--headless", action="store_true",
                   help="Run headless. Pydoll hedges on Turnstile-in-headless, and §5's "
                        "working arm was headed -- prefer headed (xvfb-run on a server).")
    p.add_argument("--binary", default=None,
                   help="Path to the Chrome/Chromium executable, if not in a default location.")
    p.add_argument("--user-data-dir", default=None,
                   help="Pin a specific Chrome profile. Rarely needed: Pydoll already "
                        "launches with its own temp profile.")
    p.add_argument("--no-sandbox", action="store_true",
                   help="Add --no-sandbox (needed when running as root, e.g. in a container).")
    p.add_argument("--ss", default="american apparel",
                   help="Business-NAME search term (not a category). Default is the "
                        "known-good case, so failures are the transport's fault.")
    p.add_argument("--ia", default="US_FL", help="State code, e.g. US_FL, US_TX, US_CA.")
    p.add_argument("--captcha-wait", type=float, default=30,
                   help="Seconds Pydoll polls for the Turnstile widget. Pydoll's own "
                        "default is 5, which is short for a flagged IP (the widget mounts "
                        "after the load event, mid proof-of-work). Default here: 30.")
    p.add_argument("--challenge-wait", type=float, default=CHALLENGE_SETTLE_TIMEOUT,
                   help=f"Seconds to wait for a challenge to clear "
                        f"(default {CHALLENGE_SETTLE_TIMEOUT}).")
    p.add_argument("--timeout", type=int, default=120, help="Per-navigation timeout (seconds).")
    p.add_argument("--debug", action="store_true",
                   help="Print Pydoll's own internal bypass logging live (DEBUG level on the "
                        "'pydoll' logger), not just the attribution summary. Use this when a "
                        "widget is present but never clears, to see whether Pydoll ever found "
                        "or attempted to click it.")
    args = p.parse_args(argv)
    if args.proxy and args.proxy_file:
        p.error("use --proxy or --proxy-file, not both")
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
