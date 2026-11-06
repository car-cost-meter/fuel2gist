#!/usr/bin/env python3
"""Fetch French fuel station data, condense it and publish it to a GitHub Gist.

Source: https://data.economie.gouv.fr - dataset
"prix-des-carburants-en-france-flux-instantane-v2" (real-time feed).

Environment variables:
    GIST_URL    (or GIST): URL of the target gist, e.g.
                https://gist.github.com/<user>/<gist_id>
    GIST_TOKEN: GitHub token with the "gist" scope.
"""

import gzip
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

SOURCE_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "prix-des-carburants-en-france-flux-instantane-v2/exports/json"
)
GIST_FILENAME = "stations.json"
USER_AGENT = "fuel2gist"

# Alternate names normalized to their canonical fuel (same product at the pump:
# "SP95-E10" is the legacy id for the fuel now declared as "E10").
FUEL_ALIASES = {"SP95-E10": "E10"}


def http_request(url, method="GET", data=None, headers=None, retries=3):
    headers = {"Accept-Encoding": "gzip", "User-Agent": USER_AGENT, **(headers or {})}
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, method=method, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            last_error = exc
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Request failed after {retries} attempts: {last_error}")


def as_list(value):
    """Normalize a JSON field that is either a single object or a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def hhmm_to_minutes(value):
    """Convert a "HH.MM" source time to minutes since midnight."""
    hours, minutes = value.split(".")
    return int(hours) * 60 + int(minutes)


def to_unix_minutes(value):
    """Convert "YYYY-MM-DD HH:MM:SS" (UTC in the feed) to minutes since epoch."""
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() // 60)


def parse_hours(horaires_json):
    """Return (has_24_7_automate, weekly_hours).

    weekly_hours is a 7-element list (Monday..Sunday) where each entry is:
      - None: station closed that day
      - []:   open, no schedule published (typically 24h)
      - [a, b, ...]: opening slots as minute pairs (open, close) since midnight
    The whole weekly_hours value is None when no schedule is published at all.
    """
    if not horaires_json:
        return 0, None

    horaires = json.loads(horaires_json)
    automate = 1 if horaires.get("@automate-24-24") == "1" else 0

    days = [d for d in as_list(horaires.get("jour")) if isinstance(d, dict)]
    by_id = {int(d.get("@id", 0)): d for d in days}
    if not by_id:
        return automate, None

    week = []
    for day_id in range(1, 8):
        day = by_id.get(day_id)
        if day is None or day.get("@ferme"):
            week.append(None)
            continue
        slots = [s for s in as_list(day.get("horaire")) if isinstance(s, dict)]
        if not slots:
            week.append([])
            continue
        flat = []
        for slot in slots:
            flat.append(hhmm_to_minutes(slot["@ouverture"]))
            flat.append(hhmm_to_minutes(slot["@fermeture"]))
        week.append(flat)

    # Collapse the common "open 24/7" case.
    if all(day == [] for day in week):
        week = None
    return automate, week


def parse_fuels(record, fuel_idx):
    """Return (priced_fuels, temporary_shortages) for a station record.

    priced_fuels: list of [fuel_index, price_milli_euro, update_unix_minutes]
    temporary_shortages: list of fuel_index
    Fuels under permanent ("definitive") shortage are ignored entirely.
    """
    ruptures = [
        r
        for r in as_list(json.loads(record.get("rupture") or "[]"))
        if isinstance(r, dict)
    ]
    permanent = {
        FUEL_ALIASES.get(r.get("@nom"), r.get("@nom"))
        for r in ruptures
        if r.get("@type") == "definitive"
    }
    temporary = {
        FUEL_ALIASES.get(r.get("@nom"), r.get("@nom"))
        for r in ruptures
        if r.get("@type") != "definitive" and not r.get("@fin")
    }

    priced = []
    prices = as_list(json.loads(record.get("prix") or "[]"))
    for price in prices:
        if not isinstance(price, dict):
            continue
        name = FUEL_ALIASES.get(price.get("@nom"), price.get("@nom"))
        if (
            name in permanent
            or name not in fuel_idx
            or not price.get("@valeur")
            or not price.get("@maj")
        ):
            continue
        priced.append(
            [
                fuel_idx[name],
                int(round(float(price["@valeur"]) * 1000)),
                to_unix_minutes(price["@maj"]),
            ]
        )

    shortages = sorted(fuel_idx[n] for n in temporary if n in fuel_idx)
    return priced, shortages


def condense(record, fuel_idx):
    geom = record.get("geom") or {}
    lat = geom.get("lat")
    lon = geom.get("lon")
    if lat is None and record.get("latitude"):
        lat = float(record["latitude"]) / 100000
    if lon is None and record.get("longitude"):
        lon = float(record["longitude"]) / 100000

    automate, week = parse_hours(record.get("horaires"))
    priced, shortages = parse_fuels(record, fuel_idx)

    return [
        record["id"],
        round(lat, 5) if lat is not None else None,
        round(lon, 5) if lon is not None else None,
        record.get("adresse") or "",
        record.get("ville") or "",
        record.get("cp") or "",
        automate,
        week,
        priced,
        shortages,
    ]


def main():
    gist_url = os.environ.get("GIST_URL") or os.environ.get("GIST")
    gist_token = os.environ.get("GIST_TOKEN")
    if not gist_url or not gist_token:
        sys.exit("Missing GIST_URL (or GIST) / GIST_TOKEN environment variables")

    print("Downloading fuel station dataset...")
    raw = http_request(SOURCE_URL)
    records = json.loads(raw)
    print(f"Downloaded {len(records)} stations ({len(raw)} bytes)")

    # First pass: collect every fuel name to build a stable index.
    fuel_names = set()
    for record in records:
        for field in ("prix", "rupture"):
            for entry in as_list(json.loads(record.get(field) or "[]")):
                if isinstance(entry, dict) and entry.get("@nom"):
                    fuel_names.add(FUEL_ALIASES.get(entry["@nom"], entry["@nom"]))
    fuels = sorted(fuel_names)
    fuel_idx = {name: i for i, name in enumerate(fuels)}

    stations = [condense(record, fuel_idx) for record in records]

    payload = {
        "v": 1,
        "ts": int(datetime.now(timezone.utc).timestamp() // 60),
        "src": SOURCE_URL,
        "fuels": fuels,
        "stations": stations,
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    print(f"Condensed payload: {len(content)} bytes for {len(stations)} stations")

    gist_id = gist_url.rstrip("/").split("/")[-1]
    body = json.dumps(
        {"files": {GIST_FILENAME: {"content": content}}}
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {gist_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    http_request(
        f"https://api.github.com/gists/{gist_id}",
        method="PATCH",
        data=body,
        headers=headers,
    )
    print(f"Gist {gist_id} updated ({GIST_FILENAME})")


if __name__ == "__main__":
    main()
