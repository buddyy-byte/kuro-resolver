"""Debug probe for animepahe — dump the raw response from /api?m=search so we can
see exactly what we're getting (HTML challenge? empty JSON? parse error?).
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from curl_cffi import requests as cr  # noqa: E402

from extractors import animepahe  # noqa: E402


class handler(BaseHTTPRequestHandler):  # noqa: N801
    def do_GET(self):  # noqa: N802
        qs = {}
        if "?" in self.path:
            qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        q = qs.get("q", "one piece")
        impersonate = qs.get("impersonate") or animepahe.impersonate()
        host = qs.get("host") or "animepahe.pw"

        url = f"https://{host}/api?m=search&q={quote(q)}"
        headers = dict(animepahe._HEADERS)  # type: ignore[attr-defined]
        headers["referer"] = f"https://{host}/"

        try:
            r = cr.get(url, headers=headers, impersonate=impersonate, timeout=10, allow_redirects=True)
            body = r.text or ""
            info = {
                "url": url,
                "impersonate": impersonate,
                "request_headers": headers,
                "status": r.status_code,
                "response_headers": dict(r.headers),
                "body_len": len(body),
                "body_sample": body[:800],
                "looks_like_html": body.lstrip().startswith("<"),
                "content_type": r.headers.get("content-type"),
            }
            try:
                info["parsed_json"] = r.json()
            except Exception as e:
                info["json_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        except Exception as e:
            info = {"url": url, "fetch_error": f"{type(e).__name__}: {str(e)[:200]}"}

        body = json.dumps(info, indent=2, default=str).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("access-control-allow-origin", "*")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
