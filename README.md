# fuel2gist

A lightweight tool that retrieves real-time open data on fuel prices in France on an hourly basis (via the official government API) and publishes it as a compact JSON file in a GitHub Gist.

## How it works

A GitHub Actions workflow (`.github/workflows/update-gist.yml`) runs every hour and:

1. Downloads the full instantaneous feed from
   [data.economie.gouv.fr](https://data.economie.gouv.fr/explore/dataset/prix-des-carburants-en-france-flux-instantane-v2/)
   (~4.7 MB, ~9,800 stations, one request, no pagination).
2. Condenses each station down to: address, city, postal code, GPS coordinates,
   opening hours, 24/7 credit-card automate availability, and for every fuel:
   its price and last-update time. Fuels under permanent ("definitive") shortage
   are dropped; fuels under temporary shortage are kept in a separate list.
3. Pushes the result (~1.6 MB minified JSON) as `stations.json` to a GitHub Gist.

The pipeline is implemented in `fuel2gist.py`, a single Python script with no
dependency outside the standard library.

## Setup

Create a (secret) GitHub gist first, then configure the repository:

| Location | Name | Content |
|---|---|---|
| Repository variable | `GIST` | Full URL of the gist, e.g. `https://gist.github.com/<user>/<gist_id>` |
| Repository secret | `GIST_TOKEN` | Fine-grained PAT or classic token with the `gist` scope |

Then enable the workflow (runs at minute 7 of every hour) or trigger it manually
with *Run workflow*.

You can also run it locally:

```sh
GIST_URL="https://gist.github.com/<user>/<gist_id>" GIST_TOKEN=... python fuel2gist.py
```

## Using the data

The file produced by this repository's workflow is published hourly at:

```
https://gist.githubusercontent.com/fathzer/fd0545d020573481d86e9966a48e51fa/raw/stations.json
```

If you run your own fork, the raw URL follows the pattern
`https://gist.githubusercontent.com/<user>/<gist_id>/raw/stations.json`.

### Format

```json
{
  "v": 1,
  "ts": 29899207,
  "src": "https://data.economie.gouv.fr/.../exports/json",
  "fuels": ["E10", "E85", "GPLc", "Gazole", "SP95", "SP98"],
  "stations": [ ... ]
}
```

- `v`: format version.
- `ts`: generation timestamp, in **minutes since the Unix epoch (UTC)**.
- `fuels`: ordered fuel names. Fuel indexes used inside stations refer to this array.
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

for (const [id, lat, lon, addr, city, cp, h24, oh, f] of data.stations) {
  for (const [fuelIdx, price, updated] of f) {
    const fuel = data.fuels[fuelIdx];
    const euros = price / 1000;
    const when = new Date(updated * 60000);
    console.log(`${addr}, ${city}: ${fuel} = ${euros} €/L (${when})`);
  }
}
```

## Notes

- Scheduled GitHub Actions may be delayed by a few minutes under load.
- The file exceeds 1 MB, so the GitHub API returns it truncated — always fetch
  the **raw** URL above, which serves the full content.
- License: see [LICENSE](LICENSE). Data is French public open data
  (Licence Ouverte / Open Licence, Etalab).
