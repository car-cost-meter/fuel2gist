# fuel2gist

A lightweight tool that retrieves real-time open data on fuel prices in France on an hourly basis (via the official government API) and publishes it as a compact JSON file in a GitHub Gist.

## How it works

Two GitHub Actions workflows — both triggered externally, see
[Triggering the workflow](#triggering-the-workflow) — publish two files to a
GitHub Gist:

### Hourly: `stations.json` (`update-gist.yml` + `fuel2gist.py`)

1. Downloads the full instantaneous feed from
   [data.economie.gouv.fr](https://data.economie.gouv.fr/explore/dataset/prix-des-carburants-en-france-flux-instantane-v2/)
   (~4.7 MB, ~9,800 stations, one request, no pagination).
2. Condenses each station down to: address, city, postal code, GPS coordinates,
   opening hours, 24/7 credit-card automate availability, brand (via the OSM
   mapping below), and for every fuel: its price and last-update time. Fuels
   under permanent ("definitive") shortage are dropped; fuels under temporary
   shortage are kept in a separate list. Stations with no prices at all are
   excluded.
3. Pushes the result (~1.7 MB minified JSON) as `stations.json` to the gist.

### Daily: `osm-map.json` (`update-brands.yml` + `osm_brands.py`)

The government feed carries no station name or brand. OpenStreetMap solves
this: contributors tag fuel stations with `ref:FR:prix-carburants` — the
government station id — giving an **exact, ambiguity-free link** (~99.5%
coverage). A bonus: OSM coordinates proved *more accurate* than the
government's geocoded positions, so they are used for station locations.

1. Queries the Overpass API for every `node|way|relation` tagged
   `ref:FR:prix-carburants` (one bulk request, ~90 s).
2. Resolves each station's identity (`brand` > `name` > `operator`), with
   spelling normalized data-driven (canonical form grouped by normalized
   key, displayed as its most frequent spelling).
3. Pushes `osm-map.json` to the same gist — the hourly pipeline fetches it
   back (and degrades gracefully to no brands if it is unavailable).

Both pipelines are single Python scripts with no dependency outside the
standard library.

## Setup

Create a (secret) GitHub gist first, then configure the repository:

| Location | Name | Content |
|---|---|---|
| Repository variable | `GIST` | Full URL of the gist, e.g. `https://gist.github.com/<user>/<gist_id>` |
| Repository secret | `GIST_TOKEN` | Fine-grained PAT or classic token with the `gist` scope |

Then trigger the workflows manually with *Run workflow* (external scheduling
described below).

You can also run it locally:

```sh
GIST_URL="https://gist.github.com/<user>/<gist_id>" GIST_TOKEN=... python fuel2gist.py
```

## Triggering the workflow

The workflow exposes a `workflow_dispatch` trigger, so anything able to POST
to the GitHub API can run it. This repository does **not** rely on GitHub's
own `schedule:` event — here's why.

### Why not GitHub `schedule:`

GitHub's cron trigger looks convenient but comes with serious limitations:

- **It is best-effort, not a real cron.** GitHub only documents that runs
  "may be delayed during periods of high load"; in practice delays of tens
  of minutes are routine (especially around the top of the hour), and
  scheduled runs are sometimes skipped entirely — you simply get fewer runs
  than the schedule implies, with no error, no log, no retry.
- **It only exists on the default branch.** The schedule is read from the
  workflow file on the default branch at firing time; pushing it on a
  feature branch does nothing. Newly registered schedules can also take a
  long time to be picked up — on this repository, a freshly pushed schedule
  never fired a single run.
- **It auto-disables on inactive repositories.** After 60 days without
  repository activity, GitHub silently disables scheduled workflows (one
  notification email, then nothing runs until someone re-enables it).
  This is a structural problem for a set-and-forget pipeline like this one:
  the whole point is that nothing commits for months — which is exactly
  what triggers the shutdown.

### Option A — cron-job.org (simple external trigger)

A free [cron-job.org](https://cron-job.org) job calls the dispatch endpoint
every hour:

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/car-cost-meter/fuel2gist/actions/workflows/update-gist.yml/dispatches` |
| Method | `POST` |
| Headers | `Authorization: Bearer <token>`, `Accept: application/vnd.github+json` |
| Body | `{"ref":"main"}` |
| Expected status | `204` (empty response) |

Any HTTP-capable scheduler works the same way — e.g. a plain `crontab` entry
running `curl -X POST` with the same arguments.

### The token problem

The dispatch endpoint requires a token:

- a **fine-grained PAT** limited to the repository with
  *Actions: Read and write* — minimal blast radius, but bound to a single
  resource owner (one user **or** one org, so one token per organization) and
  forced to expire within a year;
- or a **classic PAT** (`public_repo` is enough for public repositories) —
  one token covers every organization the user can reach and can be created
  without expiration, at the price of a broader scope.

Either way, the token is stored on a third-party service, needs periodic
rotation, and is tied to one person's account — a bus factor of 1.

### Option B — GitHub App + Cloudflare Worker (current setup)

The maintenance-free setup actually deployed, with no personal dependency:

1. A **GitHub App** owned by the organization (`fuel2gist-dispatcher`),
   granted *Actions: Read and write* and installed on `fuel2gist` only.
   Its private key never expires and outlives any individual account.
2. A **Cloudflare Worker** (free tier) — code in [`worker/`](worker/). It
   signs a JWT with the app's private key, exchanges it for an installation
   token and calls the dispatch endpoint. Two cron triggers route to the two
   workflows via `event.cron`: `7 * * * *` hourly → `update-gist.yml`,
   `23 4 * * *` daily → `update-brands.yml`.

Deployment notes:

- `worker/index.js` is the whole dispatcher — runtime APIs only, zero
  dependencies;
- `worker/wrangler.toml` versions the worker name, the cron triggers and the
  non-secret variables (`GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID` are plain
  identifiers, not credentials);
- the only real secret, `GITHUB_APP_KEY` (the app's PEM private key), lives
  in Cloudflare *Variables and Secrets*;
- `GET /` on the worker URL fires the hourly dispatch manually, `GET
  /brands` the daily one — handy for testing (remove the `fetch` handler in
  `worker/index.js` if unwanted).

## Using the data

The file produced by this repository's workflow is published hourly at:

```
https://gist.githubusercontent.com/fathzer/fd0545d020573481d86e9966a48e51fa/raw/stations.json
```

If you run your own fork, the raw URL follows the pattern
`https://gist.githubusercontent.com/<user>/<gist_id>/raw/stations.json`.

The same gist also hosts **`osm-map.json`** (updated daily), the raw
OSM→government mapping — mostly useful for auditing: `{"v", "ts", "brands",
"map"}` where `map[id] = [brandIdx, lat, lon, distM]` (`distM` = metres
between the government and OSM positions, a confidence metric).

### Format

```json
{
  "v": 1,
  "ts": 29899207,
  "src": "https://data.economie.gouv.fr/.../exports/json",
  "fuels": ["E10", "E85", "GPLc", "Gazole", "SP95", "SP98"],
  "brands": ["Agip", "Avia", "Carrefour", ...],
  "stations": [ ... ]
}
```

- `v`: format version.
- `ts`: generation timestamp, in **minutes since the Unix epoch (UTC)**.
- `fuels`: ordered fuel names. Fuel indexes used inside stations refer to this array.
- `brands`: ordered brand names from the OSM mapping (may be empty if the
  daily job has not run yet). Brand indexes used inside stations refer to
  this array.
- `stations`: one array per station, positional fields:

| # | Field | Description |
|---|---|---|
| 0 | id | Station id (integer) |
| 1 | lat | Latitude (WGS84, 5 decimals) |
| 2 | lon | Longitude (WGS84, 5 decimals) |
| 3 | address | Street address |
| 4 | city | City name |
| 5 | cp | Postal code (string) |
| 6 | h24 | `1` if a 24/7 payment automate is present, else `0` |
| 7 | oh | Opening hours, see below (`null` = open 24/7 or not published) |
| 8 | f | Priced fuels: `[[fuelIdx, price, updated], ...]` |
| 9 | o | Temporarily out-of-stock fuels: `[fuelIdx, ...]` |
| 10 | b | Brand index into `brands`, `null` when unmapped |
| 11 | g | `1` when coordinates are the unverified government geocoding (no OSM match), else `0` |

Coordinates come from OpenStreetMap when the station is mapped there — they
proved more accurate than the government's geocoded positions. Stations
with no prices are excluded entirely.

**Opening hours (`oh`)**: 7 entries, Monday (index 0) to Sunday (index 6).
Each entry is:

- `null`: closed that day;
- `[]`: open, no schedule published (typically 24h);
- `[a, b, c, d, ...]`: opening slots as minute pairs — opens at `a`,
  closes at `b` (minutes since midnight), etc. `[360, 1140]` = 06:00–19:00,
  `[480, 720, 840, 1140]` = 08:00–12:00 and 14:00–19:00.

`oh` itself is `null` when no schedule is published at all — treat as 24/7.
This also covers stations that declare every day as closed: that pattern is
a known artifact of the government feed (automate-only stations that never
declared shop hours — hundreds of them still publish prices), so it is
reported as "no schedule" rather than "permanently closed".

**Fuels (`f`)**: each entry is `[fuelIdx, price, updated]`:

- `fuelIdx`: index into the top-level `fuels` array;
- `price`: price per liter in thousandths of a euro (`2190` = 1.990 €/L);
- `updated`: last price update, in minutes since the Unix epoch (UTC).

### Example (JavaScript)

```js
const data = await (await fetch(RAW_URL)).json();

for (const [id, lat, lon, addr, city, cp, h24, oh, f, o, b, g] of data.stations) {
  const brand = b === null ? "?" : data.brands[b];
  for (const [fuelIdx, price, updated] of f) {
    const fuel = data.fuels[fuelIdx];
    const euros = price / 1000;
    const when = new Date(updated * 60000);
    console.log(`${brand} — ${addr}, ${city}: ${fuel} = ${euros} €/L (${when})`);
  }
}
```

## Notes

- Freshness is best-effort: check the `ts` field to know how old the data is.
- The file exceeds 1 MB, so the GitHub API returns it truncated — always fetch
  the **raw** URL above, which serves the full content.
- License: see [LICENSE](LICENSE). Data is French public open data
  (Licence Ouverte / Open Licence, Etalab).
