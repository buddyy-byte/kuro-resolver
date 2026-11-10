"""Playback resolution endpoint — provider-direct, no miruro.

GET /api/play?title=<name>&ep=<n>&audio=jpn|eng

Response (success, 200)::

    {
      "provider": "animepahe",
      "anime": "One Piece",
      "episode": 1,
      "url": "https://<cdn>/.../stream.m3u8",
      "referer": "https://kwik.si/",
      "quality": 1080,
      "audio": "jpn",
      "candidates": [...]   // every discovered embed, for frontend fallback
    }

Response (error, 404/500)::

    { "error": "<reason>", ... }

The frontend keeps proxying playlist + segment fetches through its existing
/api/hls route so there is no CORS or referer surgery in the browser.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Vercel's Python runtime runs handlers from /var/task with the project root on
# sys.path, but local dev and some edge cases drop sibling packages. Add the
# resolver root defensively so ``from extractors import ...`` always resolves.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from extractors import animepahe, kwik  # noqa: E402


def resolve(title: str, episode: float, audio: str = "jpn") -> dict:
    t0 = time.time()
    hits = animepahe.search(title)
    if not hits:
        return {"error": "no_search_results", "title": title}

    # Animepahe's search ranks by relevance; top hit is almost always the right show.
    # (Future: when we wire AniList id in, cross-check external_ids to disambiguate.)
    picked = hits[0]
    anime_session = picked.get("session")
    if not anime_session:
        return {"error": "no_session", "search_top": picked}

    ep = animepahe.find_episode(anime_session, episode)
    if not ep:
        return {
            "error": "episode_not_found",
            "anime": picked.get("title"),
            "ep": episode,
            "available_pages": "walked up to 50 pages",
        }

    sources = animepahe.play_sources(anime_session, ep.get("session"))
    if not sources:
        return {"error": "no_embeds", "anime": picked.get("title"), "ep": episode}

    # Prefer the requested audio track, then the highest resolution available.
    sources.sort(key=lambda s: (s["audio"] != audio, -s["quality"]))
    winner = sources[0]

    extracted = kwik.extract(winner["embed"])
    if not extracted:
        return {
            "error": "extract_failed",
            "embed": winner["embed"],
            "quality": winner["quality"],
            "candidates": sources,
        }

    m3u8, referer = extracted
    return {
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
    }


class handler(BaseHTTPRequestHandler):  # noqa: N801
    def do_GET(self):  # noqa: N802
        qs = {}
        if "?" in self.path:
            qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        title = (qs.get("title") or "").strip()
        episode_raw = (qs.get("ep") or "").strip()
        audio = (qs.get("audio") or "jpn").strip().lower()

        if not title or not episode_raw:
            self._write(400, {"error": "missing_params", "want": ["title", "ep"], "got": dict(qs)})
            return

        try:
            episode = float(episode_raw)
        except ValueError:
            self._write(400, {"error": "ep_not_numeric", "ep": episode_raw})
            return

        try:
            result = resolve(title, episode, audio=audio)
        except Exception as e:
            self._write(500, {
                "error": "resolve_crashed",
                "exception": f"{type(e).__name__}: {str(e)[:200]}",
                "trace": traceback.format_exc()[-800:],
            })
            return

        status = 200 if "url" in result else 404
        self._write(status, result)

    def _write(self, status: int, payload: dict):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002, N802
        # Quiet the default access-log spam; Vercel's own logging is enough.
        return
