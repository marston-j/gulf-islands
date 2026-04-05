# Gulf Islands

Field checklist and interactive trail map for the Gulf Islands region (FL/AL Gulf Coast).

## Scripts

| Script | Purpose |
|---|---|
| `field_checklist.py` | Generate bird + plant field checklists from eBird, Cornell, iNaturalist, Go Botany, USDA PLANTS |
| `trail_map.py` | Build interactive Leaflet.js trail maps from OSM, NPS, and eBird data |

## Usage

```bash
pip install -r requirements.txt

python3 field_checklist.py \
  --place "Gulf Islands" \
  --date 2026-04-28 \
  --lat 30.3960 --lng -86.2286 \
  --ebird-key YOUR_KEY

python3 trail_map.py \
  --bbox 29.5,-88.3,30.85,-84.0 \
  --ebird-key YOUR_KEY \
  --target output/gulf-islands/index.html
```

Get a free eBird API key at https://ebird.org/api/keygen

## Output

```
output/gulf-islands/
  index.html       Combined page (Birds / Plants / Map tabs)
  birds.csv        Bird species data
  plants.csv       Plant species data
  images/          Downloaded species images (gitignored)
```

## Data Sources

**Birds**: eBird API, Cornell All About Birds, iNaturalist

**Plants**: iNaturalist, Go Botany / Native Plant Trust, USDA PLANTS Database,
Missouri Botanical Garden Plant Finder

**Plant Reference Attribution**: Flora Novae Angliae (Haines), Dirr's Manual of
Woody Landscape Plants, Florida Natural Heritage Program, NatureServe, National
Wetland Plant List

**Map**: OpenStreetMap, NPS National Register of Historic Places, eBird hotspots
