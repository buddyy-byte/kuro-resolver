"""kuro-breaker — the Render-hosted side of the stack.

Runs a tiny Flask app that exposes the extractors over HTTP:

    GET /health                        → liveness probe (cron-job.org pings this)
    GET /api/play?title=&ep=&audio=    → provider-direct playback resolution
    GET /api/probe_tls                 → CF reachability probe (diagnosis)
    GET /api/ap_debug?host=...&q=...   → raw animepahe search dump (diagnosis)

Everything lives on a single Flask app so Render's port-binding is unambiguous.
The extractor modules themselves (extractors/animepahe.py, extractors/kwik.py)
are the ones that actually do work; this file is just a thin HTTP shell.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from urllib.parse import quote

from flask import Flask, Response, request

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from extractors import animepahe, kwik  # noqa: E402

app = Flask(__name__)


def _json(payload: dict, status: int = 200) -> Response:
    body = json.dumps(payload, indent=2, default=str)
    return Response(
        body,
        status=status,
        mimetype="application/json",
        headers={
            "access-control-allow-origin": "*",
            "cache-control": "no-store",
        },
    )


@app.get("/")
@app.get("/health")
def health() -> Response:
    return _json({
        "ok": True,
        "service": "kuro-breaker",
        "region": os.environ.get("RENDER_REGION") or "unknown",
        "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7] or None,
    })


@app.get("/api/play")
def play() -> Response:
    title = (request.args.get("title") or "").strip()
    ep_raw = (request.args.get("ep") or "").strip()
    audio = (request.args.get("audio") or "jpn").strip().lower()

    if not title or not ep_raw:
        return _json({"error": "missing_params", "want": ["title", "ep"]}, 400)
    try:
        episode = float(ep_raw)
    except ValueError:
        return _json({"error": "ep_not_numeric", "ep": ep_raw}, 400)

    t0 = time.time()
    try:
        hits = animepahe.search(title)
    except Exception as e:
        return _json({"error": "search_crashed", "exception": f"{type(e).__name__}: {str(e)[:200]}"}, 502)
    if not hits:
        return _json({"error": "no_search_results", "title": title}, 404)

    picked = hits[0]
    anime_session = picked.get("session")
    if not anime_session:
        return _json({"error": "no_session", "search_top": picked}, 502)

    try:
        ep = animepahe.find_episode(anime_session, episode)
    except Exception as e:
        return _json({"error": "episodes_crashed", "exception": f"{type(e).__name__}: {str(e)[:200]}"}, 502)
    if not ep:
        return _json({"error": "episode_not_found", "anime": picked.get("title"), "ep": episode}, 404)

    try:
        sources = animepahe.play_sources(anime_session, ep.get("session"))
    except Exception as e:
        return _json({"error": "sources_crashed", "exception": f"{type(e).__name__}: {str(e)[:200]}"}, 502)
    if not sources:
        return _json({"error": "no_embeds", "anime": picked.get("title"), "ep": episode}, 404)

    sources.sort(key=lambda s: (s["audio"] != audio, -s["quality"]))
    winner = sources[0]

    try:
        extracted = kwik.extract(winner["embed"])
    except Exception as e:
        return _json({
            "error": "extract_crashed",
            "exception": f"{type(e).__name__}: {str(e)[:200]}",
            "embed": winner["embed"],
        }, 502)
    if not extracted:
        return _json({
            "error": "extract_failed",
            "embed": winner["embed"],
            "candidates": sources,
        }, 404)

    m3u8, referer = extracted
    return _json({
        "provider": "animepahe",
        "anime": picked.get("title"),
        "anime_session": anime_session,
        "episode": episode,
        "episode_session": ep.get("session"),
        "url": m3u8,
        "referer": referer,
        "quality": winner["quality"],
        "audio": winner["audio"],
        "fansub": winner.get("fansub"),
        "candidates": sources,
        "elapsed_ms": int((time.time() - t0) * 1000),
    })


# ---------------------------------------------------------------------------
# Diagnostic endpoints — kept here so we can tell at a glance whether Render's
# IP is clean against the CF-walled targets we care about.
# ---------------------------------------------------------------------------

from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

from curl_cffi import requests as cr  # noqa: E402


_PROBE_TARGETS = [
    ("anilist_graphql",  "POST", "https://graphql.anilist.co", {"content-type": "application/json"}),
    ("animepahe_api",    "GET",  "https://animepahe.pw/api?m=search&q=one+piece", {"referer": "https://animepahe.pw/", "x-requested-with": "XMLHttpRequest", "cookie": "__ddg2_=1;"}),
    ("animepahe_home",   "GET",  "https://animepahe.pw/", {}),
    ("miruro_home",      "GET",  "https://www.miruro.to/", {}),
    ("hianime_home",     "GET",  "https://hianime.to/", {}),
    ("hianime_search",   "GET",  "https://hianime.to/search?keyword=one+piece", {"referer": "https://hianime.to/"}),
]


def _probe_one(label: str, method: str, url: str, extra: dict) -> dict:
    started = time.time()
    result: dict = {"label": label, "url": url}
    headers = {"accept": "*/*", "accept-language": "en-US,en;q=0.9", **extra}
    data = None
    if method == "POST" and "anilist" in url:
        data = json.dumps({"query": "{ Viewer { id } }"}).encode()
    try:
        r = cr.request(
            method, url,
            headers=headers, data=data,
            impersonate=animepahe.impersonate(),
            timeout=6, allow_redirects=True,
        )
        body = r.content[:512] if r.content else b""
        result.update({
            "status": r.status_code,
            "server": r.headers.get("server"),
            "cf_ray": r.headers.get("cf-ray"),
            "cf_mitigated": r.headers.get("cf-mitigated"),
            "content_type": r.headers.get("content-type"),
            "body_sample": body[:256].decode("utf-8", "replace"),
            "looks_like_cf_challenge": (
                b"Just a moment" in r.content
                or b"cdn-cgi/challenge-platform" in r.content
                or b"Attention Required" in r.content
            ) if r.content else False,
        })
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


@app.get("/api/probe_tls")
def probe_tls() -> Response:
    started = time.time()
    results = [None] * len(_PROBE_TARGETS)
    with ThreadPoolExecutor(max_workers=len(_PROBE_TARGETS)) as pool:
        futs = {pool.submit(_probe_one, *t): i for i, t in enumerate(_PROBE_TARGETS)}
        for f in as_completed(futs, timeout=10):
            try:
                results[futs[f]] = f.result()
            except Exception as e:
                i = futs[f]
                results[i] = {"label": _PROBE_TARGETS[i][0], "error": str(e)[:160]}
    for i, r in enumerate(results):
        if r is None:
            results[i] = {"label": _PROBE_TARGETS[i][0], "error": "timeout_in_pool"}
    return _json({
        "region": os.environ.get("RENDER_REGION") or "unknown",
        "impersonate": animepahe.impersonate(),
        "total_ms": int((time.time() - started) * 1000),
        "results": results,
    })


@app.get("/api/ap_debug")
def ap_debug() -> Response:
    host = request.args.get("host") or "animepahe.pw"
    q = request.args.get("q") or "one piece"
    url = f"https://{host}/api?m=search&q={quote(q)}"
    headers = dict(animepahe._HEADERS)  # type: ignore[attr-defined]
    headers["referer"] = f"https://{host}/"
    try:
        r = cr.get(url, headers=headers, impersonate=animepahe.impersonate(), timeout=10, allow_redirects=True)
        body = r.text or ""
        info = {
            "url": url,
            "status": r.status_code,
            "response_headers": dict(r.headers),
            "content_type": r.headers.get("content-type"),
            "body_len": len(body),
            "body_sample": body[:800],
            "looks_like_html": body.lstrip().startswith("<"),
        }
        try:
            info["parsed_json"] = r.json()
        except Exception as e:
            info["json_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    except Exception as e:
        info = {"url": url, "fetch_error": f"{type(e).__name__}: {str(e)[:200]}", "trace": traceback.format_exc()[-400:]}
    return _json(info)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
