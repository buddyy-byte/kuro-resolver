"""Kwik embed unpacker — fetch kwik.si/e/<id> and extract the m3u8 URL
from its Dean Edwards P.A.C.K.E.R.'d JavaScript payload.

Kwik's host rotates between kwik.si / kwik.cx / kwik.sx over the course of
weeks; curl_cffi follows redirects by default so the inbound embed URL works
whichever host is current.

The packer wrapper looks like::

    eval(function(p,a,c,k,e,d){ ... }('ENCODED', BASE, COUNT, 'word|list'.split('|'), 0, {}))

Decoding is a direct port of the JS: iterate every token from COUNT-1 down to
0, base-N encode it to its placeholder, and whole-word replace the placeholder
with the matching entry from the word list. After unpacking we regex the first
.m3u8 URL out of the readable source.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from curl_cffi import requests as cr

from extractors import animepahe  # share the impersonate picker

ANIMEPAHE_REFERER = "https://animepahe.ru/"


def _encode(c: int, base: int) -> str:
    """Base-N encode an int using 0-9, a-z, A-Z (matches the JS packer convention)."""
    if c < base:
        s = ""
    else:
        s = _encode(c // base, base)
    c = c % base
    if c < 10:
        return s + chr(c + ord("0"))
    if c < 36:
        return s + chr(c + ord("a") - 10)
    return s + chr(c + ord("A") - 36)


def unpack(packed: str) -> str | None:
    """Recover the readable source from a Dean Edwards packed payload."""
    m = re.search(
        r"}\s*\(\s*'(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'\.split\(",
        packed,
        re.DOTALL,
    )
    if not m:
        return None
    payload, base_s, count_s, words_str = m.groups()
    base = int(base_s)
    count = int(count_s)
    words = words_str.split("|")
    payload = payload.replace("\\'", "'").replace("\\\\", "\\")
    for c in range(count - 1, -1, -1):
        if c < len(words) and words[c]:
            token = _encode(c, base)
            payload = re.sub(r"\b" + re.escape(token) + r"\b", words[c], payload)
    return payload


_M3U8_RE = re.compile(r"""https?://[^\s"'<>]+\.m3u8[^\s"'<>]*""")


def extract(embed_url: str) -> tuple[str, str] | None:
    """Return (m3u8_url, referer) or None if extraction fails."""
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "referer": ANIMEPAHE_REFERER,
    }
    r = cr.get(
        embed_url,
        headers=headers,
        impersonate=animepahe.impersonate(),
        timeout=8,
        allow_redirects=True,
    )
    if r.status_code != 200:
        return None

    # The packed script is inside the HTML; unpack everything that looks like a packer call
    # (there are usually two or three on the page; the one we want produces the m3u8 URL).
    candidates = re.findall(r"eval\(function\(p,a,c,k,e,[dr]?\).*?split\('\|'\),0,\{?\}?\)\)", r.text, re.DOTALL)
    if not candidates:
        candidates = [r.text]

    for chunk in candidates:
        unpacked = unpack(chunk)
        if not unpacked:
            continue
        m = _M3U8_RE.search(unpacked)
        if m:
            parsed = urlparse(embed_url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"
            return m.group(0), referer

    return None
