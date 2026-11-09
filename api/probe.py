"""Vercel edge reachability probe.

Pings every candidate upstream we might depend on if we skip miruro's API.
Returns status codes, server headers, and small body samples so we can tell:

  - Is the host reachable from Vercel's datacenter range at all?
  - Does Cloudflare return a real page or a bot-check (403 + cf-mitigated)?
  - Does the CDN we care about (echovideo) still return #EXTM3U manifests?

This is diagnostic-only. No TLS impersonation, no masking — we want the
naive, no-tricks baseline. If a host works here, it works for a real client.
If it needs chrome131 + residential we'll see 403s and act accordingly.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler


# (label, method, url, extra_headers)
TARGETS: list[tuple[str, str, str, dict[str, str]]] = [
    ("anilist_graphql",   "POST", "https://graphql.anilist.co", {"content-type": "application/json"}),
    ("miruro_api",        "GET",  "https://www.miruro.to/api/v1/anime?limit=15&sort=-popularity", {}),
    ("hianime_home",      "GET",  "https://hianime.to/", {}),
    ("hianime_search",    "GET",  "https://hianime.to/search?keyword=one+piece", {}),
    ("hianime_ajax",      "GET",  "https://hianime.to/ajax/v2/episode/servers?episodeId=1", {}),
    ("animepahe_api",     "GET",  "https://animepahe.ru/api?m=search&q=one+piece", {}),
    ("animepahe_home",    "GET",  "https://animepahe.ru/", {}),
    ("gogoanime_home",    "GET",  "https://anitaku.bz/", {}),
    ("aniwave_home",      "GET",  "https://aniwave.to/", {}),
    ("echovideo_root",    "GET",  "https://echovideo.to/", {}),
    ("tmdb_api",          "GET",  "https://api.themoviedb.org/3/configuration", {}),
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def probe_one(label: str, method: str, url: str, extra: dict[str, str]) -> dict:
    host = url.split("/")[2]
    started = time.time()
    result: dict = {"label": label, "url": url, "host": host}

    # DNS first — tells us if the function even has outbound DNS working
    try:
        result["ip"] = socket.gethostbyname(host)
    except Exception as e:
        result["dns_error"] = str(e)[:140]
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    headers = {
        "user-agent": UA,
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        **extra,
    }
    data = None
    if method == "POST" and "anilist" in url:
        data = json.dumps({"query": "{ Viewer { id } }"}).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(512)
            result.update({
                "status": r.status,
                "server": r.headers.get("server"),
                "cf_ray": r.headers.get("cf-ray"),
                "cf_mitigated": r.headers.get("cf-mitigated"),
                "content_type": r.headers.get("content-type"),
                "body_sample": body[:256].decode("utf-8", "replace"),
                "body_is_m3u8": body[:7] == b"#EXTM3U",
            })
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(512)
        except Exception:
            pass
        result.update({
            "status": e.code,
            "server": e.headers.get("server") if e.headers else None,
            "cf_ray": e.headers.get("cf-ray") if e.headers else None,
            "cf_mitigated": e.headers.get("cf-mitigated") if e.headers else None,
            "body_sample": body[:256].decode("utf-8", "replace"),
        })
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:140]}"

    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


class handler(BaseHTTPRequestHandler):  # noqa: N801  (Vercel expects lowercase)
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
            targets = [t for t in TARGETS if t[0] == wanted]
            if not targets:
                targets = TARGETS  # unknown filter, run all

        started = time.time()
        results = [probe_one(*t) for t in targets]

        out = {
            "region": os.environ.get("VERCEL_REGION") or os.environ.get("AWS_REGION"),
            "deployment": os.environ.get("VERCEL_DEPLOYMENT_ID"),
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
