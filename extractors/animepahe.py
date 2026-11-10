"""Animepahe client — search, release list, play page scraping.

All HTTP goes through ``_fetch()`` which:
  1. Fires the request through curl_cffi with Chrome TLS impersonation.
  2. On a CF challenge response, hands off to ``cf_bypass.get_cookies()`` to
     launch Camoufox, solve the challenge, cache the cookies, then retries once.

Target host rotates (animepahe.ru → animepahe.pw currently); ``BASE`` is pulled
from env with a sane default so we can point at a new host via Render env vars
without redeploying code.

Animepahe's /api gate requires ``X-Requested-With: XMLHttpRequest`` + a
``__ddg2_`` cookie primer or it returns the HTML homepage with 200 instead of
the JSON body — that cost us a session earlier, hence the explicit headers.
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote

from curl_cffi import requests as cr

from extractors import cf_bypass

BASE = os.environ.get("ANIMEPAHE_BASE", "https://animepahe.pw").rstrip("/")
HOST = BASE.split("//", 1)[-1]

IMPERSONATE_CANDIDATES = ("chrome131", "chrome124", "chrome120")

_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "referer": BASE + "/",
    "x-requested-with": "XMLHttpRequest",
    # Primer only; real __ddg2_ gets set via the CF bypass flow when it kicks in.
    "cookie": "__ddg2_=1;",
}


def _impersonate() -> str:
    for candidate in IMPERSONATE_CANDIDATES:
        try:
            cr.get("https://api.github.com/zen", impersonate=candidate, timeout=3)
            return candidate
        except Exception as e:
            if "not supported" in str(e).lower():
                continue
            return candidate
    return IMPERSONATE_CANDIDATES[-1]


_IMPERSONATE: str | None = None


def impersonate() -> str:
    global _IMPERSONATE
    if _IMPERSONATE is None:
        _IMPERSONATE = _impersonate()
    return _IMPERSONATE


def _merged_cookie(extra: str) -> str:
    """Combine the __ddg2_ primer with any cached CF cookies for the host."""
    cached = cf_bypass.cached_cookies(HOST) or {}
    return cf_bypass.format_cookie_header(cached, extra=extra)


def _fetch(url: str, timeout: int = 12, override_accept: str | None = None):
    """GET with automatic CF-challenge retry via Camoufox cookie harvest."""
    headers = dict(_HEADERS)
    if override_accept:
        headers["accept"] = override_accept
    headers["cookie"] = _merged_cookie(_HEADERS["cookie"])

    r = cr.get(url, headers=headers, impersonate=impersonate(), timeout=timeout, allow_redirects=True)

    # Did we hit a CF interstitial? 403 is the common code but some configs use 503.
    if r.status_code in (403, 429, 503) and cf_bypass.looks_like_challenge(r.content):
        cf_bypass.get_cookies(HOST)  # populates the cache; may take 10-20s on first hit
        headers["cookie"] = _merged_cookie(_HEADERS["cookie"])
        r = cr.get(url, headers=headers, impersonate=impersonate(), timeout=timeout, allow_redirects=True)
    return r


def search(query: str) -> list[dict]:
    r = _fetch(f"{BASE}/api?m=search&q={quote(query)}")
    if r.status_code != 200:
        return []
    try:
        return (r.json() or {}).get("data") or []
    except Exception:
        return []


def release(anime_session: str, page: int = 1) -> dict:
    r = _fetch(f"{BASE}/api?m=release&id={anime_session}&sort=episode_asc&page={page}")
    if r.status_code != 200:
        return {"data": [], "last_page": 1}
    try:
        return r.json() or {"data": [], "last_page": 1}
    except Exception:
        return {"data": [], "last_page": 1}


def find_episode(anime_session: str, episode_number: float) -> dict | None:
    """Walk release pages for the episode with the matching number (floats OK)."""
    page = 1
    want = float(episode_number)
    while page <= 50:
        payload = release(anime_session, page=page)
        for ep in payload.get("data") or []:
            try:
                got = float(ep.get("episode"))
            except (TypeError, ValueError):
                continue
            if abs(got - want) < 0.001:
                return ep
        if page >= int(payload.get("last_page", 1) or 1):
            break
        page += 1
    return None


_BUTTON_RE = re.compile(
    r'<button[^>]*?data-src="([^"]+)"[^>]*?data-audio="([^"]+)"[^>]*?data-resolution="([^"]+)"[^>]*?(?:data-fansub="([^"]*)")?',
    re.IGNORECASE | re.DOTALL,
)


def play_sources(anime_session: str, episode_session: str) -> list[dict]:
    """Scrape the play page for every kwik embed candidate with metadata."""
    url = f"{BASE}/play/{anime_session}/{episode_session}"
    r = _fetch(url, override_accept="text/html,application/xhtml+xml,*/*")
    if r.status_code != 200:
        return []
    out: list[dict] = []
    for m in _BUTTON_RE.finditer(r.text):
        try:
            quality = int(m.group(3))
        except ValueError:
            continue
        out.append({
            "embed": m.group(1),
            "audio": m.group(2).lower(),
            "quality": quality,
            "fansub": (m.group(4) or "").strip(),
        })
    return out
