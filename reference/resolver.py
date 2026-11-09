"""KURO API — catalog + stream resolver + HLS proxy over Miruro's v1 API.

Run:  python -m uvicorn main:app --host 127.0.0.1 --port 8081
"""
from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import quote, urljoin

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

import anilist
import miruro
from curl_cffi import requests as cr  # noqa: F401  (used by the AniList entity search)

app = FastAPI(title="KURO API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

POOL = ThreadPoolExecutor(max_workers=24)
# upstream only accepts these sort keys
SORTS = {"-popularity", "-score", "-updated_at", "-trending"}


def q(**kw) -> str:
    parts = []
    for k, v in kw.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={quote(str(v), safe=',_-')}")
    return "&".join(parts)


def list_path(
    q_text=None,
    genres=None,
    tags=None,
    sort="-popularity",
    fmt=None,
    status=None,
    country=None,
    season=None,
    year=None,
    source=None,
    limit=15,
    cursor=None,
    score_gte=None,
    adult=None,
):
    return "/api/v1/anime?" + q(
        q=q_text,
        limit=limit,
        cursor=cursor,
        genre_all=genres,
        tag_all=tags,
        sort=sort if sort in SORTS else "-popularity",
        format=fmt,
        status=status,
        country_of_origin=country,
        season=season,
        season_year=year,
        source=source,
        score_gte=score_gte,
        is_adult=adult,
    )


def safe_list(**kw):
    """Upstream whitelists exact query shapes; fall back to looser shapes on 400."""
    ladder = [
        kw,
        {**kw, "limit": 15},
        {**kw, "limit": 15, "fmt": None},
        {**kw, "limit": 15, "fmt": None, "status": None},
        {k: v for k, v in {**kw, "limit": 15, "fmt": None, "status": None}.items() if k in ("q_text", "sort", "limit", "cursor", "genres")},
    ]
    last = None
    for attempt in ladder:
        try:
            return miruro.api(list_path(**attempt), 180)
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def slim(item: dict) -> dict:
    """Normalize a catalog row for the frontend."""
    t = item.get("title") or {}
    ids = item.get("external_ids") or {}
    al = (ids.get("anilist") or [None])[0]
    return {
        "id": item.get("id"),
        "anilist": al,
        "mal": (ids.get("mal") or [None])[0],
        "title": t.get("english") or t.get("romaji") or "Untitled",
        "romaji": t.get("romaji"),
        "native": t.get("native"),
        "cover": item.get("cover_url"),
        "banner": item.get("banner_url") or item.get("background_url"),
        "background": item.get("background_url"),
        "logo": item.get("logo_url"),
        "color": item.get("cover_color"),
        "format": item.get("format"),
        "status": item.get("status"),
        "season": item.get("season"),
        "year": item.get("season_year"),
        "score": item.get("average_score"),
        "popularity": item.get("popularity"),
        "episodes": item.get("episode_count"),
        "counts": item.get("episode_counts"),
        "duration": item.get("episode_duration_minutes"),
        "source": item.get("source"),
        "description": item.get("description"),
        "adult": item.get("is_adult"),
        "next_air": item.get("next_airing"),
        "aired_season": item.get("aired_season"),
    }


def _run(fn, *a, **k):
    return asyncio.get_event_loop().run_in_executor(POOL, lambda: fn(*a, **k))


@app.get("/health")
async def health():
    return {"ok": True, "upstream": miruro.BASE}


@app.get("/api/home")
async def home():
    import datetime as _dt

    today = _dt.date.today()
    season = ("WINTER", "SPRING", "SUMMER", "FALL")[(today.month - 1) // 3]
    jobs = {
        "trending": list_path(status="RELEASING", sort="-popularity", limit=15),
        "seasonal": list_path(season=season, year=today.year, sort="-popularity", limit=15),
        "popular": list_path(sort="-popularity", limit=15),
        "top_rated": list_path(sort="-score", limit=15),
        "upcoming": list_path(status="NOT_YET_RELEASED", sort="-popularity", limit=15),
        "just_finished": list_path(status="FINISHED", sort="-popularity", limit=15),
        "top_movies": list_path(fmt="MOVIE", sort="-score", limit=15),
    }
    out = {}
    futs = {k: _run(miruro.api, p, 600) for k, p in jobs.items()}
    for k, f in futs.items():
        try:
            out[k] = [slim(x) for x in (await f).get("data", [])]
        except Exception:
            out[k] = []
    # spotlight = top trending with banners
    out["spotlight"] = [x for x in out["trending"] if x.get("banner")][:6] or out["trending"][:6]
    out["season_label"] = f"{season.title()} {today.year}"
    return JSONResponse(out, headers={"cache-control": "public, max-age=300"})


@app.get("/api/anime")
async def browse(
    q: str | None = None,
    genre: str | None = None,
    tag: str | None = None,
    year: int | None = None,
    season: str | None = None,
    format: str | None = None,
    status: str | None = None,
    sort: str = "-popularity",
    cursor: str | None = None,
    limit: int = Query(24, ge=1, le=50),
):
    try:
        d = await _run(
            safe_list,
            q_text=q, genres=genre, tags=tag, year=year, season=season,
            fmt=format, status=status, sort=sort, cursor=cursor, limit=15,
        )
    except Exception as e:
        raise HTTPException(502, str(e)[:200])
    return {
        "items": [slim(x) for x in d.get("data", [])],
        "cursor": d.get("next_cursor"),
        "has_more": d.get("has_more", False),
    }


@app.get("/api/random")
async def random_anime():
    d = await _run(miruro.api, list_path(status="RELEASING", sort="-popularity", limit=15), 60)
    items = [slim(x) for x in d.get("data", [])]
    import random

    return random.choice(items) if items else None


@app.get("/api/anime/{anime_id}")
async def anime(anime_id: str):
    try:
        show = await _run(miruro.api, f"/api/v1/anime/{anime_id}/show", 600)
    except Exception as e:
        raise HTTPException(404, str(e)[:200])
    rows = show.get("data") or []
    if not rows:
        raise HTTPException(404, "not found")
    main = slim(rows[0])
    main["clusters"] = [slim(r) for r in rows[1:]]

    meta = {}
    if main.get("anilist"):
        meta = await _run(anilist.media, int(main["anilist"]))
    main["genres"] = meta.get("genres") or []
    main["tags"] = [t for t in (meta.get("tags") or []) if not t.get("isMediaSpoiler")][:12]
    main["studios"] = [s["name"] for s in ((meta.get("studios") or {}).get("nodes") or []) if s.get("isAnimationStudio")]
    main["trailer"] = meta.get("trailer")
    main["characters"] = meta.get("characters", {}).get("edges", [])[:12]
    main["staff"] = (meta.get("staff") or {}).get("nodes", [])[:8]
    main["rankings"] = meta.get("rankings") or []

    async def grab(path):
        try:
            return await _run(miruro.api, path, 900)
        except Exception:
            return {"data": []}

    rel, rec = await asyncio.gather(
        grab(f"/api/v1/anime/{anime_id}/relations"),
        grab(f"/api/v1/anime/{anime_id}/recommendations"),
    )
    rels = []
    for r in rel.get("data", []):
        a = r.get("anime") or r
        s = slim(a)
        s["relation"] = r.get("relation_type") or r.get("relation")
        rels.append(s)
    main["relations"] = rels[:14]
    main["recommendations"] = [slim(r.get("anime") or r) for r in rec.get("data", [])][:18]
    return JSONResponse(main, headers={"cache-control": "public, max-age=600"})


@app.get("/api/anime/{anime_id}/episodes")
async def episodes(anime_id: str):
    try:
        d = await _run(miruro.api, f"/api/v1/anime/{anime_id}/episodes?limit=10000", 300)
    except Exception as e:
        raise HTTPException(502, str(e)[:200])
    return {"items": d.get("data", []), "has_more": d.get("has_more", False)}


def resolve_play(anime_id: str, ep: int, track: str | None):
    """Health-check providers in parallel and return the first playable source."""
    # upstream rejects ?track=, so always take the full payload and order it here
    url = f"/api/v1/anime/{anime_id}/episodes/{ep}/play"
    payload = miruro.api(url, 300)
    tracks = payload.get("tracks") or []
    if track and track != "auto":
        ordered = [t for t in tracks if t.get("track") == track] + [t for t in tracks if t.get("track") != track]
    else:
        pref = {"sub": 0, "ssub": 1, "dub": 2}
        ordered = sorted(tracks, key=lambda t: pref.get(t.get("track"), 9))

    candidates = []
    for t in ordered:
        for p in t.get("providers", []):
            for s in p.get("servers", []):
                ref = (s.get("headers") or {}).get("Referer") or (s.get("headers") or {}).get("referer") or ""
                for st in s.get("streams", []):
                    if st.get("format") and st["format"] != "hls":
                        continue
                    candidates.append((t, p, s, st, ref))

    def check(c):
        t, p, s, st, ref = c
        ok, code = miruro.stream_probe(st["url"], ref)
        return ok, code

    # Probe every candidate in parallel, then pick the best healthy one by
    # track preference (explicit request wins, else sub > ssub > dub).
    pref = {"sub": 0, "ssub": 1, "dub": 2}
    if track and track != "auto":
        pref = {track: 0}
        for k, v in (("sub", 1), ("ssub", 2), ("dub", 3)):
            pref.setdefault(k, v)
    futs = {POOL.submit(check, c): i for i, c in enumerate(candidates)}
    ok_idx: set[int] = set()
    health = []
    done, pending = wait(list(futs), timeout=14)
    for f in done:
        i = futs[f]
        try:
            ok, code = f.result()
        except Exception:
            ok, code = False, 0
        c = candidates[i]
        health.append({"track": c[0]["track"], "provider": c[1].get("provider"), "server": c[2].get("server"), "ok": ok, "code": code})
        if ok:
            ok_idx.add(i)
    for f in pending:
        f.cancel()

    winner = None
    if ok_idx:
        best = min(ok_idx, key=lambda i: (pref.get(candidates[i][0]["track"], 9), i))
        winner = candidates[best]
    if not winner and candidates:
        winner = candidates[0]

    if not winner:
        raise HTTPException(404, "no sources for this episode")

    t, p, s, st, ref = winner
    subs = []
    for sub in (p.get("subtitles") or []):
        subs.append({"label": sub.get("label"), "lang": sub.get("language"), "url": sub.get("file"), "default": sub.get("default")})
    return {
        "episode": payload.get("episode_number"),
        "track": t.get("track"),
        "provider": p.get("provider"),
        "server": s.get("server"),
        "url": st["url"],
        "referer": ref,
        "subtitles": subs,
        "skip_times": payload.get("skip_times") or [],
        "thumbnails": (p.get("thumbnails") or {}).get("vtt"),
        "alternatives": [
            {"track": c[0]["track"], "provider": c[1].get("provider"), "server": c[2].get("server"), "url": c[3]["url"], "referer": c[4]}
            for c in candidates[:12]
        ],
        "health": health,
    }


@app.get("/api/anime/{anime_id}/play/{ep}")
async def play(anime_id: str, ep: int, track: str = "auto"):
    try:
        return await _run(resolve_play, anime_id, ep, track)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e)[:200])


URI_RE = re.compile(r'URI="([^"]+)"')


@app.get("/api/hls")
async def hls(u: str, r: str = "", p: str = ""):
    """Proxy an HLS playlist/segment, rewriting child URLs through this endpoint.

    `p` is the public path prefix of this service as seen by the browser
    (e.g. /api/kuro when running behind the Next.js proxy).
    """
    if not u.startswith("http"):
        raise HTTPException(400, "bad url")
    res = await _run(miruro.fetch_stream, u, r)
    if res.status_code != 200:
        raise HTTPException(res.status_code, f"upstream {res.status_code}")
    body = res.content

    if b"#EXTM3U" in body[:200]:
        text = body.decode("utf-8", "replace")

        def proxy(child: str) -> str:
            absu = urljoin(u, child)
            return f"{p}/api/hls?u={quote(absu, safe='')}&r={quote(r, safe='')}&p={quote(p, safe='')}"

        lines = []
        for line in text.splitlines():
            if line.startswith("#"):
                line = URI_RE.sub(lambda m: f'URI="{proxy(m.group(1))}"', line)
                lines.append(line)
            elif line.strip():
                lines.append(proxy(line.strip()))
            else:
                lines.append(line)
        return Response(
            "\n".join(lines),
            media_type="application/vnd.apple.mpegurl",
            headers={"cache-control": "no-store", "access-control-allow-origin": "*"},
        )

    # Upstream CDNs lie about segment types (kickassanime serves .ts as image/jpeg).
    # Sniff the payload and tell the player the truth.
    if body[:1] == b"\x47":
        ctype = "video/mp2t"
    elif body[4:8] == b"ftyp":
        ctype = "video/mp4"
    elif body[:3] == b"ID3" or body[:2] == b"\xff\xf1":
        ctype = "audio/aac"
    else:
        ctype = res.headers.get("content-type") or "application/octet-stream"
    return Response(
        body,
        media_type=ctype,
        headers={"cache-control": "public, max-age=3600", "access-control-allow-origin": "*"},
    )


@app.get("/api/sub")
async def subtitle(u: str, r: str = ""):
    res = await _run(miruro.fetch_stream, u, r)
    if res.status_code != 200:
        raise HTTPException(res.status_code, "subtitle unavailable")
    text = res.content.decode("utf-8", "replace")
    if "-->" in text and not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text
    return Response(text, media_type="text/vtt", headers={"cache-control": "public, max-age=3600", "access-control-allow-origin": "*"})


@app.get("/api/schedule")
async def schedule(start: str, end: str):
    try:
        d = await _run(miruro.api, f"/api/v1/schedule?from={start}&to={end}&track=raw%2Csub&limit=10000", 900)
    except Exception as e:
        raise HTTPException(502, str(e)[:200])
    return {"items": d.get("data", [])}


def _week_bounds():
    import datetime as dt

    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    return monday, monday + dt.timedelta(days=6)


@app.get("/api/weekly")
async def weekly(limit: int = 24):
    """This week's airing schedule: what dropped, what's next.

    Feeds the featured rail and the "new this week" row. Entries are grouped by
    show so one title appears once, carrying its newest aired episode.
    """
    import datetime as dt

    start, end = _week_bounds()
    try:
        d = await _run(
            miruro.api,
            f"/api/v1/schedule?from={start.isoformat()}&to={end.isoformat()}&track=raw%2Csub&limit=10000",
            600,
        )
    except Exception as e:
        raise HTTPException(502, str(e)[:200])

    rows = d.get("data") or []
    now = dt.datetime.now(dt.timezone.utc)

    # schedule rows are flat: {anime_id, air_at, episode_number, track}
    newest: dict[str, dict] = {}
    upcoming: dict[str, dict] = {}
    for r in rows:
        sid = r.get("anime_id")
        ep = r.get("episode_number")
        when_raw = r.get("air_at")
        if not sid or ep is None or not when_raw:
            continue
        try:
            when = dt.datetime.fromisoformat(str(when_raw).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        hours = round((now - when).total_seconds() / 3600, 1)
        if hours < 0:
            # not aired yet — this is the countdown material
            prev_up = upcoming.get(sid)
            if prev_up is None or -hours < -prev_up["in_hours"]:
                upcoming[sid] = {"episode": ep, "in_hours": -hours}
            continue
        prev = newest.get(sid)
        if prev is None or ep > prev["episode"]:
            newest[sid] = {"episode": ep, "hours_ago": hours, "track": r.get("track")}

    if not newest:
        return {"window": {"from": start.isoformat(), "to": end.isoformat()}, "items": [], "next": []}

    ids = list(newest.keys())
    shows: dict[str, dict] = {}
    debug: list[str] = []
    for chunk_start in range(0, len(ids), 60):
        chunk = ids[chunk_start : chunk_start + 60]
        try:
            bulk = await _run(miruro.api, "/api/v1/anime?id_in=" + ",".join(chunk) + "&limit=200", 600)
        except Exception as e:
            debug.append(f"chunk@{chunk_start} n={len(chunk)} :: {str(e)[:90]}")
            continue
        for item in bulk.get("data") or []:
            shows[item.get("id")] = slim(item)

    out = []
    for sid, info in newest.items():
        show = shows.get(sid)
        if not show or not show.get("cover"):
            continue
        out.append({**show, "episode": info["episode"], "hours_ago": info["hours_ago"], "track": info["track"]})

    out.sort(key=lambda x: x.get("hours_ago") if x.get("hours_ago") is not None else 9999)

    # hydrate the not-yet-aired ones so the hero can run a real countdown
    next_ids = [i for i in upcoming.keys() if i in shows] or list(upcoming.keys())[:20]
    for chunk_start in range(0, len(next_ids), 60):
        chunk = next_ids[chunk_start : chunk_start + 60]
        missing = [i for i in chunk if i not in shows]
        if not missing:
            continue
        try:
            bulk = await _run(miruro.api, "/api/v1/anime?id_in=" + ",".join(missing) + "&limit=200", 600)
        except Exception:
            continue
        for item in bulk.get("data") or []:
            shows[item.get("id")] = slim(item)

    nxt = []
    for sid, info in upcoming.items():
        show = shows.get(sid)
        if not show or not show.get("cover"):
            continue
        nxt.append({**show, "episode": info["episode"], "in_hours": info["in_hours"]})
    nxt.sort(key=lambda x: x.get("in_hours") if x.get("in_hours") is not None else 9999)

    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "candidates": len(newest),
        "items": out[:limit],
        "next": nxt[:limit],
    }


@app.get("/api/resolve")
async def resolve(anilist: int, title: str | None = None):
    """Map an AniList id to a Miruro entry by title search + id match (cached).

    Used when importing an AniList list: their ids are not ours.
    """
    key = f"resolve:{anilist}"
    hit = miruro._cache.get(key)
    if hit and __import__("time").time() - hit[0] < 86400:
        return hit[1]

    candidates: list[dict] = []
    if title:
        try:
            d = await _run(miruro.api, list_path(q_text=title, sort="-popularity", limit=15), 3600)
            candidates = d.get("data") or []
        except Exception:
            candidates = []

    match = None
    for c in candidates:
        ids = c.get("external_ids") or {}
        if str(anilist) in [str(x) for x in (ids.get("anilist") or [])]:
            match = c
            break
    if match is None and candidates:
        match = candidates[0]

    result = {"anilist": anilist, "matched": bool(match and str(anilist) in [str(x) for x in (((match.get("external_ids") or {}).get("anilist")) or [])])}
    if match:
        result["anime"] = slim(match)
    miruro._cache[key] = (__import__("time").time(), result)
    return result


