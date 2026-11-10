"""Animepahe client — search, release list, play page scraping.

All requests go through curl_cffi with Chrome TLS impersonation. Animepahe
sits behind DDoS-Guard (not Cloudflare) and accepted our naive urllib probe
with a 200, so impersonation here is belt-and-suspenders.

Reference: the play page uses button rows like

    <button data-src="https://kwik.si/e/ABC" data-audio="jpn"
            data-resolution="1080" data-fansub="EVA">1080p</button>

We harvest every button, let the caller sort by preferred audio/resolution,
then hand the chosen kwik embed URL to extractors.kwik for unpacking.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from curl_cffi import requests as cr

BASE = "https://animepahe.ru"
IMPERSONATE_CANDIDATES = ("chrome131", "chrome124", "chrome120")

_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "referer": BASE + "/",
}


def _impersonate() -> str:
    """Pick the newest Chrome impersonation profile the installed curl_cffi knows."""
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


def _get(url: str, timeout: int = 8):
    return cr.get(url, headers=_HEADERS, impersonate=impersonate(), timeout=timeout)


def search(query: str) -> list[dict]:
    """Return list of hits: {id, title, session, episodes, type, poster, score, ...}."""
    r = _get(f"{BASE}/api?m=search&q={quote(query)}")
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    return data.get("data") or []


def release(anime_session: str, page: int = 1) -> dict:
    """Return the release-list page payload (total, per_page, last_page, data)."""
    r = _get(f"{BASE}/api?m=release&id={anime_session}&sort=episode_asc&page={page}")
    if r.status_code != 200:
        return {"data": [], "last_page": 1}
    try:
        return r.json()
    except Exception:
        return {"data": [], "last_page": 1}


def find_episode(anime_session: str, episode_number: float) -> dict | None:
    """Walk release pages until we find the episode matching ``episode_number``.

    Episode numbers can be floats (e.g. 7.5 for specials), so we compare with a
    small epsilon rather than strict equality.
    """
    page = 1
    want = float(episode_number)
    while page <= 50:  # hard cap; animepahe's longest series fit comfortably
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


# Match animepahe play-page button rows. Buttons carry data-src (kwik embed),
# data-audio (jpn/eng), data-resolution (360/720/1080), and data-fansub.
_BUTTON_RE = re.compile(
    r'<button[^>]*?data-src="([^"]+)"[^>]*?data-audio="([^"]+)"[^>]*?data-resolution="([^"]+)"[^>]*?(?:data-fansub="([^"]*)")?',
    re.IGNORECASE | re.DOTALL,
)


def play_sources(anime_session: str, episode_session: str) -> list[dict]:
    """Scrape the play page for every kwik embed candidate with metadata."""
    url = f"{BASE}/play/{anime_session}/{episode_session}"
    headers = {**_HEADERS, "accept": "text/html,application/xhtml+xml,*/*"}
    r = cr.get(url, headers=headers, impersonate=impersonate(), timeout=8)
    if r.status_code != 200:
        return []
    out = []
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
