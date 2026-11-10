"""Cloudflare challenge solver using Camoufox.

Camoufox is a Firefox fork with native C++ fingerprint spoofing — the only
category of tool that reliably clears 2026-era CF "Just a moment..." Turnstile
challenges from a non-residential IP.

Strategy:
  1. curl_cffi tries the request with cached cookies (fast, no browser).
  2. If we get back a CF challenge page (403 + known markers), we launch Camoufox
     once, navigate to the origin, wait for the challenge to clear, scrape the
     cf_clearance + __cf_bm cookies, and cache them for ~25 min.
  3. curl_cffi retries with the fresh cookies and we're through.

Only one Camoufox instance runs at a time (serialized by _LOCK) because each
Firefox process eats 250-400 MB and Render's free tier has 512 MB total. The
cache + lock means bursts of requests to the same domain share one solve.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Tuple

logger = logging.getLogger("cf_bypass")

# domain -> (expires_at_epoch, cookies_dict)
_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}
_LOCK = threading.Lock()

# cf_clearance is typically valid 30 min; refresh a little early
TTL_SECONDS = 25 * 60

# Response-body markers that indicate we got a CF interstitial instead of real content
CHALLENGE_MARKERS = (
    b"Just a moment",
    b"cdn-cgi/challenge-platform",
    b"Attention Required",
    b"_cf_chl_opt",
)


def looks_like_challenge(body: bytes) -> bool:
    if not body:
        return False
    head = body[:4096]
    return any(m in head for m in CHALLENGE_MARKERS)


def cached_cookies(domain: str) -> Dict[str, str] | None:
    hit = _CACHE.get(domain)
    if hit and time.time() < hit[0]:
        return hit[1]
    return None


def format_cookie_header(cookies: Dict[str, str], extra: str = "") -> str:
    parts = [f"{k}={v}" for k, v in cookies.items()]
    if extra:
        parts.append(extra.rstrip(";"))
    return "; ".join(parts)


def get_cookies(domain: str) -> Dict[str, str]:
    """Return fresh cookies for the domain, solving the CF challenge if needed."""
    hit = cached_cookies(domain)
    if hit is not None:
        return hit

    with _LOCK:
        # Someone else may have solved while we waited on the lock
        hit = cached_cookies(domain)
        if hit is not None:
            return hit

        cookies = _solve(domain)
        _CACHE[domain] = (time.time() + TTL_SECONDS, cookies)
        return cookies


def _solve(domain: str) -> Dict[str, str]:
    """Launch headless Camoufox, visit the origin, wait for CF clear, return cookies."""
    # Import lazily so an import-time failure doesn't nuke the whole Flask app.
    # If Camoufox isn't installed we fall back to empty cookies and the retry will
    # just fail cleanly instead of 500'ing the whole request.
    try:
        from camoufox.sync_api import Camoufox
    except Exception as e:
        logger.error("Camoufox import failed: %s", e)
        return {}

    started = time.time()
    url = f"https://{domain}/"
    cookies: Dict[str, str] = {}

    try:
        with Camoufox(headless=True) as browser:
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # The challenge auto-runs; wait for the title to stop being "Just a moment..."
                for _ in range(10):
                    title = (page.title() or "").lower()
                    if (
                        "just a moment" not in title
                        and "attention required" not in title
                        and "cloudflare" not in title
                    ):
                        break
                    page.wait_for_timeout(1500)

                # Harvest cookies for this origin
                ctx = browser.contexts[0] if browser.contexts else page.context
                for c in ctx.cookies(url):
                    cookies[c["name"]] = c["value"]
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception as e:
        logger.error("Camoufox solve for %s failed: %s", domain, e)
        return {}

    elapsed = time.time() - started
    logger.info("cf_bypass %s solved in %.1fs, %d cookies: %s",
                domain, elapsed, len(cookies), list(cookies.keys()))
    return cookies
