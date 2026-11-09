"""TLS-impersonation probe.

Same target list as probe.py but every request is fired through curl_cffi with
Chrome 131 fingerprint impersonation. Compares status codes directly: if a host
was timing out or 403'ing with plain urllib and now returns 200, the gate was
JA3/JA4-based and TLS impersonation alone defeats it. If it still 403s with
"cf-mitigated: challenge", the IP reputation layer is what's actually blocking
us and only a non-datacenter exit will help.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler

from curl_cffi import requests as cr


TARGETS: list[tuple[str, str, str, dict[str, str]]] = [
    ("anilist_graphql",   "POST", "https://graphql.anilist.co", {"content-type": "application/json"}),
    ("miruro_api",        "GET",  "https://www.miruro.to/api/v1/anime?limit=15&sort=-popularity", {"referer": "https://www.miruro.to/", "origin": "https://www.miruro.to"}),
    ("miruro_home",       "GET",  "https://www.miruro.to/", {}),
    ("hianime_home",      "GET",  "https://hianime.to/", {}),
    ("hianime_search",    "GET",  "https://hianime.to/search?keyword=one+piece", {"referer": "https://hianime.to/"}),
    ("hianime_ajax",      "GET",  "https://hianime.to/ajax/v2/episode/servers?episodeId=1", {"referer": "https://hianime.to/", "x-requested-with": "XMLHttpRequest"}),
    ("animepahe_api",     "GET",  "https://animepahe.ru/api?m=search&q=one+piece", {}),
    ("aniwave_home",      "GET",  "https://aniwave.to/", {}),
]

PER_REQUEST_TIMEOUT = 6.0
IMPERSONATE = "chrome131"


def probe_one(label: str, method: str, url: str, extra: dict[str, str]) -> dict:
    started = time.time()
    result: dict = {"label": label, "url": url}

    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        **extra,
    }
    data = None
    if method == "POST" and "anilist" in url:
        data = json.dumps({"query": "{ Viewer { id } }"}).encode()

    try:
        r = cr.request(
            method,
            url,
            headers=headers,
            data=data,
            impersonate=IMPERSONATE,
            timeout=PER_REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        body = r.content[:512] if r.content else b""
        result.update({
            "status": r.status_code,
            "server": r.headers.get("server"),
            "cf_ray": r.headers.get("cf-ray"),
            "cf_mitigated": r.headers.get("cf-mitigated"),
            "content_type": r.headers.get("content-type"),
            "body_sample": body[:256].decode("utf-8", "replace"),
            "body_len": len(r.content) if r.content else 0,
            "looks_like_cf_challenge": (
                b"challenge-platform" in r.content
                or b"cf_chl_opt" in r.content
                or b"Just a moment" in r.content
                or b"cdn-cgi/challenge-platform" in r.content
            ) if r.content else False,
        })
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:160]}"

    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def run_all(targets):
    out = [None] * len(targets)
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futs = {pool.submit(probe_one, *t): i for i, t in enumerate(targets)}
        for f in as_completed(futs, timeout=PER_REQUEST_TIMEOUT + 3):
            try:
                out[futs[f]] = f.result()
            except Exception as e:
                i = futs[f]
                out[i] = {"label": targets[i][0], "url": targets[i][2], "error": f"future: {e}"}
    for i, r in enumerate(out):
        if r is None:
            out[i] = {"label": targets[i][0], "url": targets[i][2], "error": "timeout_in_pool"}
    return out


class handler(BaseHTTPRequestHandler):  # noqa: N801
    def do_GET(self):  # noqa: N802
        wanted = None
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
            for pair in qs.split("&"):
                if pair.startswith("target="):
                    wanted = pair.split("=", 1)[1]
                    break

        targets = TARGETS
        if wanted:
            targets = [t for t in TARGETS if t[0] == wanted] or TARGETS

        started = time.time()
        results = run_all(targets)

        out = {
            "region": os.environ.get("VERCEL_REGION") or os.environ.get("AWS_REGION"),
            "impersonate": IMPERSONATE,
            "per_request_timeout_s": PER_REQUEST_TIMEOUT,
            "total_ms": int((time.time() - started) * 1000),
            "results": results,
        }
        body = json.dumps(out, indent=2).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
