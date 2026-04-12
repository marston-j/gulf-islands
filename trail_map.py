#!/usr/bin/env python3
"""
Gulf Islands Trail Map Builder

Fetches geographic data from OpenStreetMap (Overpass API) and eBird,
then injects an interactive Leaflet.js map as a third tab into an
existing field checklist index.html.

Data layers:
  OSM  — Hiking trails, bike routes, beaches (public/private/vehicles),
         state parks, wilderness, wildlife refuges, state/national forests,
         lighthouses
  NPS  — Heritage sites (National Register + OSM historic structures)
  eBird — Birding hotspots, 30-day recent observations

Usage:
  python3 trail_map.py \\
    --bbox 29.5,-88.3,30.85,-84.0 \\
    --ebird-key YOUR_KEY \\
    --back 30 \\
    --target output/gulf-islands/index.html
"""

import argparse
import json
import logging
import math
import os
import re
import time
from pathlib import Path

import requests

try:
    import osm2geojson
except ImportError:
    print("Install osm2geojson:  pip install osm2geojson")
    raise SystemExit(1)

log = logging.getLogger("trail_map")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
EBIRD_API = "https://api.ebird.org/v2"
HEADERS = {"User-Agent": "TrailMapBuilder/1.0 (field-checklist)"}

NPS_NRHP_API = "https://mapservices.nps.gov/arcgis/rest/services/cultural_resources/nrhp_locations/MapServer"

# Preset bounding boxes for known regions
BBOX_PRESETS = {
    "gulf-panhandle": "29.5,-88.3,30.85,-84.0",
    "apalachicola-nerr": "29.586522,-85.385000,29.867725,-84.572274",
    "grayton-beach": "30.25,-86.30,30.45,-86.05",
}

LAYER_DEFS = {
    "hiking":      {"label": "Hiking Trails",       "color": "#D4820F", "on": True},
    "bike":        {"label": "Bike Routes",          "color": "#2E6B94", "on": True},
    "beaches_public":  {"label": "Public Beaches",      "color": "#27AE60", "on": False},
    "beaches_private": {"label": "Private / Restricted", "color": "#E74C3C", "on": False},
    "state_parks": {"label": "State Parks",          "color": "#3A7D50", "on": True},
    "wilderness":  {"label": "Wilderness Areas",     "color": "#4A6A3A", "on": False},
    "refuges":     {"label": "Wildlife Refuges",     "color": "#2A7A7A", "on": False},
    "forests":     {"label": "State / Nat'l Forests", "color": "#2D5A1E", "on": False},
    "lighthouses": {"label": "Lighthouses",          "color": "#C0392B", "on": True},
    "heritage":    {"label": "Heritage Sites",       "color": "#7A5230", "on": True},
    "critical_wildlife": {"label": "Protected Areas", "color": "#C62828", "on": True},
    "nerrs":       {"label": "Estuarine Reserves",   "color": "#005F73", "on": False},
    "inat_rare":   {"label": "Rare Species (iNat)",  "color": "#D4380D", "on": False},
    "hotspots":    {"label": "Birding Hotspots",     "color": "#8B4513", "on": True},
    "ebird_obs":   {"label": "eBird Obs (30 d)",     "color": "#1A6B3A", "on": False},
    "bathymetry":  {"label": "Gulf Bathymetry",      "color": "#1A5276", "on": False, "tile": True},
    "noaa_charts": {"label": "NOAA Nautical Charts", "color": "#1B4F72", "on": False, "tile": True},
    "currents":    {"label": "Ocean Currents",       "color": "#2874A6", "on": False, "tile": True},
}

# ─── Caching ───────────────────────────────────────────────────────

def load_cache(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_cache(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ─── Bounding-box / Grid ──────────────────────────────────────────

def parse_bbox(s: str) -> tuple:
    parts = [float(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must be S,W,N,E — got {s!r}")
    return tuple(parts)


def bbox_ql(bbox: tuple) -> str:
    return f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"


def grid_points(bbox: tuple, step_km: float = 70) -> list:
    s, w, n, e = bbox
    mid_lat = (s + n) / 2
    lat_step = step_km / 111.0
    lng_step = step_km / (111.0 * math.cos(math.radians(mid_lat)))
    pts = []
    lat = s + lat_step / 2
    while lat < n + lat_step / 2:
        lng = w + lng_step / 2
        while lng < e + lng_step / 2:
            pts.append((round(lat, 2), round(lng, 2)))
            lng += lng_step
        lat += lat_step
    return pts


# ─── GeoJSON helpers ──────────────────────────────────────────────

def _round_coords(coords):
    if isinstance(coords, (int, float)):
        return round(coords, 5)
    return [_round_coords(c) for c in coords]


def simplify_geojson(geojson: dict, keep_tags: list | None = None) -> dict:
    features = []
    for f in geojson.get("features", []):
        props = f.get("properties", {})
        tags = props.get("tags", {})
        flat: dict = {}
        if keep_tags:
            for k in keep_tags:
                val = tags.get(k) or props.get(k)
                if val:
                    flat[k] = val
        else:
            flat = dict(tags)
        geom = f.get("geometry")
        if not geom:
            continue
        if geom.get("coordinates"):
            geom = {**geom, "coordinates": _round_coords(geom["coordinates"])}
        features.append({"type": "Feature", "properties": flat, "geometry": geom})
    return {"type": "FeatureCollection", "features": features}


# ─── Overpass API ─────────────────────────────────────────────────

def _overpass(query: str, cache: dict, key: str) -> dict:
    if key in cache:
        log.info("    [cached] %s", key)
        return cache[key]
    log.info("    Querying Overpass: %s", key)
    for attempt in range(3):
        try:
            r = requests.post(
                OVERPASS_URL,
                data={"data": query},
                headers=HEADERS,
                timeout=180,
            )
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                log.warning("    Rate-limited — waiting %d s", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            cache[key] = data
            return data
        except Exception as exc:
            log.warning("    Attempt %d failed: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
    return {"elements": []}


def _osm_geojson(query: str, cache: dict, key: str,
                 keep: list | None = None) -> dict:
    raw = _overpass(query, cache, key)
    try:
        gj = osm2geojson.json2geojson(raw, log_level="ERROR")
    except Exception as exc:
        log.warning("    osm2geojson failed for %s: %s", key, exc)
        gj = {"type": "FeatureCollection", "features": []}
    return simplify_geojson(gj, keep)


PARK_AREA_REGEX = (
    "Bon Secour National Wildlife Refuge"
    "|Gulf Islands National Seashore"
    "|Fort Pickens"
    "|Point Washington State Forest"
    "|Pine Log State Forest"
    "|Topsail Hill Preserve"
    "|Tarklin Bayou"
    "|Perdido River"
    "|Eglin"
    "|Blackwater River State Forest"
    "|Conecuh National Forest"
    "|Big Lagoon State Park"
    "|Grayton Beach State Park"
    "|Deer Lake State Park"
    "|Henderson Beach State Park"
    "|Camp Helen State Park"
    "|St\\\\. Andrews State Park"
    "|Panama City Beach Conservation"
    "|Gayle.?s Trail"
    "|UWF"
    "|University of West Florida"
    "|SRIA"
    "|Santa Rosa Island Authority"
    "|Dunes Preserve"
    "|Village Point Preserve"
    "|Naval Live Oaks"
    "|St\\\\. George Island State Park"
    "|Saint George Island"
    "|St\\\\. Marks National Wildlife Refuge"
    "|Saint Marks National Wildlife"
    "|Apalachicola National Forest"
    "|Tate.?s Hell State Forest"
    "|Ochlockonee River State Park"
    "|Bald Point State Park"
    "|St\\\\. Joseph Bay"
    "|St\\\\. Andrews State Park"
    "|St\\\\. George Island"
)


def fetch_hiking(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:300];\n"
        f"(\n"
        f'  relation["route"="hiking"]({bb});\n'
        f'  relation["route"="foot"]({bb});\n'
        f'  way["highway"~"^(path|footway)$"]["name"]({bb});\n'
        f'  way["highway"="track"]["sac_scale"]({bb});\n'
        f");\n"
        f"out body;\n>;\nout skel qt;"
    )
    broad = _osm_geojson(q, cache, "hiking_v2",
                         ["name", "route", "highway", "sac_scale", "surface"])

    pq = (
        f"[out:json][timeout:300];\n"
        f'area["name"~"{PARK_AREA_REGEX}",i]->.parks;\n'
        f"(\n"
        f'  way["highway"~"^(path|footway|track|bridleway)$"](area.parks)({bb});\n'
        f'  relation["route"~"^(hiking|foot|walking)$"](area.parks)({bb});\n'
        f");\n"
        f"out body;\n>;\nout skel qt;"
    )
    park = _osm_geojson(pq, cache, "park_trails_v2",
                        ["name", "route", "highway", "sac_scale", "surface"])

    nq = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  way["highway"~"^(path|footway|track|bridleway)$"]'
        f'["name"~"Florida Trail|Juniper Creek|Jackson Trail|'
        f"Jackson Red Ground|Fort Pickens|Dunes Preserve|UWF|"
        f"Wiregrass|Bear Lake|Sweetwater|Eglin Trail|"
        f'Longleaf|Topsail|Tarklin|Bon Secour",i]({bb});\n'
        f");\n"
        f"out body;\n>;\nout skel qt;"
    )
    named = _osm_geojson(nq, cache, "named_trails",
                         ["name", "route", "highway", "sac_scale", "surface"])

    seen_coords = set()
    merged = []
    for f in (broad.get("features", [])
              + park.get("features", [])
              + named.get("features", [])):
        key = json.dumps(f.get("geometry", {}).get("coordinates", []))
        if key not in seen_coords:
            seen_coords.add(key)
            merged.append(f)
    return {"type": "FeatureCollection", "features": merged}


def fetch_bike(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  relation["route"="bicycle"]({bb});\n'
        f'  way["highway"="cycleway"]({bb});\n'
        f'  way["cycleway"~"."]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "bike",
                        ["name", "route", "highway", "cycleway", "surface"])


def fetch_beaches(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  way["natural"="beach"]({bb});\n'
        f'  node["natural"="beach"]({bb});\n'
        f'  relation["natural"="beach"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "beaches", ["name", "surface", "access", "vehicles"])


def fetch_state_parks(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  nwr["boundary"="protected_area"]["protection_title"~"State Park",i]({bb});\n'
        f'  nwr["leisure"="park"]["name"~"State",i]({bb});\n'
        f'  nwr["boundary"="national_park"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "state_parks",
                        ["name", "protection_title", "operator"])


def fetch_wilderness(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  nwr["boundary"="protected_area"]["protect_class"~"^(1b|2)$"]({bb});\n'
        f'  nwr["boundary"="protected_area"]["protection_title"~"Wilderness",i]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "wilderness",
                        ["name", "protection_title", "protect_class"])


def fetch_refuges(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  nwr["boundary"="protected_area"]["protection_title"~"Wildlife Refuge|NWR|Wildlife Management",i]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "refuges",
                        ["name", "protection_title", "operator"])


def fetch_forests(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  nwr["boundary"="protected_area"]["protection_title"~"State Forest|National Forest",i]({bb});\n'
        f'  nwr["landuse"="forest"]["name"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "forests",
                        ["name", "protection_title", "operator"])


def fetch_lighthouses(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  node["man_made"="lighthouse"]({bb});\n'
        f'  way["man_made"="lighthouse"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "lighthouses",
                        ["name", "start_date", "operator", "website"])


def fetch_historic(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n"
        f"(\n"
        f'  nwr["historic"~"^(monument|memorial|building|fort|ruins|'
        f'castle|archaeological_site|battlefield|heritage)$"]({bb});\n'
        f'  node["tourism"="museum"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "historic",
                        ["name", "historic", "tourism", "heritage",
                         "start_date", "description", "wikipedia",
                         "website", "operator"])


# ─── NPS National Register of Historic Places ────────────────────

def _nrhp_query(layer_id: int, bbox: tuple, cache: dict,
                cache_key: str) -> list:
    """Query one NPS ArcGIS layer and return GeoJSON features."""
    if cache_key in cache:
        log.info("    [cached] %s", cache_key)
        return cache[cache_key]

    s, w, n, e = bbox
    url = f"{NPS_NRHP_API}/{layer_id}/query"
    fields = "RESNAME,ResType,Address,City,County,State,CertDate,Is_NHL,NRIS_Refnum,NARA_URL"
    offset = 0
    all_features = []
    while True:
        params = {
            "where": "1=1",
            "geometry": f"{w},{s},{e},{n}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": fields,
            "returnGeometry": "true",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": 2000,
        }
        log.info("    NPS NRHP layer %d  offset=%d ...", layer_id, offset)
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.warning("    NPS query failed: %s", exc)
            break
        batch = data.get("features", [])
        all_features.extend(batch)
        if len(batch) < 2000:
            break
        offset += len(batch)

    cache[cache_key] = all_features
    return all_features


def fetch_nrhp(bbox, cache):
    """Fetch NRHP points (layer 0) and district polygons (layer 1)."""
    pts = _nrhp_query(0, bbox, cache, "nrhp_points")
    polys = _nrhp_query(1, bbox, cache, "nrhp_polygons")

    features = []
    for f in pts + polys:
        props = f.get("properties", {})
        geom = f.get("geometry")
        if not geom:
            continue
        name = props.get("RESNAME") or "Unknown"
        rtype = props.get("ResType") or ""
        city = props.get("City") or ""
        county = props.get("County") or ""
        state = props.get("State") or ""
        addr = props.get("Address") or ""
        cert = props.get("CertDate") or ""
        nhl = props.get("Is_NHL")
        refnum = props.get("NRIS_Refnum") or ""
        nara = props.get("NARA_URL") or ""
        slim = {
            "name": name,
            "type": rtype,
            "address": addr,
            "city": city,
            "county": county,
            "state": state,
            "listed": cert,
            "refnum": refnum,
        }
        if nhl:
            slim["nhl"] = True
        if nara:
            slim["nara"] = nara
        if geom.get("coordinates"):
            geom = {**geom, "coordinates": _round_coords(geom["coordinates"])}
        features.append({"type": "Feature", "properties": slim, "geometry": geom})

    return {"type": "FeatureCollection", "features": features}


def fetch_heritage(bbox, cache):
    """Fetch and merge OSM historic structures + NPS NRHP into one layer."""
    hist = fetch_historic(bbox, cache)
    nrhp = fetch_nrhp(bbox, cache)
    for f in hist.get("features", []):
        f["properties"]["_source"] = "osm"
    for f in nrhp.get("features", []):
        f["properties"]["_source"] = "nrhp"
    merged = hist.get("features", []) + nrhp.get("features", [])
    return {"type": "FeatureCollection", "features": merged}


# ─── Ecological layers ─────────────────────────────────────────────

def fetch_critical_wildlife(bbox, cache):
    """Fetch critical/protected wildlife areas from OSM."""
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:300];\n"
        f"(\n"
        f'  relation["boundary"="protected_area"]["protect_class"~"^(1|1a|1b|2|3|4)$"]({bb});\n'
        f'  way["boundary"="protected_area"]["protect_class"~"^(1|1a|1b|2|3|4)$"]({bb});\n'
        f'  node["natural"="bird_hide"]({bb});\n'
        f'  node["leisure"="bird_hide"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "critical_wildlife_v3",
                        ["name", "protect_class", "protection_title",
                         "operator", "designation", "website",
                         "wikipedia", "wikidata", "opening_hours",
                         "ownership", "protected_area"])


def fetch_nerrs(bbox, cache):
    """Fetch National Estuarine Research Reserve boundaries from OSM."""
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:300];\n"
        f"(\n"
        f'  relation["boundary"="protected_area"]["name"~"Estuarine Research Reserve|NERR",i]({bb});\n'
        f'  way["boundary"="protected_area"]["name"~"Estuarine Research Reserve|NERR",i]({bb});\n'
        f'  relation["name"~"Apalachicola.*Reserve",i]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "nerrs_v1",
                        ["name", "protect_class", "protection_title"])


INAT_API_URL = "https://api.inaturalist.org/v1"


def fetch_inat_rare(bbox, cache):
    """Fetch threatened/rare species observations from iNaturalist."""
    ck = "inat_rare_v1"
    if ck in cache:
        log.info("    [cached] iNat rare species")
        return cache[ck]

    s, w, n, e = bbox
    features = []
    try:
        r = requests.get(
            f"{INAT_API_URL}/observations",
            params={
                "nelat": n, "nelng": e, "swlat": s, "swlng": w,
                "threatened": "true", "quality_grade": "research",
                "per_page": 200, "order": "desc", "order_by": "observed_on",
            },
            headers=HEADERS,
            timeout=120,
        )
        r.raise_for_status()
        for obs in r.json().get("results", []):
            taxon = obs.get("taxon") or {}
            loc = obs.get("location")
            if not loc or not taxon.get("name"):
                continue
            lat_s, lng_s = loc.split(",")
            cs = taxon.get("conservation_status") or {}
            features.append({
                "type": "Feature",
                "properties": {
                    "name": taxon.get("preferred_common_name", taxon["name"]),
                    "sciName": taxon["name"],
                    "status": cs.get("status_name", ""),
                    "iucn": cs.get("iucn", ""),
                    "observedOn": obs.get("observed_on", ""),
                    "uri": obs.get("uri", ""),
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(float(lng_s), 5), round(float(lat_s), 5)],
                },
            })
    except Exception as exc:
        log.warning("    iNat rare species query failed: %s", exc)

    gj = {"type": "FeatureCollection", "features": features}
    cache[ck] = gj
    return gj


# ─── eBird API ────────────────────────────────────────────────────

def fetch_hotspots(bbox, api_key, cache):
    if "hotspots" in cache:
        log.info("    [cached] hotspots")
        raw = cache["hotspots"]
    else:
        pts = grid_points(bbox, step_km=70)
        seen, raw = set(), []
        log.info("    Querying eBird hotspots (%d grid pts)...", len(pts))
        for lat, lng in pts:
            try:
                r = requests.get(
                    f"{EBIRD_API}/ref/hotspot/geo",
                    params={"lat": lat, "lng": lng, "dist": 50, "fmt": "json"},
                    headers={"X-eBirdApiToken": api_key, **HEADERS},
                    timeout=30,
                )
                r.raise_for_status()
                for h in r.json():
                    lid = h.get("locId", "")
                    if lid and lid not in seen:
                        seen.add(lid)
                        raw.append(h)
                time.sleep(0.3)
            except Exception as exc:
                log.warning("    Hotspot query at %.2f,%.2f: %s", lat, lng, exc)
        cache["hotspots"] = raw

    features = []
    for h in raw:
        features.append({
            "type": "Feature",
            "properties": {
                "name": h.get("locName", ""),
                "numSpecies": h.get("numSpeciesAllTime", 0),
                "latestObs": h.get("latestObsDt", ""),
            },
            "geometry": {
                "type": "Point",
                "coordinates": [round(h.get("lng", 0), 5),
                                round(h.get("lat", 0), 5)],
            },
        })
    return {"type": "FeatureCollection", "features": features}


def fetch_ebird_obs(bbox, api_key, back, cache):
    ck = f"ebird_obs_{back}"
    if ck in cache:
        log.info("    [cached] ebird observations")
        raw = cache[ck]
    else:
        pts = grid_points(bbox, step_km=70)
        seen, raw = set(), []
        log.info("    Querying eBird obs (%d grid pts, back=%d)...", len(pts), back)
        for lat, lng in pts:
            try:
                r = requests.get(
                    f"{EBIRD_API}/data/obs/geo/recent",
                    params={"lat": lat, "lng": lng, "dist": 50,
                            "back": back, "includeProvisional": "true",
                            "maxResults": 10000},
                    headers={"X-eBirdApiToken": api_key, **HEADERS},
                    timeout=60,
                )
                r.raise_for_status()
                for obs in r.json():
                    key = (obs.get("speciesCode", ""), obs.get("locId", ""))
                    if key not in seen:
                        seen.add(key)
                        raw.append(obs)
                time.sleep(0.5)
            except Exception as exc:
                log.warning("    Obs query at %.2f,%.2f: %s", lat, lng, exc)
        cache[ck] = raw

    loc_agg: dict[str, dict] = {}
    for obs in raw:
        loc = obs.get("locId", "")
        if not loc:
            continue
        if loc not in loc_agg:
            loc_agg[loc] = {
                "locName": obs.get("locName", ""),
                "lat": obs.get("lat", 0),
                "lng": obs.get("lng", 0),
                "latestDate": obs.get("obsDt", ""),
                "species": {},
            }
        entry = loc_agg[loc]
        if obs.get("obsDt", "") > entry["latestDate"]:
            entry["latestDate"] = obs["obsDt"]
        sp = obs.get("comName", "Unknown")
        sp_key = obs.get("speciesCode", sp)
        if sp_key not in entry["species"]:
            entry["species"][sp_key] = {
                "species": sp,
                "sciName": obs.get("sciName", ""),
                "howMany": obs.get("howMany") or 1,
                "obsDt": obs.get("obsDt", ""),
                "subId": obs.get("subId", ""),
            }
        else:
            prev = entry["species"][sp_key]
            prev["howMany"] = max(prev["howMany"], obs.get("howMany") or 1)
            if obs.get("obsDt", "") > prev.get("obsDt", ""):
                prev["obsDt"] = obs["obsDt"]
                if obs.get("subId"):
                    prev["subId"] = obs["subId"]

    features = []
    for loc_id, rec in loc_agg.items():
        sp_list = sorted(rec["species"].values(), key=lambda s: s["species"])
        features.append({
            "type": "Feature",
            "properties": {
                "locName": rec["locName"],
                "latestDate": rec["latestDate"],
                "locId": loc_id,
                "species_list": sp_list,
            },
            "geometry": {
                "type": "Point",
                "coordinates": [round(rec["lng"], 5), round(rec["lat"], 5)],
            },
        })
    return {"type": "FeatureCollection", "features": features}


# ─── Leaflet HTML builder ────────────────────────────────────────

MAP_CSS = """
#leaflet-map{height:calc(100vh - 20px);width:100%;z-index:1;min-width:0}
.layout{max-width:none!important}
@media(min-width:1400px){.main{max-width:none;padding-right:60px}}
.panel:not(.active){display:none!important;overflow:hidden;height:0}
.map-layer-toggle{display:flex;align-items:center;gap:8px;padding:5px 16px;font-size:12px;cursor:pointer;user-select:none}
.map-layer-toggle:hover{background:rgba(0,0,0,.04)}
.map-layer-toggle input[type=checkbox]{margin:0;accent-color:var(--accent)}
.map-layer-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.map-layer-label{flex:1}
.map-layer-count{color:var(--muted);font-size:11px;min-width:28px;text-align:right}
.leaflet-popup-content{font-family:'IBM Plex Sans',sans-serif;font-size:13px;line-height:1.4}
.leaflet-popup-content b{font-weight:600}
.popup-species{color:#1A6B3A;font-weight:500}
.popup-meta{color:#707070;font-size:11px}
.leaflet-control-layers-toggle{background-image:none!important;background-size:0!important;display:flex!important;align-items:center!important;justify-content:center!important;width:36px!important;height:36px!important;padding:0!important;margin:0!important}
.leaflet-control-layers-toggle svg{width:20px;height:20px;display:block}
.leaflet-control-layers{border-radius:6px!important;box-shadow:0 2px 8px rgba(0,0,0,.18)!important}
"""

HEAD_CDN = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none';"
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net;"
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com https://cdn.jsdelivr.net;"
    "img-src 'self' data: https://cdn.download.ams.birds.cornell.edu https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org https://*.tile.opentopomap.org https://server.arcgisonline.com https://tiles.arcgis.com https://gis.charttools.noaa.gov https://tiledimageservices.arcgis.com https://unpkg.com;"
    "font-src https://fonts.gstatic.com;"
    "media-src https://cdn.download.ams.birds.cornell.edu;"
    "connect-src https://tiledimageservices.arcgis.com;"
    '"/>\n'
    '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"'
    ' integrity="sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H"'
    ' crossorigin="anonymous"/>\n'
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"'
    ' integrity="sha384-pmjIAcz2bAn0xukfxADbZIb3t8oRT9Sv0rvO+BR5Csr6Dhqq+nZs59P0pPKQJkEV"'
    ' crossorigin="anonymous"/>\n'
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"'
    ' integrity="sha384-wgw+aLYNQ7dlhK47ZPK7FRACiq7ROZwgFNg0m04avm4CaXS+Z9Y7nMu8yNjBKYC+"'
    ' crossorigin="anonymous"/>\n'
    '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"'
    ' integrity="sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH"'
    ' crossorigin="anonymous"></' + 'script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"'
    ' integrity="sha384-eXVCORTRlv4FUUgS/xmOyr66XBVraen8ATNLMESp92FKXLAMiKkerixTiBvXriZr"'
    ' crossorigin="anonymous"></' + 'script>\n'
)

MAP_JS_TEMPLATE = r"""
var _map=null,_mapLayers={};
function initMap(){
  if(_map){_map.invalidateSize();return;}
  _map=L.map('leaflet-map',{zoomControl:true}).setView([__CENTER_LAT__,__CENTER_LNG__],9);
  var voyager=L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',{
    attribution:'&copy; <a href="https://carto.com/">CARTO</a>',maxZoom:19,subdomains:'abcd'});
  var minimal=L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',{
    attribution:'CartoDB',maxZoom:19,subdomains:'abcd'});
  var osm=L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
    attribution:'&copy; OpenStreetMap',maxZoom:19});
  var topo=L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{
    attribution:'OpenTopoMap',maxZoom:17});
  var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{
    attribution:'Esri World Imagery',maxZoom:19});
  voyager.addTo(_map);
  var lc=L.control.layers({'Voyager':voyager,'Minimal':minimal,'Street':osm,'Topo':topo,'Satellite':sat},null,{collapsed:true}).addTo(_map);
  var tog=document.querySelector('.leaflet-control-layers-toggle');
  if(tog){tog.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="#555" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 22 8.5 12 15 2 8.5"/><polyline points="2 12 12 18.5 22 12"/><polyline points="2 15.5 12 22 22 15.5"/></svg>';}
  L.control.scale().addTo(_map);

  var areaPane=_map.createPane('areas');areaPane.style.zIndex=350;
  var markerPane=_map.createPane('markers');markerPane.style.zIndex=450;

  function ps(c,d){return function(){return{color:c,weight:2,opacity:.8,fillOpacity:.15,dashArray:d||'',pane:'areas'};};}
  function cm(c,r){return function(f,ll){return L.circleMarker(ll,{radius:r||5,fillColor:c,color:'#333',weight:1,fillOpacity:.85,pane:'markers'});};}
  function bp(ly,fn){ly.eachLayer(function(l){var p=l.feature&&l.feature.properties;if(p)l.bindPopup(fn(p));});}
  function nm(p){return '<b>'+(p.name||'Unnamed')+'</b>';}

  _mapLayers.hiking=L.geoJSON(mapData_hiking,{style:ps('#D4820F','8 4')});
  bp(_mapLayers.hiking,function(p){return '<b>'+(p.name||'Trail')+'</b>'+(p.surface?'<br>Surface: '+p.surface:'');});

  _mapLayers.bike=L.geoJSON(mapData_bike,{style:ps('#2E6B94')});
  bp(_mapLayers.bike,function(p){return '<b>'+(p.name||'Bike Route')+'</b>';});

  function beachStyle(color){return function(){return{color:color,weight:3,opacity:.8,fillOpacity:.2,pane:'areas'};};}
  function beachPopup(f,layer){
    var p=f.properties;
    var s='<b>'+(p.name||'Beach')+'</b>';
    if(p.access)s+='<br>Access: '+(p.access==='yes'||p.access==='public'?'\u2705 Public':'\u{1F512} Private');
    if(p.vehicles)s+='<br>Vehicles: '+(p.vehicles==='yes'?'\u{1F697} Allowed':'\u{1F6AB} Not allowed');
    layer.bindPopup(s);
  }
  _mapLayers.beaches_public=L.geoJSON(mapData_beaches,{
    filter:function(f){var a=f.properties.access;return !a||a==='yes'||a==='public';},
    style:beachStyle('__CLR_BEACHES_PUBLIC__'),onEachFeature:beachPopup,pointToLayer:cm('__CLR_BEACHES_PUBLIC__',6)});
  _mapLayers.beaches_private=L.geoJSON(mapData_beaches,{
    filter:function(f){var a=f.properties.access;return a==='private'||a==='no';},
    style:beachStyle('__CLR_BEACHES_PRIVATE__'),onEachFeature:beachPopup,pointToLayer:cm('__CLR_BEACHES_PRIVATE__',6)});

  _mapLayers.state_parks=L.geoJSON(mapData_state_parks,{style:ps('#3A7D50')});
  bp(_mapLayers.state_parks,function(p){return '<b>'+(p.name||'State Park')+'</b>'+(p.protection_title?'<br>'+p.protection_title:'');});

  _mapLayers.wilderness=L.geoJSON(mapData_wilderness,{style:ps('#4A6A3A')});
  bp(_mapLayers.wilderness,nm);

  _mapLayers.refuges=L.geoJSON(mapData_refuges,{style:ps('#2A7A7A')});
  bp(_mapLayers.refuges,function(p){return '<b>'+(p.name||'Wildlife Refuge')+'</b>';});

  _mapLayers.forests=L.geoJSON(mapData_forests,{style:ps('#2D5A1E')});
  bp(_mapLayers.forests,nm);

  _mapLayers.lighthouses=L.geoJSON(mapData_lighthouses,{
    pointToLayer:function(f,ll){
      return L.marker(ll,{pane:'markers',icon:L.divIcon({className:'',
        html:'<svg width="20" height="20" viewBox="0 0 20 20"><polygon points="10,1 13,8 10,6 7,8" fill="#C0392B"/><rect x="8" y="8" width="4" height="10" fill="#C0392B"/></svg>',
        iconSize:[20,20],iconAnchor:[10,18]})});
    },
    style:ps('#C0392B')
  });
  bp(_mapLayers.lighthouses,function(p){return '<b>'+(p.name||'Lighthouse')+'</b>'+(p.start_date?'<br>Built: '+p.start_date:'');});

  _mapLayers.heritage=L.geoJSON(mapData_heritage,{
    pointToLayer:function(f,ll){
      var p=f.properties;
      if(p._source==='nrhp'){
        var nhl=p.nhl;
        return L.circleMarker(ll,{radius:nhl?8:5,fillColor:nhl?'#FFD700':'#9B2335',
          color:nhl?'#8B6914':'#333',weight:nhl?2:1,fillOpacity:.9,pane:'markers'});
      }
      return L.circleMarker(ll,{radius:5,fillColor:'#7A5230',color:'#333',weight:1,fillOpacity:.85,pane:'markers'});
    },
    style:function(f){
      var p=f.properties;
      if(p._source==='nrhp')return{color:'#9B2335',weight:2,opacity:.8,fillOpacity:.15,dashArray:'4 4',pane:'areas'};
      return{color:'#7A5230',weight:2,opacity:.8,fillOpacity:.15,pane:'areas'};
    },
    onEachFeature:function(f,layer){
      var p=f.properties,s='<div style="max-width:300px">';
      if(p._source==='nrhp'){
        s+='<b>'+(p.name||'NRHP Site')+'</b>';
        if(p.nhl)s+=' <span style="color:#DAA520;font-weight:700">\u2605 National Historic Landmark</span>';
        if(p.type)s+='<br><span style="font-size:11px;color:#666;text-transform:capitalize">'+p.type+'</span>';
        if(p.address)s+='<div style="font-size:11px;color:#444;margin-top:3px">'+p.address+'</div>';
        if(p.city||p.county){s+='<div style="font-size:11px;color:#444">';if(p.city)s+=p.city;if(p.city&&p.county)s+=', ';if(p.county)s+=p.county+' Co.';if(p.state)s+=', '+p.state;s+='</div>';}
        if(p.listed)s+='<div style="font-size:11px;margin-top:3px"><span style="background:#f0e8f5;padding:1px 5px;border-radius:3px">Listed '+p.listed+'</span></div>';
        if(p.nara)s+='<div style="margin-top:5px;font-size:11px"><a href="'+p.nara+'" target="_blank">NARA Record</a></div>';
        if(p.refnum)s+='<div style="font-size:9px;color:#999;margin-top:2px">NRIS #'+p.refnum+'</div>';
      } else {
        s+='<b>'+(p.name||'Historic Site')+'</b>';
        var tags=[];
        if(p.historic)tags.push(p.historic);
        if(p.tourism)tags.push(p.tourism);
        if(tags.length)s+='<br><span style="font-size:11px;color:#666;text-transform:capitalize">'+tags.join(' \u00b7 ')+'</span>';
        if(p.description)s+='<div style="font-size:11px;color:#444;margin-top:3px;line-height:1.4">'+p.description+'</div>';
        if(p.start_date)s+='<div style="font-size:11px;margin-top:3px"><span style="background:#f5f0e8;padding:1px 5px;border-radius:3px">Est. '+p.start_date+'</span></div>';
        if(p.operator)s+='<div style="font-size:11px;color:#555;margin-top:2px">'+p.operator+'</div>';
        var links=[];
        if(p.website)links.push('<a href="'+p.website+'" target="_blank">Website</a>');
        if(p.wikipedia)links.push('<a href="https://en.wikipedia.org/wiki/'+encodeURIComponent(p.wikipedia)+'" target="_blank">Wikipedia</a>');
        if(links.length)s+='<div style="margin-top:5px;font-size:11px;display:flex;gap:8px">'+links.join('')+'</div>';
      }
      s+='</div>';
      layer.bindPopup(s,{maxWidth:320});
    }
  });

  _mapLayers.critical_wildlife=L.geoJSON(mapData_critical_wildlife,{
    style:ps('#C62828','4 4'),
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:7,fillColor:'#C62828',color:'#fff',weight:2,fillOpacity:.9,pane:'markers'});}
  });
  bp(_mapLayers.critical_wildlife,function(p){var s='<b style="font-size:14px">'+(p.name||'Protected Area')+'</b>';if(p.protection_title)s+='<br><span style="font-size:12px;color:#444">'+p.protection_title+'</span>';if(p.protect_class){var cls={'1':'Strict Nature Reserve','1a':'Strict Nature Reserve','1b':'Wilderness Area','2':'National Park','3':'Natural Monument','4':'Habitat/Species Management'};s+='<br><span style="font-size:11px;color:#555">IUCN Category '+(cls[p.protect_class]||p.protect_class)+' (Class '+p.protect_class+')</span>';}if(p.operator)s+='<br><span style="font-size:11px;color:#666">Managed by '+p.operator+'</span>';if(p.ownership)s+='<br><span style="font-size:11px;color:#666">Ownership: '+p.ownership+'</span>';if(p.opening_hours)s+='<br><span style="font-size:11px;color:#666">Hours: '+p.opening_hours+'</span>';var links=[];if(p.website)links.push('<a href="'+p.website+'" target="_blank" rel="noopener" style="font-size:11px;color:#2E6B94">Official site &#8599;</a>');if(p.wikipedia){var wp=p.wikipedia.replace(/^en:/,'');links.push('<a href="https://en.wikipedia.org/wiki/'+encodeURIComponent(wp)+'" target="_blank" rel="noopener" style="font-size:11px;color:#2E6B94">Wikipedia &#8599;</a>');}else if(p.wikidata){links.push('<a href="https://www.wikidata.org/wiki/'+p.wikidata+'" target="_blank" rel="noopener" style="font-size:11px;color:#2E6B94">Wikidata &#8599;</a>');}if(links.length)s+='<br><div style="margin-top:4px;display:flex;gap:10px">'+links.join('')+'</div>';return s;});

  _mapLayers.nerrs=L.geoJSON(mapData_nerrs,{style:ps('#005F73','4 6')});
  bp(_mapLayers.nerrs,function(p){return '<b>'+(p.name||'Estuarine Reserve')+'</b>'+(p.protection_title?'<br>'+p.protection_title:'');});

  _mapLayers.inat_rare=L.geoJSON(mapData_inat_rare,{
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:6,fillColor:'#D4380D',color:'#fff',weight:2,fillOpacity:.9,pane:'markers'});}
  });
  bp(_mapLayers.inat_rare,function(p){var s='<b>'+(p.name||p.sciName)+'</b>';if(p.sciName)s+='<br><i style="color:#666">'+p.sciName+'</i>';if(p.status)s+='<br><span style="color:#D4380D;font-weight:600;font-size:11px">'+p.status+'</span>';if(p.observedOn)s+='<br><span class="popup-meta">Observed: '+p.observedOn+'</span>';if(p.uri)s+='<br><a href="'+p.uri+'" target="_blank" style="font-size:11px">View on iNaturalist</a>';return s;});

  _mapLayers.hotspots=L.geoJSON(mapData_hotspots,{
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:7,fillColor:'#8B4513',color:'#fff',weight:2,fillOpacity:.9,pane:'markers'});}
  });
  bp(_mapLayers.hotspots,function(p){return '<b>'+p.name+'</b><br><span class="popup-meta">'+p.numSpecies+' species all-time'+(p.latestObs?'<br>Latest: '+p.latestObs:'')+'</span>';});

  var obsCluster=L.markerClusterGroup({maxClusterRadius:40,showCoverageOnHover:false,
    iconCreateFunction:function(c){var n=c.getChildCount(),sz=n<20?'small':n<100?'medium':'large';
      return L.divIcon({html:'<div><span>'+n+'</span></div>',className:'marker-cluster marker-cluster-'+sz,iconSize:L.point(40,40)});}});
  L.geoJSON(mapData_ebird_obs,{
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:4,fillColor:'#1A6B3A',color:'#fff',weight:1,fillOpacity:.8,pane:'markers'});},
    onEachFeature:function(f,layer){
      var p=f.properties;
      if(p.species_list){
        var list=p.species_list;
        var locLink=p.locId?'<a href="https://ebird.org/hotspot/'+p.locId+'" target="_blank" style="font-size:11px;color:#1A6B3A">View on eBird</a>':'';
        var s='<div style="max-width:320px"><b>'+(p.locName||'Observation')+'</b><br><span style="font-size:11px;color:#555">'+list.length+' species \u00b7 Latest: '+(p.latestDate||'')+'</span>';
        if(locLink)s+=' \u00b7 '+locLink;
        s+='<div style="max-height:220px;overflow-y:auto;margin-top:4px">';
        var show=Math.min(list.length,12);
        for(var i=0;i<show;i++){
          var sp=list[i];
          var spName=sp.subId?'<a href="https://ebird.org/checklist/'+sp.subId+'" target="_blank" class="popup-species" style="text-decoration:none">'+sp.species+'</a>':'<span class="popup-species">'+sp.species+'</span>';
          s+='<div style="font-size:11px;padding:2px 0;border-bottom:1px solid #eee">'+spName+' <i style="color:#888">'+sp.sciName+'</i>'+(sp.howMany>1?' ('+sp.howMany+')':'')+(sp.obsDt?'<span style="float:right;color:#999;font-size:10px">'+sp.obsDt.split(' ')[0]+'</span>':'')+'</div>';
        }
        if(list.length>12)s+='<div style="font-size:11px;color:#888;padding:2px 0">+ '+(list.length-12)+' more species</div>';
        s+='</div></div>';
        layer.bindPopup(s,{maxWidth:340,maxHeight:320});
      } else {
        layer.bindPopup('<span class="popup-species">'+(p.species||'')+'</span><br><i>'+(p.sciName||'')+'</i><br><span class="popup-meta">'+(p.locName||'')+'<br>'+(p.obsDt||'')+'</span>');
      }
    }
  }).addTo(obsCluster);
  _mapLayers.ebird_obs=obsCluster;

  _mapLayers.bathymetry=L.tileLayer('https://tiles.arcgis.com/tiles/C8EMgrsFcRFL6LrL/arcgis/rest/services/Gulf_Wide_Bathymetry/MapServer/tile/{z}/{y}/{x}',{opacity:0.5,maxZoom:10,attribution:'NOAA NCEI Gulf Bathymetry'});
  _mapLayers.noaa_charts=L.tileLayer('https://gis.charttools.noaa.gov/arcgis/rest/services/MarineChart_Services/NOAACharts/MapServer/tile/{z}/{y}/{x}',{opacity:0.6,attribution:'NOAA Chart Display'});
  _mapLayers.currents=L.tileLayer.wms('https://tiledimageservices.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/annual_drifter_mean_v3/ImageServer/WMSServer',{layers:'0',format:'image/png',transparent:true,opacity:0.6,attribution:'NOAA/AOML Ocean Currents'});

  var defaults=__DEFAULTS_OBJ__;
  for(var k in _mapLayers){if(defaults[k])_mapLayers[k].addTo(_map);}
  _map.fitBounds([[__SOUTH__,__WEST__],[__NORTH__,__EAST__]]);

  L.marker([30.3307,-86.1650],{icon:L.divIcon({className:'base-star',html:'<div style="text-align:center"><span style="font-size:22px;color:#D4380D;text-shadow:0 0 3px #fff">&#9733;</span><div style="font-size:9px;font-weight:600;color:#333;white-space:nowrap;margin-top:-2px;font-family:\'IBM Plex Sans\',sans-serif">Base Camp</div></div>',iconSize:[60,36],iconAnchor:[30,18]}),zIndexOffset:1000}).addTo(_map);

  setTimeout(function(){_map.invalidateSize();},250);
}
function toggleMapLayer(key,on){
  if(!_map||!_mapLayers[key])return;
  if(on)_mapLayers[key].addTo(_map);else _map.removeLayer(_mapLayers[key]);
}
"""

SWITCH_JS_TEMPLATE = ""


def build_parts(layers: dict, bbox: tuple) -> dict:
    s, w, n, e = bbox
    clat, clng = (s + n) / 2, (w + e) / 2

    beaches_gj = layers.get("beaches", {"type": "FeatureCollection", "features": []})
    beach_features = beaches_gj.get("features", [])

    def _beach_count(pred):
        return sum(1 for f in beach_features if pred(f.get("properties", {})))

    beach_counts = {
        "beaches_public": _beach_count(lambda p: p.get("access", "") in ("", "yes", "public")),
        "beaches_private": _beach_count(lambda p: p.get("access", "") in ("private", "no")),
    }

    nav_items = []
    for key, ld in LAYER_DEFS.items():
        is_tile = ld.get("tile", False)
        chk = "checked" if ld["on"] else ""
        if is_tile:
            cnt_str = ""
        elif key in beach_counts:
            cnt_str = str(beach_counts[key])
            if int(cnt_str) == 0 and not ld["on"]:
                continue
        else:
            cnt = len(layers.get(key, {}).get("features", []))
            if cnt == 0 and not ld["on"]:
                continue
            cnt_str = str(cnt)
        nav_items.append(
            f'<label class="map-layer-toggle">'
            f'<input type="checkbox" {chk} onchange="toggleMapLayer(\'{key}\',this.checked)">'
            f'<span class="map-layer-dot" style="background:{ld["color"]}"></span>'
            f'<span class="map-layer-label">{ld["label"]}</span>'
            f'<span class="map-layer-count">{cnt_str}</span>'
            f'</label>'
        )
    nav_html = (
        '<div class="nav-links" id="nav-map" style="display:none">\n'
        + "\n".join(nav_items)
        + "\n</div>"
    )

    panel_html = (
        '<div class="panel" id="panel-map"><div id="leaflet-map"></div>'
        '<div style="padding:8px 16px;font-size:9px;color:#999;line-height:1.6">'
        'Map data: OpenStreetMap, NPS National Register of Historic Places, '
        'eBird (Cornell Lab of Ornithology), iNaturalist, '
        'OSM Protected Areas, NOAA NERR. '
        'Tiles: CartoDB, OpenTopoMap, Esri World Imagery.</div></div>'
    )

    data_keys = set()
    for key in LAYER_DEFS:
        if key.startswith("beaches_"):
            data_keys.add("beaches")
        else:
            data_keys.add(key)

    data_lines = []
    for key in data_keys:
        gj = layers.get(key, {"type": "FeatureCollection", "features": []})
        data_lines.append(f"var mapData_{key}={json.dumps(gj, separators=(',', ':'))};")
    data_script = "\n".join(data_lines)

    defaults_obj = json.dumps({k: 1 for k, v in LAYER_DEFS.items() if v["on"]})
    init_js = (
        MAP_JS_TEMPLATE
        .replace("__CENTER_LAT__", str(round(clat, 4)))
        .replace("__CENTER_LNG__", str(round(clng, 4)))
        .replace("__SOUTH__", str(s))
        .replace("__WEST__", str(w))
        .replace("__NORTH__", str(n))
        .replace("__EAST__", str(e))
        .replace("__DEFAULTS_OBJ__", defaults_obj)
        .replace("__CLR_BEACHES_PUBLIC__", LAYER_DEFS["beaches_public"]["color"])
        .replace("__CLR_BEACHES_PRIVATE__", LAYER_DEFS["beaches_private"]["color"])
    )

    return {
        "nav_html": nav_html,
        "panel_html": panel_html,
        "data_script": data_script,
        "init_js": init_js,
    }


# ─── Inject into existing index.html ─────────────────────────────

def inject_map_tab(target: Path, parts: dict):
    backup = target.with_name(".index_pre_map.html")

    if backup.exists():
        log.info("Restoring clean backup before re-injection")
        html = backup.read_text(encoding="utf-8")
    else:
        html = target.read_text(encoding="utf-8")
        backup.write_text(html, encoding="utf-8")
        log.info("Saved clean backup to %s", backup.name)

    html = html.replace("</style>", MAP_CSS + "</style>", 1)

    html = html.replace("</head>", HEAD_CDN + "</head>", 1)

    # Insert Map button at end of mode-toggle div
    if 'id="btn-map"' not in html:
        map_btn = '<button class="mode-btn" id="btn-map" onclick="switchMode(\'map\')">Map</button>'
        # Find the closing </div> of the mode-toggle by matching the last button before it
        html = re.sub(
            r'(class="mode-toggle">[^<]*(?:<button[^>]*>.*?</button>\s*)+)(</div>)',
            lambda m: m.group(1) + map_btn + m.group(2),
            html,
            count=1,
            flags=re.DOTALL,
        )

    html = html.replace("</nav>", parts["nav_html"] + "\n</nav>", 1)

    html = html.replace("</main>", parts["panel_html"] + "\n</main>", 1)

    # Inject map data and init function before the DOMContentLoaded handler.
    markers = [
        "document.addEventListener('DOMContentLoaded',initAprMay)",
        "document.addEventListener('DOMContentLoaded', initAprMay)",
        "document.addEventListener('DOMContentLoaded',function(){",
        "document.addEventListener('DOMContentLoaded',function (){",
    ]
    idx = -1
    for marker in markers:
        idx = html.find(marker)
        if idx >= 0:
            break
    if idx >= 0:
        map_inject = parts["data_script"] + "\n" + parts["init_js"] + "\n"
        html = html[:idx] + map_inject + "\n" + html[idx:]

    target.write_text(html, encoding="utf-8")
    log.info("Wrote %s (%d KB)", target, len(html) // 1024)


# ─── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Build an interactive Leaflet trail map and inject it "
                    "as a third tab into an existing checklist page.",
    )
    parser.add_argument("--bbox", required=True,
                        help="Bounding box S,W,N,E")
    parser.add_argument("--ebird-key",
                        default=os.environ.get("EBIRD_API_KEY", ""),
                        help="eBird API key")
    parser.add_argument("--back", type=int, default=30,
                        help="eBird lookback days (default 30)")
    parser.add_argument("--target", type=Path, required=True,
                        help="Existing index.html to inject Map tab into")
    parser.add_argument("--output", type=Path, default=None,
                        help="Cache directory (default: target parent)")
    args = parser.parse_args()

    bbox_str = BBOX_PRESETS.get(args.bbox, args.bbox)
    bbox = parse_bbox(bbox_str)
    target = args.target.resolve()
    if not target.exists():
        parser.error(f"Target not found: {target}")

    output_dir = (args.output or target.parent).resolve()
    cache_path = output_dir / ".map_cache.json"
    cache = load_cache(cache_path)

    log.info("=" * 60)
    log.info("Gulf Islands Trail Map Builder")
    log.info("  bbox  S=%.2f  W=%.2f  N=%.2f  E=%.2f", *bbox)
    log.info("  target  %s", target)
    log.info("=" * 60)

    # ── OSM layers ──
    log.info("\nStep 1/3  Fetching OpenStreetMap data...")
    layers = {}

    fetchers = [
        ("hiking",            fetch_hiking,            "Hiking trails"),
        ("bike",              fetch_bike,              "Bike routes"),
        ("beaches",           fetch_beaches,           "Beaches"),
        ("state_parks",       fetch_state_parks,       "State parks"),
        ("wilderness",        fetch_wilderness,        "Wilderness areas"),
        ("refuges",           fetch_refuges,           "Wildlife refuges"),
        ("forests",           fetch_forests,            "State/nat'l forests"),
        ("lighthouses",       fetch_lighthouses,       "Lighthouses"),
        ("heritage",          fetch_heritage,          "Heritage sites"),
        ("critical_wildlife", fetch_critical_wildlife, "Protected wildlife areas"),
        ("nerrs",             fetch_nerrs,             "Estuarine reserves"),
        ("inat_rare",         fetch_inat_rare,         "Rare species (iNat)"),
    ]
    total_osm = len(fetchers)
    for i, (key, fn, label) in enumerate(fetchers, 1):
        log.info("  [%d/%d] %s", i, total_osm, label)
        layers[key] = fn(bbox, cache)
        save_cache(cache_path, cache)

    # ── eBird layers ──
    log.info("\nStep 2/3  Fetching eBird data...")
    has_cached_ebird = "hotspots" in cache
    if args.ebird_key or has_cached_ebird:
        if not args.ebird_key:
            log.info("  No eBird key — using cached data only")
        log.info("  [11/12] Birding hotspots")
        layers["hotspots"] = fetch_hotspots(bbox, args.ebird_key or "", cache)
        save_cache(cache_path, cache)

        log.info("  [12/12] Recent observations (%d days)", args.back)
        layers["ebird_obs"] = fetch_ebird_obs(bbox, args.ebird_key or "",
                                              args.back, cache)
        save_cache(cache_path, cache)
    else:
        log.warning("  No eBird key and no cache — skipping bird layers")
        layers["hotspots"] = {"type": "FeatureCollection", "features": []}
        layers["ebird_obs"] = {"type": "FeatureCollection", "features": []}

    log.info("\nLayer summary:")
    for key, ld in LAYER_DEFS.items():
        cnt = len(layers.get(key, {}).get("features", []))
        log.info("  %-26s %5d features", ld["label"], cnt)

    # ── Build & inject ──
    log.info("\nStep 3/3  Injecting map tab into %s...", target.name)
    parts = build_parts(layers, bbox)
    inject_map_tab(target, parts)

    log.info("\n" + "=" * 60)
    log.info("Done!  Open %s and click the Map tab.", target)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
