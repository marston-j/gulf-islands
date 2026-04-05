#!/usr/bin/env python3
"""
Gulf Coast Trail Map Builder

Fetches geographic data from OpenStreetMap (Overpass API) and eBird,
then injects an interactive Leaflet.js map as a third tab into an
existing field checklist index.html.

Data layers (12):
  OSM  — Hiking trails, bike routes, beaches, state parks, wilderness,
         wildlife refuges, state/national forests, lighthouses, historic
  NPS  — National Register of Historic Places (points + districts)
  eBird — Birding hotspots, 30-day recent observations

Usage:
  python3 trail_map.py \\
    --bbox 29.9,-88.3,30.85,-85.7 \\
    --ebird-key YOUR_KEY \\
    --back 30 \\
    --target output/santa-rosa-beach-florida/index.html
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

LAYER_DEFS = {
    "hiking":      {"label": "Hiking Trails",       "color": "#D4820F", "on": True},
    "bike":        {"label": "Bike Routes",          "color": "#2E6B94", "on": True},
    "beaches":     {"label": "Beaches",              "color": "#D4A843", "on": False},
    "state_parks": {"label": "State Parks",          "color": "#3A7D50", "on": True},
    "wilderness":  {"label": "Wilderness Areas",     "color": "#4A6A3A", "on": False},
    "refuges":     {"label": "Wildlife Refuges",     "color": "#2A7A7A", "on": False},
    "forests":     {"label": "State / Nat'l Forests", "color": "#2D5A1E", "on": False},
    "lighthouses": {"label": "Lighthouses",          "color": "#C0392B", "on": True},
    "historic":    {"label": "Historic Structures",  "color": "#7A5230", "on": False},
    "nrhp":        {"label": "Nat'l Register Sites", "color": "#9B2335", "on": True},
    "hotspots":    {"label": "Birding Hotspots",     "color": "#8B4513", "on": True},
    "ebird_obs":   {"label": "eBird Obs (30 d)",     "color": "#1A6B3A", "on": False},
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
    return _osm_geojson(q, cache, "beaches", ["name", "surface", "access"])


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

    agg: dict[tuple, dict] = {}
    for obs in raw:
        sp = obs.get("comName", "Unknown")
        loc = obs.get("locId", "")
        key = (sp, loc)
        if key not in agg:
            agg[key] = {
                "species": sp,
                "sciName": obs.get("sciName", ""),
                "locName": obs.get("locName", ""),
                "lat": obs.get("lat", 0),
                "lng": obs.get("lng", 0),
                "obsDt": obs.get("obsDt", ""),
                "howMany": obs.get("howMany") or 1,
            }
        else:
            e = agg[key]
            if obs.get("obsDt", "") > e["obsDt"]:
                e["obsDt"] = obs["obsDt"]
            e["howMany"] = max(e["howMany"], obs.get("howMany") or 1)

    features = []
    for rec in agg.values():
        features.append({
            "type": "Feature",
            "properties": {
                "species": rec["species"],
                "sciName": rec["sciName"],
                "locName": rec["locName"],
                "obsDt": rec["obsDt"],
                "howMany": rec["howMany"],
            },
            "geometry": {
                "type": "Point",
                "coordinates": [round(rec["lng"], 5), round(rec["lat"], 5)],
            },
        })
    return {"type": "FeatureCollection", "features": features}


# ─── Leaflet HTML builder ────────────────────────────────────────

MAP_CSS = """
#leaflet-map{height:calc(100vh - 20px);width:100%;z-index:1}
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
"""

HEAD_CDN = (
    '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>\n'
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>\n'
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>\n'
    '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></' + 'script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></' + 'script>\n'
)

MAP_JS_TEMPLATE = r"""
var _map=null,_mapLayers={};
function initMap(){
  if(_map){_map.invalidateSize();return;}
  _map=L.map('leaflet-map',{zoomControl:true}).setView([__CENTER_LAT__,__CENTER_LNG__],9);
  var osm=L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
    attribution:'&copy; OpenStreetMap',maxZoom:19});
  var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{
    attribution:'Esri World Imagery',maxZoom:19});
  osm.addTo(_map);
  L.control.layers({'Street':osm,'Satellite':sat},null,{collapsed:true}).addTo(_map);
  L.control.scale().addTo(_map);

  function ps(c,d){return function(){return{color:c,weight:2,opacity:.8,fillOpacity:.15,dashArray:d||''};};}
  function cm(c,r){return function(f,ll){return L.circleMarker(ll,{radius:r||5,fillColor:c,color:'#333',weight:1,fillOpacity:.85});};}
  function bp(ly,fn){ly.eachLayer(function(l){var p=l.feature&&l.feature.properties;if(p)l.bindPopup(fn(p));});}
  function nm(p){return '<b>'+(p.name||'Unnamed')+'</b>';}

  _mapLayers.hiking=L.geoJSON(mapData_hiking,{style:ps('#D4820F','8 4')});
  bp(_mapLayers.hiking,function(p){return '<b>'+(p.name||'Trail')+'</b>'+(p.surface?'<br>Surface: '+p.surface:'');});

  _mapLayers.bike=L.geoJSON(mapData_bike,{style:ps('#2E6B94')});
  bp(_mapLayers.bike,function(p){return '<b>'+(p.name||'Bike Route')+'</b>';});

  _mapLayers.beaches=L.geoJSON(mapData_beaches,{style:ps('#D4A843'),pointToLayer:cm('#D4A843',6)});
  bp(_mapLayers.beaches,function(p){return '<b>'+(p.name||'Beach')+'</b>';});

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
      return L.marker(ll,{icon:L.divIcon({className:'',
        html:'<svg width="20" height="20" viewBox="0 0 20 20"><polygon points="10,1 13,8 10,6 7,8" fill="#C0392B"/><rect x="8" y="8" width="4" height="10" fill="#C0392B"/></svg>',
        iconSize:[20,20],iconAnchor:[10,18]})});
    },
    style:ps('#C0392B')
  });
  bp(_mapLayers.lighthouses,function(p){return '<b>'+(p.name||'Lighthouse')+'</b>'+(p.start_date?'<br>Built: '+p.start_date:'');});

  _mapLayers.historic=L.geoJSON(mapData_historic,{pointToLayer:cm('#7A5230',5),style:ps('#7A5230')});
  bp(_mapLayers.historic,function(p){var t=p.historic||p.tourism||'';var s='<b>'+(p.name||'Historic Site')+'</b>';if(t)s+='<br><i>'+t+'</i>';if(p.start_date)s+='<br>Est. '+p.start_date;if(p.operator)s+='<br>'+p.operator;if(p.website)s+='<br><a href="'+p.website+'" target="_blank">More info</a>';return s;});

  _mapLayers.nrhp=L.geoJSON(mapData_nrhp,{
    pointToLayer:function(f,ll){
      var p=f.properties,nhl=p.nhl;
      return L.circleMarker(ll,{radius:nhl?8:5,fillColor:nhl?'#FFD700':'#9B2335',
        color:nhl?'#8B6914':'#333',weight:nhl?2:1,fillOpacity:.9});
    },
    style:ps('#9B2335','4 4')
  });
  bp(_mapLayers.nrhp,function(p){
    var s='<b>'+(p.name||'NRHP Site')+'</b>';
    if(p.nhl)s+=' <span style="color:#DAA520;font-weight:700">★ NHL</span>';
    if(p.type)s+='<br><i>'+p.type+'</i>';
    if(p.address)s+='<br>'+p.address;
    if(p.city||p.county)s+='<br>'+(p.city||'')+(p.city&&p.county?', ':'')+
      (p.county?p.county+' Co.':'');
    if(p.listed)s+='<br>Listed: '+p.listed;
    if(p.nara)s+='<br><a href="'+p.nara+'" target="_blank">NARA record</a>';
    if(p.refnum)s+='<br><span style="color:#888;font-size:10px">NRIS #'+p.refnum+'</span>';
    return s;
  });

  _mapLayers.hotspots=L.geoJSON(mapData_hotspots,{
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:7,fillColor:'#8B4513',color:'#fff',weight:2,fillOpacity:.9});}
  });
  bp(_mapLayers.hotspots,function(p){return '<b>'+p.name+'</b><br><span class="popup-meta">'+p.numSpecies+' species all-time'+(p.latestObs?'<br>Latest: '+p.latestObs:'')+'</span>';});

  var obsCluster=L.markerClusterGroup({maxClusterRadius:40,showCoverageOnHover:false,
    iconCreateFunction:function(c){var n=c.getChildCount(),sz=n<20?'small':n<100?'medium':'large';
      return L.divIcon({html:'<div><span>'+n+'</span></div>',className:'marker-cluster marker-cluster-'+sz,iconSize:L.point(40,40)});}});
  L.geoJSON(mapData_ebird_obs,{
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:4,fillColor:'#1A6B3A',color:'#fff',weight:1,fillOpacity:.8});},
    onEachFeature:function(f,layer){var p=f.properties;
      layer.bindPopup('<span class="popup-species">'+p.species+'</span><br><i>'+p.sciName+'</i><br><span class="popup-meta">'+p.locName+'<br>'+p.obsDt+(p.howMany>1?' ('+p.howMany+')':'')+'</span>');}
  }).addTo(obsCluster);
  _mapLayers.ebird_obs=obsCluster;

  var defaults=__DEFAULTS_OBJ__;
  for(var k in _mapLayers){if(defaults[k])_mapLayers[k].addTo(_map);}
  _map.fitBounds([[__SOUTH__,__WEST__],[__NORTH__,__EAST__]]);
  setTimeout(function(){_map.invalidateSize();},250);
}
function toggleMapLayer(key,on){
  if(!_map||!_mapLayers[key])return;
  if(on)_mapLayers[key].addTo(_map);else _map.removeLayer(_mapLayers[key]);
}
"""

SWITCH_JS_TEMPLATE = """
function switchMode(mode){
  ['birds','plants','map'].forEach(function(m){
    var p=document.getElementById('panel-'+m);
    var n=document.getElementById('nav-'+m);
    var b=document.getElementById('btn-'+m);
    if(p)p.classList.toggle('active',m===mode);
    if(n)n.style.display=m===mode?'':'none';
    if(b)b.classList.toggle('active',m===mode);
  });
  var stats={birds:'__BCOUNT__ Bird Species',plants:'__PCOUNT__ Plant Species',map:'Interactive Trail Map'};
  document.getElementById('stat-text').textContent=stats[mode]||'';
  if(mode==='map'){initMap();}else{scrollTo({top:0});}
}
"""


def build_parts(layers: dict, bbox: tuple) -> dict:
    s, w, n, e = bbox
    clat, clng = (s + n) / 2, (w + e) / 2

    nav_items = []
    for key, ld in LAYER_DEFS.items():
        chk = "checked" if ld["on"] else ""
        cnt = len(layers.get(key, {}).get("features", []))
        nav_items.append(
            f'<label class="map-layer-toggle">'
            f'<input type="checkbox" {chk} onchange="toggleMapLayer(\'{key}\',this.checked)">'
            f'<span class="map-layer-dot" style="background:{ld["color"]}"></span>'
            f'<span class="map-layer-label">{ld["label"]}</span>'
            f'<span class="map-layer-count">{cnt}</span>'
            f'</label>'
        )
    nav_html = (
        '<div class="nav-links" id="nav-map" style="display:none">\n'
        + "\n".join(nav_items)
        + "\n</div>"
    )

    panel_html = '<div class="panel" id="panel-map"><div id="leaflet-map"></div></div>'

    data_lines = []
    for key in LAYER_DEFS:
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

    bird_count = "0"
    plant_count = "0"
    m = re.search(r"birds\?'(\d+)\s+Bird", html)
    if m:
        bird_count = m.group(1)
    m = re.search(r"[':]\s*'(\d+)\s+Plant", html)
    if m:
        plant_count = m.group(1)

    switch_js = (
        SWITCH_JS_TEMPLATE
        .replace("__BCOUNT__", bird_count)
        .replace("__PCOUNT__", plant_count)
    )

    html = html.replace("</style>", MAP_CSS + "</style>", 1)

    html = html.replace("</head>", HEAD_CDN + "</head>", 1)

    html = html.replace(
        """onclick="switchMode('plants')">Plants</button></div>""",
        """onclick="switchMode('plants')">Plants</button>"""
        """<button class="mode-btn" id="btn-map" onclick="switchMode('map')">Map</button></div>""",
    )

    html = html.replace("</nav>", parts["nav_html"] + "\n</nav>", 1)

    html = html.replace("</main>", parts["panel_html"] + "\n</main>", 1)

    new_script = (
        "<script>\n"
        + parts["data_script"] + "\n"
        + "function flipImg(btn){\n"
        "  var card=btn.parentElement;\n"
        "  var layers=card.querySelectorAll('.img-layer');\n"
        "  if(layers.length<2)return;\n"
        "  layers[0].classList.toggle('active');\n"
        "  layers[1].classList.toggle('active');\n"
        "}\n"
        + switch_js + "\n"
        + parts["init_js"] + "\n"
        + "</script>"
    )
    html = re.sub(
        r"<script>\s*function flipImg.*?</script>",
        lambda _: new_script,
        html,
        flags=re.DOTALL,
    )

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

    bbox = parse_bbox(args.bbox)
    target = args.target.resolve()
    if not target.exists():
        parser.error(f"Target not found: {target}")

    output_dir = (args.output or target.parent).resolve()
    cache_path = output_dir / ".map_cache.json"
    cache = load_cache(cache_path)

    log.info("=" * 60)
    log.info("Gulf Coast Trail Map Builder")
    log.info("  bbox  S=%.2f  W=%.2f  N=%.2f  E=%.2f", *bbox)
    log.info("  target  %s", target)
    log.info("=" * 60)

    # ── OSM layers ──
    log.info("\nStep 1/3  Fetching OpenStreetMap data...")
    layers = {}

    fetchers = [
        ("hiking",      fetch_hiking,      "Hiking trails"),
        ("bike",        fetch_bike,        "Bike routes"),
        ("beaches",     fetch_beaches,     "Beaches"),
        ("state_parks", fetch_state_parks, "State parks"),
        ("wilderness",  fetch_wilderness,  "Wilderness areas"),
        ("refuges",     fetch_refuges,     "Wildlife refuges"),
        ("forests",     fetch_forests,     "State/nat'l forests"),
        ("lighthouses", fetch_lighthouses, "Lighthouses"),
        ("historic",    fetch_historic,    "Historic structures"),
        ("nrhp",        fetch_nrhp,        "Nat'l Register sites"),
    ]
    total_osm = len(fetchers)
    for i, (key, fn, label) in enumerate(fetchers, 1):
        log.info("  [%d/%d] %s", i, total_osm, label)
        layers[key] = fn(bbox, cache)
        save_cache(cache_path, cache)

    # ── eBird layers ──
    log.info("\nStep 2/3  Fetching eBird data...")
    if args.ebird_key:
        log.info("  [11/12] Birding hotspots")
        layers["hotspots"] = fetch_hotspots(bbox, args.ebird_key, cache)
        save_cache(cache_path, cache)

        log.info("  [12/12] Recent observations (%d days)", args.back)
        layers["ebird_obs"] = fetch_ebird_obs(bbox, args.ebird_key,
                                              args.back, cache)
        save_cache(cache_path, cache)
    else:
        log.warning("  No eBird key — skipping bird layers")
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
