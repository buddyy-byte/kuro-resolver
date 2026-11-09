"""Miruro v1 catalog client: XOR('miruro/catalog') + gzip over curl_cffi TLS impersonation."""
from __future__ import annotations

import json
import os
import time
import zlib

from curl_cffi import requests as cr

CHAIN = ("https://www.miruro.to", "https://miruro.to")
BASE = os.environ.get("MIRURO_BASE") or CHAIN[0]
KEY = b"miruro/catalog"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# what answered last — surfaced for diagnostics
ACTIVE = {"base": BASE}


def bases() -> list[str]:
    """Env override first, then the known-good hosts.

    Verified 2026-10-28: www.miruro.to serves /api/v1/* (200) while miruro.cx does
    not (404), so repointing the resolver when Miruro phases .to out is an env
    change (MIRURO_BASE) rather than a code change.
    """
    env = (os.environ.get("MIRURO_BASE") or "").strip().rstrip("/")
    chain = [env] if env else []
    for b in CHAIN:
        if b not in chain:
            chain.append(b)
    return chain


def headers_for(base: str) -> dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": base + "/",
        "origin": base,
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "user-agent": UA,
    }


HEADERS = headers_for(BASE)

_cache: dict[str, tuple[float, object]] = {}


def _decode(raw: bytes):
    buf = bytearray(raw)
    kl = len(KEY)
    for i in range(len(buf)):
        buf[i] ^= KEY[i % kl]
    data = bytes(buf)
    for wbits in (47, -15, 31):
        try:
            return json.loads(zlib.decompress(data, wbits).decode("utf-8"))
        except Exception:
            continue
    # not compressed — plain JSON fallback
    return json.loads(data.decode("utf-8"))


def api(path: str, ttl: float = 300.0):
    """GET a Miruro v1 path, decoded, cached, with base failover."""
    now = time.time()
    hit = _cache.get(path)
    if hit and now - hit[0] < ttl:
        return hit[1]

    errors: list[str] = []
    for base in bases():
        try:
            r = cr.get(base + path, headers=headers_for(base), impersonate="chrome131", timeout=30)
        except Exception as e:  # network/TLS failure — try the next host
            errors.append(f"{base}: {str(e)[:80]}")
            continue
        if r.status_code != 200:
            errors.append(f"{base}: HTTP {r.status_code} {r.text[:80]!r}")
            continue
        try:
            data = _decode(r.content)
        except Exception as e:
            errors.append(f"{base}: decode {str(e)[:60]}")
            continue
        ACTIVE["base"] = base
        _cache[path] = (now, data)
        return data

    raise RuntimeError("miruro unreachable — " + " | ".join(errors[:3]))


def stream_probe(url: str, referer: str) -> tuple[bool, int]:
    try:
        r = cr.get(
            url,
            headers={"referer": referer, "user-agent": UA, "origin": referer.rstrip("/")},
            impersonate="chrome131",
            timeout=20,
        )
        return (r.status_code == 200 and "#EXTM3U" in r.text), r.status_code
    except Exception:
        return False, 0


def fetch_stream(url: str, referer: str):
    return cr.get(
        url,
        headers={"referer": referer, "user-agent": UA, "origin": referer.rstrip("/")},
        impersonate="chrome131",
        timeout=30,
    )
