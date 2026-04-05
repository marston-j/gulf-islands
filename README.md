# Gulf Islands

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Field checklist and interactive trail map for the Gulf Islands region (FL/AL Gulf Coast). Generates a self-contained HTML page with bird and plant species cards, an interactive Leaflet.js trail map, and NOAA tide predictions — all from public APIs.

## Prerequisites

- Python 3.9+
- An eBird API key (free — [get one here](https://ebird.org/api/keygen))

## Quick Start

```bash
# Clone and install
git clone https://github.com/marston-j/gulf-islands.git
cd gulf-islands
pip install -r requirements.txt

# Set your eBird key (never commit this)
export EBIRD_API_KEY="your_key_here"

# Generate the checklist
python3 field_checklist.py \
  --place "Grayton Beach Florida" \
  --date 2026-04-28 \
  --lat 30.3298 --lng -86.1650 \
  --ebird-key "$EBIRD_API_KEY"

# Build the trail map
python3 trail_map.py \
  --bbox 29.5,-88.3,30.85,-84.0 \
  --ebird-key "$EBIRD_API_KEY" \
  --target output/gulf-islands/index.html
```

Then open `output/gulf-islands/index.html` in a browser.

## Scripts

| Script | Purpose |
|---|---|
| `field_checklist.py` | Generate bird + plant field checklists from eBird, Cornell, iNaturalist, Go Botany, USDA PLANTS |
| `trail_map.py` | Build interactive Leaflet.js trail maps from OSM, NPS, and eBird data |
| `fetch_descriptions.py` | Enrich species cards with Wikipedia / iNaturalist descriptions |

## Bbox Presets

`trail_map.py` supports named bbox presets in place of coordinates:

| Preset | Area | Bbox (S,W,N,E) |
|--------|------|-----------------|
| `gulf-panhandle` | Full FL/AL Gulf Panhandle | `29.5,-88.3,30.85,-84.0` |
| `apalachicola-nerr` | Apalachicola NERR Estuary | `29.586522,-85.385000,29.867725,-84.572274` |
| `grayton-beach` | Grayton Beach area | `30.25,-86.30,30.45,-86.05` |

Example: `--bbox apalachicola-nerr`

## Tide Predictions

Auto-fetch NOAA tide predictions by adding station and date range:

```bash
python3 field_checklist.py \
  --place "Grayton Beach Florida" \
  --date 2026-04-28 \
  --lat 30.3298 --lng -86.1650 \
  --ebird-key "$EBIRD_API_KEY" \
  --tide-station 8729511 \
  --tide-dates 20260425,20260502
```

Known stations: Destin East Pass (`8729511`), Panama City Beach (`8729210`), Pensacola (`8729840`)

## Output

Generated files are written to `output/gulf-islands/` (gitignored — regenerate locally):

```
output/gulf-islands/
  index.html       Combined page (Birds / Plants / Map tabs)
  birds.csv        Bird species data
  plants.csv       Plant species data
  images/          Downloaded species images
```

## Data Sources

| Category | Sources |
|----------|---------|
| **Birds** | [eBird API](https://ebird.org/home), [Cornell All About Birds](https://www.allaboutbirds.org/), [iNaturalist](https://www.inaturalist.org/), [Wikipedia](https://en.wikipedia.org/) |
| **Plants** | [iNaturalist](https://www.inaturalist.org/), [Go Botany / Native Plant Trust](https://gobotany.nativeplanttrust.org/), [USDA PLANTS](https://plants.usda.gov/), [Missouri Botanical Garden](https://www.missouribotanicalgarden.org/plantfinder/), [Wikipedia](https://en.wikipedia.org/) |
| **Map** | [OpenStreetMap](https://www.openstreetmap.org/) (Overpass API), [NPS NRHP](https://www.nps.gov/subjects/nationalregister/), eBird hotspots & observations, iNaturalist (rare species), Florida DEP Aquatic Preserves, [NOAA NERR](https://coast.noaa.gov/nerrs/) |
| **Tides** | [NOAA Tides & Currents](https://tidesandcurrents.noaa.gov/) (CO-OPS API) |
| **References** | Flora Novae Angliae (Haines), Dirr's Manual of Woody Landscape Plants, Florida Natural Heritage Program, NatureServe, National Wetland Plant List |

## Security

- **Never commit API keys.** Use environment variables or CLI arguments.
- Generated output files are gitignored to prevent accidental data leakage.
- No authentication data is stored in source code.

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-change`)
3. Commit your changes (`git commit -m "Add my change"`)
4. Push to your branch (`git push origin feature/my-change`)
5. Open a Pull Request

## License

[MIT](LICENSE)
