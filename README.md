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
| `field_checklist.py` | Generate bird + plant + sea life field checklists from eBird, Cornell, iNaturalist, Go Botany, USDA, iDigBio, OBIS, WoRMS, and more |
| `trail_map.py` | Build interactive Leaflet.js map with trails, parks, heritage sites, bathymetry, nautical charts, and ocean currents from OSM, eBird, NOAA |

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

### Bird Pipeline

| Source | API / Method | Data Provided |
|--------|-------------|---------------|
| [eBird API](https://ebird.org/home) | `/v2/data/obs/geo/recent` | Species list (recent observations), seasonality (presence-based) |
| [Cornell All About Birds](https://www.allaboutbirds.org/) | Web scrape: overview, field ID, sounds pages | Descriptions, habitat, food, nesting, behavior, conservation status, images (CDN), audio (ML asset IDs), field measurements, order/family taxonomy |

### Plant Pipeline

| Source | API / Method | Data Provided |
|--------|-------------|---------------|
| [iNaturalist](https://www.inaturalist.org/) | `/v1/observations/species_counts`, `/v1/observations/histogram` | Species list, observation counts, seasonality histograms, taxon photos |
| [Go Botany / Native Plant Trust](https://gobotany.nativeplanttrust.org/) | Web scrape | Descriptions, habitat, family, growth form, images |
| [USDA PLANTS](https://plants.usda.gov/) | Web scrape (PlantSearch + detail pages) | Descriptions, habitat, family |
| [Missouri Botanical Garden](https://www.missouribotanicalgarden.org/plantfinder/) | Web scrape | Descriptions, habitat, family |
| [UF/IFAS Florida Trees](https://floridatrees.ifas.ufl.edu/) | XML servlet (`getTreeXML`) | Tree descriptions, habitat, growth habit (Florida-specific) |
| [Florida Plant Atlas](https://florida.plantatlas.usf.edu/) | Web scrape | Family, growth habit (Florida-specific) |
| [iDigBio](https://portal.idigbio.org/) | `/v2/search/records/` POST | Habitat notes from herbarium specimen records (92K+ plant specimens in the Panhandle) |
| [Wikipedia](https://en.wikipedia.org/) | MediaWiki API (`w/api.php`) | Fallback descriptions |

### Sea Life Pipeline

| Source | API / Method | Data Provided |
|--------|-------------|---------------|
| [iNaturalist](https://www.inaturalist.org/) | `/v1/observations/species_counts` | Species list for mollusks, crustaceans, echinoderms, jellyfish, sea turtles, marine mammals, seaweed |
| [OBIS](https://obis.org/) | `/v3/checklist` | Supplemental seaweed/algae species |
| [WoRMS](https://www.marinespecies.org/) | REST: `AphiaRecordsByMatchNames`, `AphiaAttributesByAphiaID`, `AphiaVernacularsByAphiaID` | Taxonomy, body size, functional group, habitat, depth, distribution, common names |
| [Wikimedia Commons](https://commons.wikimedia.org/) | MediaWiki API | Fallback seaweed images |
| Curated data | Built-in dictionaries | 26 Gulf fish species (Springer-sourced descriptions), 31 seaweed morphological descriptions, edibility tags |

### Weather & Astronomy

| Source | API | Data Provided |
|--------|-----|---------------|
| [Open-Meteo](https://open-meteo.com/) | `/v1/forecast` + `/v1/marine` | Daily weather forecast, sunrise/sunset, wind, wave height (seas) |
| [USNO](https://aa.usno.navy.mil/) | `/api/moon/phases/year` | Moon phase calendar for trip dates |

### Map Layers

| Layer | Source | Type |
|-------|--------|------|
| Hiking Trails, Bike Routes, Beaches, Parks, Forests | [OpenStreetMap](https://www.openstreetmap.org/) Overpass API | GeoJSON |
| Lighthouses, Heritage Sites | OSM + [NPS NRHP](https://www.nps.gov/subjects/nationalregister/) | GeoJSON |
| Protected Wildlife Areas | OSM (`protect_class`) | GeoJSON |
| Estuarine Reserves | OSM (`operator=NOAA`) | GeoJSON |
| Rare Species | [iNaturalist](https://www.inaturalist.org/) threatened/endangered observations | GeoJSON |
| Birding Hotspots | [eBird API](https://ebird.org/) `/v2/ref/hotspot/geo` | GeoJSON |
| eBird Observations | [eBird API](https://ebird.org/) `/v2/data/obs/geo/recent` | Clustered markers |
| Gulf Bathymetry | [NOAA NCEI](https://data.noaa.gov/) Gulf-wide DEM (ArcGIS tiles) | Tile overlay |
| NOAA Nautical Charts | [NOAA Chart Display Service](https://gis.charttools.noaa.gov/) WMTS | Tile overlay |
| Active Wildfires | [NIFC WFIGS](https://data-nifc.opendata.arcgis.com/) current fire perimeters (ArcGIS FeatureServer) | GeoJSON (live) |
| Smoke Plumes | [NOAA HMS](https://www.ospo.noaa.gov/products/land/hms.html) satellite smoke detection (ArcGIS FeatureServer) | GeoJSON (live) |
| Ocean Surface Currents | [NOAA/AOML](https://www.aoml.noaa.gov/) drifter-derived climatology (ArcGIS ImageServer) | WMS overlay |
| Base Camp marker | Fixed coordinate (42 Banfill Rd, Grayton Beach) | Star icon |

### Tides

| Source | API | Data Provided |
|--------|-----|---------------|
| [NOAA Tides & Currents](https://tidesandcurrents.noaa.gov/) | CO-OPS API | Tide predictions for trip date range |

## Caching & Render-Only Mode

A full build fetches data from 15+ external APIs and takes 5-10 minutes. To avoid this on CSS/template-only changes, the build saves a `.snapshot.json` file containing all species data. Subsequent runs can use `--render-only` to rebuild HTML in under 1 second with zero API calls.

```bash
# Full build (fetches everything, saves snapshot)
python3 field_checklist.py \
  --place "Gulf Islands" --date 2026-04-28 \
  --lat 30.3298 --lng -86.1650 \
  --ebird-key "$EBIRD_API_KEY" \
  --tide-station 8729511 --tide-dates 20260425,20260502

# Render-only (uses cached snapshot, no API calls)
python3 field_checklist.py \
  --place "Gulf Islands" --date 2026-04-28 \
  --lat 30.3298 --lng -86.1650 --render-only
```

### Cache Files

| File | Contents | Used By |
|------|----------|---------|
| `.snapshot.json` | Complete species data for `--render-only` | `field_checklist.py` |
| `.bird_cache.json` | Cornell All About Birds scraped data | Bird pipeline |
| `.plant_cache.json` | Go Botany, USDA, MoBot, UF Trees, Florida Atlas, iDigBio data | Plant pipeline |
| `.sea_cache.json` | OBIS, WoRMS, Wikipedia data for sea life | Sea life pipeline |
| `.seasonality.json` | iNaturalist monthly histograms (plants/sea life) | Plant + sea life pipelines |
| `.atlas_index.json` | Florida Plant Atlas species index | Plant pipeline |
| `.uf_trees_index.json` | UF/IFAS Florida Trees index | Plant pipeline |
| `.map_cache.json` | OpenStreetMap Overpass query results | `trail_map.py` |

### CLI Flags

| Flag | Description |
|------|-------------|
| `--place` | Place name for the checklist header |
| `--date` | Target date (YYYY-MM-DD) |
| `--lat`, `--lng` | Search center coordinates |
| `--radius` | Search radius in km (default: 20) |
| `--ebird-key` | eBird API key (or set `EBIRD_API_KEY` env var) |
| `--tide-station` | NOAA station ID for tide predictions |
| `--tide-dates` | Date range `YYYYMMDD,YYYYMMDD` for tides/weather/moon |
| `--skip-images` | Skip downloading species images (use existing) |
| `--render-only` | Rebuild HTML from cached snapshot (no API calls) |
| `--birds-only` | Only run bird pipeline |
| `--plants-only` | Only run plant pipeline |

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
