"""AniList enrichment (public GraphQL) — genres, characters, staff, trailer, tags."""
from __future__ import annotations

import time

from curl_cffi import requests as cr

ENDPOINT = "https://graphql.anilist.co"
_cache: dict[int, tuple[float, dict]] = {}

QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id idMal
    genres
    tags { name rank isMediaSpoiler }
    studios { nodes { name isAnimationStudio } }
    trailer { id site thumbnail }
    isAdult
    rankings { rank type context year season allTime }
    externalLinks { site url }
    characters(perPage: 14, sort: [ROLE, RELEVANCE]) {
      edges { role node { id name { full } image { large } } voiceActors(language: JAPANESE) { name { full } image { large } } }
    }
    staff(perPage: 8) { nodes { id name { full } image { large } primaryOccupations } }
    streamingEpisodes { title thumbnail }
  }
}
"""


def media(anilist_id: int) -> dict:
    now = time.time()
    hit = _cache.get(anilist_id)
    if hit and now - hit[0] < 3600:
        return hit[1]
    try:
        r = cr.post(
            ENDPOINT,
            json={"query": QUERY, "variables": {"id": int(anilist_id)}},
            headers={"content-type": "application/json", "accept": "application/json"},
            impersonate="chrome131",
            timeout=25,
        )
        data = (r.json() or {}).get("data", {}).get("Media") or {}
    except Exception:
        data = {}
    _cache[anilist_id] = (now, data)
    return data
