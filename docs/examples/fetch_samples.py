#!/usr/bin/env python3
"""Fetch the real, trimmed API responses used in docs/api.md from a running deployment (read-only GET requests).

    BASE=https://graph-series.katzer.ru/api/backend python docs/examples/fetch_samples.py

The base URL is the public site's same-origin API path. A pause between requests keeps the load negligible.
Long text fields are shortened and long lists are cut, with an explicit marker, so the files stay small.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BASE", "https://graph-series.katzer.ru/api/backend").rstrip("/")
OUT = Path(__file__).resolve().parent


def get(path: str):
    req = urllib.request.Request(BASE + path, headers={"accept": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body, status = r.read().decode(), r.status
    except urllib.error.HTTPError as e:
        body, status = e.read().decode(), e.code
    time.sleep(2)
    return status, body, round(time.time() - t0, 1)


def cut(text, n=110):
    return text if not isinstance(text, str) or len(text) <= n else text[:n] + "…"


def trim(obj, lists=3):
    """Shorten long strings and lists (keeping a marker with the number of omitted items)."""
    if isinstance(obj, dict):
        return {k: (cut(v) if k in ("overview", "tagline") else trim(v, lists)) for k, v in obj.items()}
    if isinstance(obj, list):
        out = [trim(x, lists) for x in obj[:lists]]
        if len(obj) > lists:
            out.append({"…": f"{len(obj) - lists} more"})
        return out
    return obj


def save(name, path, lists=3, raw=False):
    status, body, secs = get(path)
    if raw:
        text = "\n".join(l[:200] for l in body.splitlines())
        (OUT / name).write_text(f"# GET {path}\n# HTTP {status}\n{text}\n")
    else:
        data = json.loads(body)
        (OUT / name).write_text(json.dumps({"_request": f"GET {path}", "_http": status, "response": trim(data, lists)},
                                           indent=2, ensure_ascii=False) + "\n")
    print(f"{status} {secs:>5}s  {path}")
    return status, body


save("health.json", "/health")
save("health-full.json", "/health/full")
save("series-1396.json", "/api/series/1396", lists=4)
save("series-by-name.json", "/api/series?name=breaking%20ba&limit=2")
save("search-semantic.json", "/api/search?q=space%20western&mode=semantic&limit=3")
save("search-hybrid.json", "/api/search?q=breaking%20bad&mode=hybrid&limit=3")
save("search-structural.json", "/api/search?q=breaking&mode=structural&limit=3")
save("search-persons.json", "/api/search/persons?q=cranston&limit=3")
save("search-stream.txt", "/api/search/stream?q=space%20western&limit=2", raw=True)
save("similar-hybrid.json", "/api/series/1396/similar?mode=hybrid", lists=4)
save("graph-series-1396-filtered.json", "/api/graph/series/1396?depth=1&include_types=HAS_GENRE,PRODUCED_IN", lists=5)
st, body = save("graph-series-1396-depth1.json", "/api/graph/series/1396?depth=1", lists=3)
g = json.loads(body)
(OUT / "graph-series-1396-depth1.counts.json").write_text(json.dumps({
    "nodes": len(g["nodes"]), "edges": len(g["edges"]),
    "by_label": {l: sum(1 for n in g["nodes"] if n["label"] == l) for l in sorted({n["label"] for n in g["nodes"]})},
    "by_edge_type": {t: sum(1 for e in g["edges"] if e["type"] == t) for t in sorted({e["type"] for e in g["edges"]})},
}, indent=2) + "\n")
save("graph-hub-genre-18.json", "/api/graph/node/genre/18?limit=3")
save("graph-shared-cast.json", "/api/graph/series/1396/shared-cast?limit=3")
save("graph-intersection.json", "/api/graph/intersection?series_a=1396&series_b=60059", lists=5)
save("person-17419.json", "/api/person/17419")
save("person-graph.json", "/api/person/17419/graph", lists=3)
errors = []
for p in ["/api/series/99999999", "/api/series/abc", "/api/graph/node/work/1", "/api/graph/node/genre/abc", "/api/search?q=a"]:
    s, b, _ = get(p)
    errors.append({"request": f"GET {p}", "http": s, "body": json.loads(b)})
(OUT / "errors.json").write_text(json.dumps(errors, indent=2, ensure_ascii=False) + "\n")
print("done")
