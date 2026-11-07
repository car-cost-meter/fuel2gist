#!/usr/bin/env python3
"""Build the OSM -> government station mapping and push it to the gist.

OpenStreetMap contributors tag fuel stations with `ref:FR:prix-carburants`,
the government station id — giving an exact, ambiguity-free link (99.5%
coverage) and GPS coordinates that proved more accurate than the geocoded
positions in the government feed.

Output (gist file `osm-map.json`):
    {"v": 1, "ts": <unix minutes>,
     "brands": ["Avia", "Carrefour", ...],   # ordered, referenced by index
     "map": {"58240003": [bIdx, lat, lon, distM], ...}}
    - bIdx: index into brands (null if the OSM feature has no identity)
    - lat/lon: OSM coordinates of the station feature
    - distM: metres between the government and OSM positions (confidence)

Runs daily: OSM brands change slowly and the Overpass call is heavy (~90 s).

Environment variables: GIST_URL (or GIST), GIST_TOKEN — see fuel2gist.py.
"""

import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_QUERY = """
[out:json][timeout:90];
(
  node["ref:FR:prix-carburants"];
  way["ref:FR:prix-carburants"];
  relation["ref:FR:prix-carburants"];
);
out center tags;
"""
GOV_EXPORT_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "prix-des-carburants-en-france-flux-instantane-v2/exports/json"
)
MAP_FILENAME = "osm-map.json"
USER_AGENT = "fuel2gist"


def http_request(url, method="GET", data=None, headers=None, retries=3):
    import gzip

    headers = {"Accept-Encoding": "gzip", "User-Agent": USER_AGENT, **(headers or {})}
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, method=method, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            last_error = exc
    raise RuntimeError(f"Request failed: {last_error}")


def fetch_overpass():
    """Query Overpass mirrors, retrying across the pool until one answers.

    Overpass is a volunteer-run service — transient 502s and read timeouts
    are common under load, so we retry each mirror several times with a
    growing delay between rounds.
    """
    body = ("data=" + urllib.parse.quote(OVERPASS_QUERY)).encode()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    last_error = None
    for attempt in range(4):
        for url in OVERPASS_URLS:
            try:
                return json.loads(
                    http_request(url, "POST", body, headers, retries=1)
                )
            except Exception as exc:  # noqa: BLE001 - try the next mirror
                last_error = exc
                print(f"Overpass {url} attempt {attempt + 1} failed: {exc}")
        if attempt < 3:
            time.sleep(30 * (attempt + 1))
    raise RuntimeError(f"All Overpass mirrors failed: {last_error}")


def element_pos(el):
    if "lat" in el:
        return el["lat"], el["lon"]
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def norm_key(name):
    """Canonical grouping key for a brand/name spelling."""
    return re.sub(r"[^0-9a-zàâäéèêëîïôöùûüç]", "", name.lower())


def identity(tags):
    """Best identity for a fuel feature: brand > name > operator."""
    return tags.get("brand") or tags.get("name") or tags.get("operator")


def pick_element(elements, gov_pos):
    """Among OSM elements sharing a gov ref, keep the best one:
    prefer an element carrying an identity, then nearest gov position."""
    with_identity = [e for e in elements if identity(e.get("tags") or {})]
    pool = with_identity or elements
    if gov_pos[0] is None:
        return pool[0]
    return min(pool, key=lambda e: haversine_m(gov_pos[0], gov_pos[1], *element_pos(e)))


def main():
    gist_url = os.environ.get("GIST_URL") or os.environ.get("GIST")
    gist_token = os.environ.get("GIST_TOKEN")
    if not gist_url or not gist_token:
        sys.exit("Missing GIST_URL (or GIST) / GIST_TOKEN environment variables")

    print("Downloading OSM ref:FR:prix-carburants elements...")
    elements = fetch_overpass()["elements"]
    print(f"Downloaded {len(elements)} OSM elements")

    print("Downloading government feed (positions for distance metric)...")
    gov = json.loads(http_request(GOV_EXPORT_URL))
    gov_by_id = {str(r["id"]): r for r in gov}

    # Group OSM elements per government id.
    by_ref = defaultdict(list)
    for el in elements:
        ref = (el.get("tags") or {}).get("ref:FR:prix-carburants", "").strip()
        if ref.isdigit() and ref in gov_by_id and element_pos(el)[0] is not None:
            by_ref[ref].append(el)

    # Resolve identity per station, collecting spellings for normalization.
    entries = {}  # gov_id -> (name, lat, lon, dist_m)
    spellings = Counter()
    for ref, els in by_ref.items():
        geom = gov_by_id[ref].get("geom") or {}
        gov_pos = (geom.get("lat"), geom.get("lon"))
        el = pick_element(els, gov_pos)
        name = identity(el.get("tags") or {})
        lat, lon = element_pos(el)
        dist = (
            int(round(haversine_m(gov_pos[0], gov_pos[1], lat, lon)))
            if gov_pos[0] is not None
            else None
        )
        entries[ref] = (name, lat, lon, dist)
        if name:
            spellings[name] += 1

    # Brand table: canonical key -> most frequent spelling.
    canonical = {}
    for name, count in spellings.items():
        key = norm_key(name)
        if key not in canonical or count > canonical[key][1]:
            canonical[key] = (name, count)
    brands = sorted(name for name, _ in canonical.values())
    brand_idx = {name: i for i, name in enumerate(brands)}

    station_map = {}
    for ref, (name, lat, lon, dist) in entries.items():
        b_idx = brand_idx[canonical[norm_key(name)][0]] if name else None
        station_map[ref] = [b_idx, round(lat, 5), round(lon, 5), dist]

    payload = {
        "v": 1,
        "ts": int(datetime.now(timezone.utc).timestamp() // 60),
        "brands": brands,
        "map": station_map,
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    covered = len(station_map)
    print(
        f"Mapped {covered}/{len(gov)} stations "
        f"({100 * covered / len(gov):.1f}%), {len(brands)} brands, {len(content)} bytes"
    )

    gist_id = gist_url.rstrip("/").split("/")[-1]
    body = json.dumps({"files": {MAP_FILENAME: {"content": content}}}).encode()
    http_request(
        f"https://api.github.com/gists/{gist_id}",
        method="PATCH",
        data=body,
        headers={
            "Authorization": f"Bearer {gist_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    print(f"Gist {gist_id} updated ({MAP_FILENAME})")


if __name__ == "__main__":
    main()
