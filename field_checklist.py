#!/usr/bin/env python3
"""
Field Checklist Builder

Generates a combined bird + plant field checklist for any location and date.

Data sources:
  Birds  — eBird API (species list) + Cornell All About Birds (detail, images)
  Plants — iNaturalist (species list, images) + Go Botany (descriptions)
  Both   — iNaturalist histograms (seasonality bars)

Produces:
  <output>/<place-slug>/
    images/Birds/<Group>/<Species>.jpg
    images/Plants/<Group>/<Species>_1.jpg, _2.jpg
    birds.csv
    plants.csv
    index.html   (combined page with Birds/Plants toggle)

Usage:
  python3 field_checklist.py \\
    --place "Chappaquiddick Island" \\
    --date 2026-04-28 \\
    --lat 41.3636 --lng -70.5025 \\
    --ebird-key YOUR_KEY \\
    --moon "Waxing gibbous, 88%. Full moon May 1." \\
    --tides "Low 5:00 AM ... High 10:30 AM ..."
"""

import argparse
import csv
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

# ── Constants ──────────────────────────────────────────────────────────

CORNELL_CDN = "https://cdn.download.ams.birds.cornell.edu/api/v1/asset/{asset_id}/640"
CORNELL_GUIDE = "https://www.allaboutbirds.org/guide/{slug}"
CORNELL_ID_URL = "https://www.allaboutbirds.org/guide/{slug}/id"
EBIRD_API = "https://api.ebird.org/v2"
GOBOTANY_SPECIES = "https://gobotany.nativeplanttrust.org/species/{genus}/{species}/"
INAT_API = "https://api.inaturalist.org/v1"

HEADERS = {"User-Agent": "FieldChecklist/1.0 (educational)"}

# ── Bird group taxonomy ────────────────────────────────────────────────

BIRD_ORDER_GROUP = {
    "Charadriiformes": "Shorebirds",
    "Anseriformes": "Waterbirds",
    "Gaviiformes": "Waterbirds",
    "Podicipediformes": "Waterbirds",
    "Procellariiformes": "Waterbirds",
    "Suliformes": "Waterbirds",
    "Pelecaniformes": "Wading Birds",
    "Accipitriformes": "Raptors",
    "Falconiformes": "Raptors",
    "Strigiformes": "Raptors",
    "Caprimulgiformes": "Songbirds",
    "Apodiformes": "Songbirds",
    "Coraciiformes": "Songbirds",
    "Piciformes": "Songbirds",
    "Columbiformes": "Songbirds",
    "Cuculiformes": "Songbirds",
    "Gruiformes": "Waterbirds",
}

BIRD_FAMILY_GROUP = {
    "Hirundinidae": "Swallows",
    "Parulidae": "Warblers",
    "Vireonidae": "Warblers",
    "Passerellidae": "Sparrows",
    "Cardinalidae": "Sparrows",
    "Icteridae": "Sparrows",
}

BIRD_GROUP_ORDER = [
    "Shorebirds", "Waterbirds", "Wading Birds", "Raptors",
    "Swallows", "Warblers", "Sparrows", "Songbirds",
]

BIRD_GROUP_COLORS = {
    "Shorebirds": "#8B7348",
    "Waterbirds": "#2E6B94",
    "Wading Birds": "#3A7D50",
    "Raptors": "#6B4420",
    "Swallows": "#3A6BB5",
    "Warblers": "#6B7820",
    "Sparrows": "#7A5F40",
    "Songbirds": "#505060",
}

# ── Plant group helpers ────────────────────────────────────────────────

PLANT_GROUP_ORDER = ["Wildflowers & Herbs", "Ferns", "Shrubs", "Trees", "Vines"]

PLANT_GROUP_COLORS = {
    "Wildflowers & Herbs": "#6a8e3f",
    "Ferns": "#3a7a5a",
    "Shrubs": "#8a6a3a",
    "Trees": "#5a6a3a",
    "Vines": "#7a5a6a",
}

TREE_FAMILIES = {
    "Pinaceae", "Cupressaceae", "Taxaceae", "Fagaceae", "Betulaceae",
    "Juglandaceae", "Sapindaceae", "Ulmaceae", "Platanaceae", "Tiliaceae",
    "Malvaceae", "Oleaceae", "Nyssaceae", "Magnoliaceae", "Hamamelidaceae",
    "Altingiaceae", "Salicaceae", "Moraceae", "Simaroubaceae",
    "Arecaceae", "Burseraceae", "Meliaceae", "Lauraceae", "Sapotaceae",
    "Casuarinaceae", "Combretaceae", "Taxodiaceae", "Cycadaceae",
    "Zamiaceae", "Bignoniaceae", "Rhizophoraceae", "Annonaceae",
    "Chrysobalanaceae", "Clusiaceae", "Podocarpaceae",
}

SHRUB_FAMILIES = {
    "Ericaceae", "Caprifoliaceae", "Hydrangeaceae", "Rhamnaceae",
    "Aquifoliaceae", "Myricaceae", "Clethraceae", "Grossulariaceae",
    "Adoxaceae", "Cornaceae", "Cistaceae", "Thymelaeaceae",
    "Rubiaceae", "Verbenaceae", "Acanthaceae", "Melastomataceae",
    "Surianaceae", "Theaceae", "Calycanthaceae", "Staphyleaceae",
}

VINE_FAMILIES = {"Vitaceae", "Smilacaceae", "Menispermaceae", "Convolvulaceae"}

FERN_ANCESTOR_ID = 121943  # Polypodiopsida

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("field_checklist")


# ── Shared utilities ───────────────────────────────────────────────────

def strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#?\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def download_image(url: str, dest: Path, retries: int = 3) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            result = subprocess.run(
                ["curl", "-sS", "-L", "-o", str(dest), "-w", "%{http_code}", url],
                capture_output=True, text=True, timeout=30,
            )
            code = result.stdout.strip()
            if code == "200" and dest.exists() and dest.stat().st_size > 1000:
                return True
            dest.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(2 * attempt)
        except Exception:
            dest.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(2 * attempt)
    return False


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def month_level(counts: list[int], month_idx: int) -> int:
    if not counts or max(counts) == 0:
        return 0
    mx = max(counts)
    v = counts[month_idx]
    if v == 0:
        return 0
    ratio = v / mx
    if ratio >= 0.6:
        return 3
    if ratio >= 0.25:
        return 2
    return 1


def place_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "checklist"


# ── Cornell (bird detail) scraping ─────────────────────────────────────

def name_to_slug(common_name: str) -> str:
    name = common_name.strip()
    name = name.replace("N. Rough-winged", "Northern_Rough-winged")
    name = name.replace("Bonaparte's", "Bonapartes")
    name = name.replace("'", "")
    name = name.replace(" ", "_")
    return name


def extract_description(html: str) -> str:
    m = re.search(r'<h2[^>]*>Basic Description</h2>\s*<p>(.*?)</p>', html, re.DOTALL)
    return strip_tags(m.group(1)) if m else ""


def extract_find_bird(html: str) -> str:
    m = re.search(r'<h2>Find This Bird</h2>\s*<p>(?:<p>)?(.*?)</p>', html, re.DOTALL)
    return strip_tags(m.group(1)) if m else ""


def extract_sidebar_value(html: str, label: str) -> str:
    pattern = rf'<span>{re.escape(label)}</span>\s*<span>([^<]+)</span>'
    m = re.search(pattern, html)
    return m.group(1).strip() if m else ""


def extract_cool_facts(html: str) -> str:
    m = re.search(r'Cool Facts</a>.*?<ul>(.*?)</ul>', html, re.DOTALL)
    if not m:
        return ""
    items = re.findall(r'<li>(.*?)</li>', m.group(1), re.DOTALL)
    cleaned = [strip_tags(it) for it in items[:5] if len(strip_tags(it)) > 20]
    return " | ".join(cleaned)


def extract_order_family(html: str) -> tuple[str, str]:
    order = ""
    family = ""
    m = re.search(r'ORDER:.*?</span>\s*(\w+)', html)
    if m:
        order = m.group(1)
    m = re.search(r'FAMILY:.*?</span>\s*(\w+)', html)
    if m:
        family = m.group(1)
    return order, family


def scrape_cornell_overview(slug: str) -> dict:
    """Scrape the All About Birds overview page for one species."""
    url = CORNELL_GUIDE.format(slug=quote(slug, safe="/_-"))
    info = {
        "asset_id": "", "asset_id_2": "",
        "description": "", "habitat": "", "food": "", "nesting": "",
        "behavior": "", "conservation": "", "order": "", "family": "",
        "find_this_bird": "", "cool_facts": "",
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return info
        html = resp.text
    except Exception as e:
        log.warning("    Cornell overview error: %s", e)
        return info

    ids = re.findall(r"photo-gallery/(\d+)", html)
    if ids:
        info["asset_id"] = ids[0]
    seen = set()
    unique_ids = [x for x in ids if not (x in seen or seen.add(x))]
    if len(unique_ids) >= 2:
        info["asset_id_2"] = unique_ids[1]

    info["description"] = extract_description(html)
    info["find_this_bird"] = extract_find_bird(html)
    info["cool_facts"] = extract_cool_facts(html)
    info["habitat"] = extract_sidebar_value(html, "Habitat")
    info["food"] = extract_sidebar_value(html, "Food")
    info["nesting"] = extract_sidebar_value(html, "Nesting")
    info["behavior"] = extract_sidebar_value(html, "Behavior")
    info["conservation"] = extract_sidebar_value(html, "Conservation")
    order, family = extract_order_family(html)
    info["order"] = order
    info["family"] = family
    return info


def scrape_cornell_field_ids(slug: str) -> dict:
    """Scrape measurements and ID descriptions from the Cornell /id page."""
    url = CORNELL_ID_URL.format(slug=quote(slug, safe="/_-"))
    info = {"measurements": "", "size_shape": "", "color_pattern": "",
            "id_behavior": "", "id_habitat": ""}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return info
        html = resp.text
    except Exception:
        return info

    parts = []
    m = re.search(r'Length:\s*(.*?)(?:<|\n)', html)
    if m:
        parts.append("Length " + strip_tags(m.group(1)))
    m = re.search(r'Weight:\s*(.*?)(?:<|\n)', html)
    if m:
        parts.append("Weight " + strip_tags(m.group(1)))
    m = re.search(r'Wingspan:\s*(.*?)(?:<|\n)', html)
    if m:
        parts.append("Wingspan " + strip_tags(m.group(1)))
    info["measurements"] = " · ".join(parts) if parts else ""

    for section, key in [
        ("Size &amp; Shape", "size_shape"),
        ("Size & Shape", "size_shape"),
        ("Color Pattern", "color_pattern"),
        ("Behavior", "id_behavior"),
        ("Habitat", "id_habitat"),
    ]:
        pattern = rf'{re.escape(section)}</.*?<p>(.*?)</p>'
        m = re.search(pattern, html, re.DOTALL)
        if m and not info.get(key):
            info[key] = strip_tags(m.group(1))[:600]

    return info


# ── eBird API ──────────────────────────────────────────────────────────

def ebird_recent_species(lat: float, lng: float, radius_km: int,
                         api_key: str, back: int = 30) -> list[dict]:
    """Get unique species recently observed near coordinates from eBird."""
    dist = min(radius_km, 50)
    url = f"{EBIRD_API}/data/obs/geo/recent"
    params = {"lat": lat, "lng": lng, "dist": dist, "back": back}
    headers = {**HEADERS, "X-eBirdApiToken": api_key}

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 403:
        log.error("eBird API returned 403 — check your API key")
        sys.exit(1)
    resp.raise_for_status()
    data = resp.json()

    seen = {}
    for obs in data:
        code = obs.get("speciesCode", "")
        if code and code not in seen:
            seen[code] = {
                "species_code": code,
                "common_name": obs.get("comName", ""),
                "sci_name": obs.get("sciName", ""),
            }
    species = list(seen.values())
    log.info("  eBird: %d unique species from %d observations", len(species), len(data))
    return species


# ── iNaturalist API ────────────────────────────────────────────────────

def inat_species_for_month(taxon_id: int, lat: float, lng: float,
                           radius_km: int, months: str) -> dict:
    """Get species observed in given months near coordinates.

    Returns {scientific_name: {count, taxon_id, common_name, ...}}.
    """
    results_map = {}
    page = 1
    while True:
        params = {
            "taxon_id": taxon_id,
            "lat": lat, "lng": lng,
            "radius": radius_km,
            "month": months,
            "quality_grade": "research",
            "native": "true",
            "per_page": 200,
            "page": page,
        }
        try:
            resp = requests.get(
                f"{INAT_API}/observations/species_counts",
                params=params, headers=HEADERS, timeout=30,
            )
            data = resp.json()
        except Exception as e:
            log.warning("  iNat API error: %s", e)
            break

        for r in data.get("results", []):
            taxon = r["taxon"]
            sci = taxon.get("name", "")
            rank = taxon.get("rank", "")
            if rank not in ("species", "subspecies", "variety"):
                continue
            ancestor_ids = set(taxon.get("ancestor_ids", []))
            if 311249 in ancestor_ids or 311295 in ancestor_ids:
                continue
            results_map[sci] = {
                "count": r["count"],
                "taxon_id": taxon["id"],
                "common_name": taxon.get("preferred_common_name", ""),
                "rank": rank,
                "default_photo": taxon.get("default_photo"),
                "ancestor_ids": list(ancestor_ids),
                "iconic_taxon_name": taxon.get("iconic_taxon_name", ""),
            }
        if len(data.get("results", [])) < 200:
            break
        page += 1
        time.sleep(0.5)
    return results_map


def inat_monthly_histogram(taxon_id: int, lat: float, lng: float,
                           radius_km: int) -> list[int]:
    params = {
        "taxon_id": taxon_id,
        "lat": lat, "lng": lng, "radius": radius_km,
        "quality_grade": "research",
    }
    try:
        resp = requests.get(
            f"{INAT_API}/observations/histogram",
            params={**params, "date_field": "observed", "interval": "month_of_year"},
            headers=HEADERS, timeout=15,
        )
        data = resp.json()
        md = data.get("results", {}).get("month_of_year", {})
        return [md.get(str(m), 0) for m in range(1, 13)]
    except Exception:
        return [0] * 12


def inat_taxon_photos(taxon_id: int, limit: int = 6) -> list[str]:
    try:
        resp = requests.get(
            f"{INAT_API}/taxon_photos",
            params={"taxon_id": taxon_id, "per_page": limit},
            headers=HEADERS, timeout=15,
        )
        urls = []
        for tp in resp.json().get("results", []):
            url = tp.get("photo", {}).get("medium_url") or tp.get("photo", {}).get("url", "")
            if url:
                urls.append(url.replace("/medium.", "/large.").replace("/square.", "/large."))
        return urls
    except Exception:
        return []


def inat_observation_photos(taxon_id: int, lat: float, lng: float,
                            radius_km: int, limit: int = 10) -> list[str]:
    try:
        resp = requests.get(
            f"{INAT_API}/observations",
            params={
                "taxon_id": taxon_id, "lat": lat, "lng": lng,
                "radius": min(radius_km * 4, 100),
                "quality_grade": "research", "photos": "true",
                "per_page": limit, "order_by": "votes",
            },
            headers=HEADERS, timeout=15,
        )
        urls = []
        for obs in resp.json().get("results", []):
            for photo in obs.get("photos", []):
                url = photo.get("url", "")
                if url:
                    urls.append(url.replace("/square.", "/large."))
                if len(urls) >= limit:
                    return urls
        return urls
    except Exception:
        return []


def inat_batch_taxon_families(taxon_ids: list[int]) -> dict[int, str]:
    """Batch-fetch family names for a list of iNat taxon IDs.

    Returns {taxon_id: family_scientific_name}.
    """
    families = {}
    for i in range(0, len(taxon_ids), 30):
        batch = taxon_ids[i:i + 30]
        ids_str = ",".join(str(t) for t in batch)
        try:
            resp = requests.get(
                f"{INAT_API}/taxa/{ids_str}",
                headers=HEADERS, timeout=20,
            )
            for t in resp.json().get("results", []):
                tid = t["id"]
                for anc in reversed(t.get("ancestors", [])):
                    if anc.get("rank") == "family":
                        families[tid] = anc.get("name", "")
                        break
        except Exception:
            pass
        if i + 30 < len(taxon_ids):
            time.sleep(0.5)
    return families


def inat_bird_taxon_id(sci_name: str) -> int | None:
    """Look up a bird's iNat taxon ID by scientific name."""
    try:
        resp = requests.get(
            f"{INAT_API}/taxa",
            params={"q": sci_name, "rank": "species", "is_active": "true", "per_page": 5},
            headers=HEADERS, timeout=10,
        )
        for t in resp.json().get("results", []):
            if t.get("name", "").lower() == sci_name.lower():
                return t["id"]
        results = resp.json().get("results", [])
        if results:
            return results[0]["id"]
    except Exception:
        pass
    return None


# ── Go Botany (plant detail) scraping ──────────────────────────────────

def scrape_gobotany(genus: str, species_epithet: str) -> dict:
    info = {
        "facts": "", "habitat": "", "family": "", "conservation": "",
        "growth_form": "", "go_botany_images": [],
    }
    url = GOBOTANY_SPECIES.format(genus=genus.lower(), species=species_epithet.lower())
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return info
        html = resp.text
    except Exception:
        return info

    m = re.search(r"<h2[^>]*>\s*Facts\s*</h2>(.*?)(?=<h2|<div class=\"maps\")", html, re.DOTALL)
    if m:
        info["facts"] = strip_tags(m.group(1))[:600]

    m = re.search(r"<h2[^>]*>\s*Habitat\s*</h2>\s*<p>(.*?)</p>", html, re.DOTALL)
    if m:
        info["habitat"] = strip_tags(m.group(1))

    m = re.search(r"<h3[^>]*>\s*Family\s*</h3>(.*?)(?=<h3|</section)", html, re.DOTALL)
    if m:
        info["family"] = strip_tags(m.group(1))[:80]

    m = re.search(r"Conservation status</h3>(.*?)(?=<h3|</div>\s*</div>)", html, re.DOTALL)
    if m:
        info["conservation"] = strip_tags(m.group(1))[:300]

    m = re.search(r"Growth form</dt>(.*?)(?=</dd>|<dt)", html, re.DOTALL | re.IGNORECASE)
    if not m:
        m = re.search(r"Growth form.*?>(.*?)</", html, re.DOTALL | re.IGNORECASE)
    if m:
        info["growth_form"] = strip_tags(m.group(1)).lower()

    imgs = re.findall(
        r'src="(https://newfs\.s3\.amazonaws\.com/taxon-images-239x239/[^"]+)"', html,
    )
    info["go_botany_images"] = [u.replace("239x239", "1000s1000") for u in imgs][:6]

    return info


# ── Bird pipeline ──────────────────────────────────────────────────────

def assign_bird_group(order: str, family: str) -> str:
    if family in BIRD_FAMILY_GROUP:
        return BIRD_FAMILY_GROUP[family]
    if order in BIRD_ORDER_GROUP:
        return BIRD_ORDER_GROUP[order]
    if order == "Passeriformes":
        return "Songbirds"
    return "Songbirds"


BIRD_CSV_FIELDS = [
    "Common Name", "Scientific Name", "Order", "Family", "Group",
    "Habitat", "Food", "Nesting", "Behavior", "Conservation",
    "Description", "Find This Bird", "Cool Facts",
    "Measurements", "Size & Shape", "Color Pattern",
    "Asset ID", "Asset ID 2", "Local Path", "Downloaded",
]


def run_birds(cfg: dict) -> list[dict]:
    """Run the bird pipeline. Returns enriched bird records."""
    log.info("=" * 60)
    log.info("Bird Pipeline — %s", cfg["place"])
    log.info("=" * 60)

    out_dir = cfg["output_dir"] / "images" / "Birds"
    bird_cache_path = cfg["output_dir"] / ".bird_cache.json"
    seasonality_path = cfg["output_dir"] / ".seasonality.json"

    log.info("\nStep 1: Querying eBird for recent species...")
    ebird_species = ebird_recent_species(
        cfg["lat"], cfg["lng"], cfg["radius"],
        cfg["ebird_key"], back=30,
    )

    cache = load_json(bird_cache_path)
    seasonality = load_json(seasonality_path)

    log.info("\nStep 2: Scraping Cornell for species detail...")
    birds = []
    for i, sp in enumerate(ebird_species):
        name = sp["common_name"]
        slug = name_to_slug(name)
        log.info("  [%d/%d] %s", i + 1, len(ebird_species), name)

        if name in cache and cache[name].get("description"):
            info = cache[name]
            log.info("    (cached)")
        else:
            info = scrape_cornell_overview(slug)
            field_ids = scrape_cornell_field_ids(slug)
            info.update(field_ids)
            cache[name] = info
            save_json(bird_cache_path, cache)
            time.sleep(0.5)

        group = assign_bird_group(info.get("order", ""), info.get("family", ""))
        birds.append({**sp, **info, "group": group})

        if info.get("asset_id"):
            log.info("    %s | %s | %s", group,
                     info.get("conservation", "") or "—",
                     (info["description"][:70] + "...") if len(info.get("description", "")) > 70 else "—")
        else:
            log.warning("    Not found on All About Birds")

    birds.sort(key=lambda b: (
        BIRD_GROUP_ORDER.index(b["group"]) if b["group"] in BIRD_GROUP_ORDER else 99,
        b["common_name"],
    ))

    log.info("\nStep 3: Downloading bird images from Cornell CDN...")
    out_dir.mkdir(parents=True, exist_ok=True)
    success = 0
    for i, bird in enumerate(birds):
        name = bird["common_name"]
        group_dir = out_dir / safe_filename(bird["group"])
        dest = group_dir / (safe_filename(name) + ".jpg")
        log.info("  [%d/%d] %s", i + 1, len(birds), name)

        if dest.exists() and dest.stat().st_size > 1000:
            log.info("    exists")
            bird["local_path"] = str(dest.relative_to(cfg["output_dir"]))
            success += 1
        elif bird.get("asset_id"):
            dl_url = CORNELL_CDN.format(asset_id=bird["asset_id"])
            if download_image(dl_url, dest):
                bird["local_path"] = str(dest.relative_to(cfg["output_dir"]))
                success += 1
                log.info("    OK")
            else:
                bird["local_path"] = ""
                log.warning("    FAILED")
            time.sleep(1.5)
        else:
            bird["local_path"] = ""

    log.info("  Downloaded %d / %d bird images", success, len(birds))

    log.info("\nStep 4: Bird seasonality from iNaturalist...")
    for i, bird in enumerate(birds):
        sci = bird.get("sci_name", "")
        cache_key = f"bird:{sci}"
        if cache_key in seasonality:
            bird["seasonality"] = seasonality[cache_key]
            continue

        tid = inat_bird_taxon_id(sci)
        if tid:
            log.info("  [%d/%d] %s (taxon %d)", i + 1, len(birds), bird["common_name"], tid)
            hist = inat_monthly_histogram(tid, cfg["lat"], cfg["lng"], cfg["radius"])
            seasonality[cache_key] = hist
            bird["seasonality"] = hist
            save_json(seasonality_path, seasonality)
            time.sleep(0.3)
        else:
            bird["seasonality"] = [0] * 12

    csv_path = cfg["output_dir"] / "birds.csv"
    records = []
    for bird in birds:
        records.append({
            "Common Name": bird["common_name"],
            "Scientific Name": bird.get("sci_name", ""),
            "Order": bird.get("order", ""),
            "Family": bird.get("family", ""),
            "Group": bird["group"],
            "Habitat": bird.get("habitat", ""),
            "Food": bird.get("food", ""),
            "Nesting": bird.get("nesting", ""),
            "Behavior": bird.get("behavior", ""),
            "Conservation": bird.get("conservation", ""),
            "Description": bird.get("description", ""),
            "Find This Bird": bird.get("find_this_bird", ""),
            "Cool Facts": bird.get("cool_facts", ""),
            "Measurements": bird.get("measurements", ""),
            "Size & Shape": bird.get("size_shape", ""),
            "Color Pattern": bird.get("color_pattern", ""),
            "Asset ID": bird.get("asset_id", ""),
            "Asset ID 2": bird.get("asset_id_2", ""),
            "Local Path": bird.get("local_path", ""),
            "Downloaded": "yes" if bird.get("local_path") else "no",
        })
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BIRD_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    log.info("  Wrote %s (%d species)", csv_path, len(records))

    return birds


# ── Plant pipeline ─────────────────────────────────────────────────────

def infer_plant_group(taxon_info: dict, gobotany_info: dict,
                      inat_family: str = "") -> str:
    """Classify a plant into a display group."""
    ancestor_ids = set(taxon_info.get("ancestor_ids", []))
    if FERN_ANCESTOR_ID in ancestor_ids:
        return "Ferns"

    gf = gobotany_info.get("growth_form", "")
    if "tree" in gf:
        return "Trees"
    if "shrub" in gf:
        return "Shrubs"
    if "vine" in gf or "climbing" in gf:
        return "Vines"
    if "fern" in gf:
        return "Ferns"

    family = gobotany_info.get("family", "")
    family_clean = family.split("(")[0].strip().split(",")[0].strip()
    if not family_clean:
        family_clean = inat_family
    if family_clean in TREE_FAMILIES:
        return "Trees"
    if family_clean in SHRUB_FAMILIES:
        return "Shrubs"
    if family_clean in VINE_FAMILIES:
        return "Vines"

    return "Wildflowers & Herbs"


PLANT_CSV_FIELDS = [
    "Common Name", "Scientific Name", "Family", "Group",
    "Description", "Habitat", "Conservation",
    "iNat Observations", "Taxon ID",
    "Image 1", "Image 2",
]


def run_plants(cfg: dict) -> list[dict]:
    """Run the plant pipeline. Returns enriched plant records."""
    log.info("\n" + "=" * 60)
    log.info("Plant Pipeline — %s", cfg["place"])
    log.info("=" * 60)

    out_dir = cfg["output_dir"] / "images" / "Plants"
    plant_cache_path = cfg["output_dir"] / ".plant_cache.json"
    seasonality_path = cfg["output_dir"] / ".seasonality.json"

    target_month = cfg["date"].month
    m_prev = ((target_month - 2) % 12) + 1
    m_next = (target_month % 12) + 1
    months_str = ",".join(str(m) for m in sorted({m_prev, target_month, m_next}))

    log.info("\nStep 1: Querying iNaturalist for plant species (months %s)...", months_str)
    inat_data = inat_species_for_month(
        47126, cfg["lat"], cfg["lng"], cfg["radius"], months_str,
    )
    log.info("  Found %d native plant species", len(inat_data))

    species_list = []
    for sci, info in inat_data.items():
        if info["count"] < 1:
            continue
        species_list.append({
            "common_name": info["common_name"] or sci.split()[0],
            "scientific_name": sci,
            "inat_count": info["count"],
            "taxon_id": info["taxon_id"],
            "ancestor_ids": info.get("ancestor_ids", []),
            "default_photo": info.get("default_photo"),
        })

    cache = load_json(plant_cache_path)
    seasonality = load_json(seasonality_path)

    log.info("\nStep 2: Scraping Go Botany for descriptions...")
    for i, entry in enumerate(species_list):
        sci = entry["scientific_name"]
        parts = sci.split()
        if len(parts) < 2:
            entry["gobotany"] = {}
            continue
        genus, sp_epithet = parts[0], parts[1]
        cache_key = f"{genus}_{sp_epithet}"
        log.info("  [%d/%d] %s (%s)", i + 1, len(species_list), entry["common_name"], sci)

        if cache_key in cache and cache[cache_key].get("facts"):
            gb = cache[cache_key]
            log.info("    (cached)")
        else:
            gb = scrape_gobotany(genus, sp_epithet)
            cache[cache_key] = gb
            save_json(plant_cache_path, cache)
            time.sleep(0.4)

        entry["gobotany"] = gb
        entry["facts"] = gb.get("facts", "")
        entry["habitat"] = gb.get("habitat", "")
        entry["family"] = gb.get("family", "")
        entry["conservation"] = gb.get("conservation", "")

        if gb.get("facts"):
            log.info("    %s", gb["facts"][:70] + "...")
        else:
            log.info("    No Go Botany page")

    needs_family = [e for e in species_list
                    if not e.get("family") and e.get("taxon_id")]
    inat_families: dict[int, str] = {}
    if needs_family:
        log.info("\n  Fetching iNaturalist taxonomy for %d species without Go Botany data...",
                 len(needs_family))
        tids = [e["taxon_id"] for e in needs_family]
        inat_families = inat_batch_taxon_families(tids)
        for e in needs_family:
            fam = inat_families.get(e["taxon_id"], "")
            if fam:
                e["family"] = fam

    for entry in species_list:
        inat_fam = inat_families.get(entry.get("taxon_id", 0), "")
        group = infer_plant_group(entry, entry.get("gobotany", {}), inat_fam)
        entry["group"] = group

    species_list.sort(key=lambda e: (
        PLANT_GROUP_ORDER.index(e["group"]) if e["group"] in PLANT_GROUP_ORDER else 99,
        -e["inat_count"],
    ))

    log.info("\nStep 3: Plant seasonality from iNaturalist...")
    for i, entry in enumerate(species_list):
        tid = entry.get("taxon_id")
        if not tid:
            entry["seasonality"] = [0] * 12
            continue
        cache_key = f"plant:{tid}"
        if cache_key in seasonality:
            entry["seasonality"] = seasonality[cache_key]
            continue
        log.info("  [%d/%d] %s (taxon %d)", i + 1, len(species_list), entry["common_name"], tid)
        hist = inat_monthly_histogram(tid, cfg["lat"], cfg["lng"], cfg["radius"])
        seasonality[cache_key] = hist
        entry["seasonality"] = hist
        save_json(seasonality_path, seasonality)
        time.sleep(0.3)

    if not cfg.get("skip_images"):
        log.info("\nStep 4: Downloading plant images...")
        out_dir.mkdir(parents=True, exist_ok=True)
        success = 0

        for i, entry in enumerate(species_list):
            name = entry["common_name"]
            group_dir = out_dir / safe_filename(entry["group"])
            base = safe_filename(name)
            dest1 = group_dir / f"{base}_1.jpg"
            dest2 = group_dir / f"{base}_2.jpg"
            log.info("  [%d/%d] %s", i + 1, len(species_list), name)

            photo_urls = []
            photo_urls.extend(entry.get("gobotany", {}).get("go_botany_images", []))
            tid = entry.get("taxon_id")
            if tid:
                photo_urls.extend(inat_taxon_photos(tid, limit=4))
            if len(photo_urls) < 2 and tid:
                photo_urls.extend(
                    inat_observation_photos(tid, cfg["lat"], cfg["lng"], cfg["radius"], limit=4)
                )

            if dest1.exists() and dest1.stat().st_size > 1000:
                entry["image_1"] = str(dest1.relative_to(cfg["output_dir"]))
            else:
                for url in photo_urls:
                    if download_image(url, dest1):
                        entry["image_1"] = str(dest1.relative_to(cfg["output_dir"]))
                        break
                else:
                    entry["image_1"] = ""

            if dest2.exists() and dest2.stat().st_size > 1000:
                entry["image_2"] = str(dest2.relative_to(cfg["output_dir"]))
            else:
                remaining = photo_urls[1:] if photo_urls else []
                for url in remaining:
                    if download_image(url, dest2):
                        entry["image_2"] = str(dest2.relative_to(cfg["output_dir"]))
                        break
                else:
                    entry["image_2"] = ""

            if entry.get("image_1"):
                success += 1
            time.sleep(0.3)

        log.info("  Images for %d / %d species", success, len(species_list))
    else:
        log.info("\nStep 4: Skipping image download")
        for entry in species_list:
            group_dir = out_dir / safe_filename(entry.get("group", ""))
            base = safe_filename(entry["common_name"])
            d1 = group_dir / f"{base}_1.jpg"
            d2 = group_dir / f"{base}_2.jpg"
            entry["image_1"] = str(d1.relative_to(cfg["output_dir"])) if d1.exists() else ""
            entry["image_2"] = str(d2.relative_to(cfg["output_dir"])) if d2.exists() else ""

    csv_path = cfg["output_dir"] / "plants.csv"
    records = []
    for entry in species_list:
        records.append({
            "Common Name": entry["common_name"],
            "Scientific Name": entry["scientific_name"],
            "Family": entry.get("family", ""),
            "Group": entry.get("group", ""),
            "Description": entry.get("facts", ""),
            "Habitat": entry.get("habitat", ""),
            "Conservation": entry.get("conservation", ""),
            "iNat Observations": entry.get("inat_count", 0),
            "Taxon ID": entry.get("taxon_id", ""),
            "Image 1": entry.get("image_1", ""),
            "Image 2": entry.get("image_2", ""),
        })
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PLANT_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    log.info("  Wrote %s (%d species)", csv_path, len(records))

    return species_list


# ── HTML generation ────────────────────────────────────────────────────

CSS = """\
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#fff;--card:#fff;--text:#1a1a1a;--muted:#707070;--border:#d0d0d0;--accent:#444}
body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans','Helvetica Neue',system-ui,sans-serif;font-size:15px;line-height:1.6}
a{color:inherit;text-decoration:none}
.layout{display:flex;min-height:100vh}
.sidebar{position:sticky;top:0;height:100vh;width:240px;flex-shrink:0;background:#fafafa;border-right:1px solid var(--border);overflow-y:auto;padding:20px 0}
.sidebar-head{padding:0 16px 16px;border-bottom:1px solid var(--border)}
.sidebar-head h1{font-family:'IBM Plex Serif',Georgia,serif;font-size:16px;font-weight:600;line-height:1.3}
.sidebar-head .subtitle{font-size:11px;color:var(--muted);margin-top:3px;letter-spacing:.4px;text-transform:uppercase}
.sidebar-head .date{font-size:11px;color:var(--muted);margin-top:5px;font-family:'IBM Plex Mono',monospace}
.sidebar-head .stat{font-size:11px;color:var(--muted);margin-top:3px}
.mode-toggle{display:flex;margin:12px 16px;border:1px solid var(--border);overflow:hidden}
.mode-btn{flex:1;padding:7px 0;font-size:11px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;text-align:center;cursor:pointer;border:none;background:transparent;color:var(--muted);font-family:'IBM Plex Sans','Helvetica Neue',system-ui,sans-serif;transition:background .2s,color .2s}
.mode-btn.active{background:var(--text);color:#fff}
.nav-links{padding:12px 8px}
.nav-link{display:flex;align-items:center;gap:6px;padding:6px 10px;font-size:12px;font-weight:500;color:var(--muted)}
.nav-link:hover{color:var(--text)}
.nav-link.active{color:var(--text);font-weight:600}
.nav-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.nav-count{margin-left:auto;font-size:10px;opacity:.5}
.main{flex:1;max-width:1200px;padding:32px 40px 80px}
.page-header{text-align:left;padding:32px 0 28px;border-bottom:1px solid var(--border);margin-bottom:32px}
.page-header h1{font-family:'IBM Plex Serif',Georgia,serif;font-size:28px;font-weight:600;letter-spacing:-.2px}
.page-header .sub{font-size:13px;color:var(--muted);margin-top:4px}
.page-header .location{font-size:12px;color:var(--muted);margin-top:8px;font-family:'IBM Plex Mono',monospace}
.page-header .locations{font-size:11px;color:var(--muted);margin-top:3px;letter-spacing:.2px}
.trip-info{padding:0;margin-bottom:24px}
.trip-grid{display:flex;gap:20px}
.trip-item{font-size:12px;line-height:1.5;color:#444}
.trip-item strong{font-size:12px;text-transform:uppercase;letter-spacing:.3px;color:var(--muted);font-weight:600}
.trip-row{display:flex;gap:24px;flex-wrap:wrap;font-size:12px;color:var(--muted);line-height:1.6}
.trip-row strong{color:var(--text);font-weight:600;margin-right:4px}
.group-section{margin-bottom:40px}
.group-header{display:flex;align-items:baseline;gap:10px;margin-bottom:16px;padding-top:8px;border-bottom:1px solid var(--border);padding-bottom:8px}
.group-bar{width:3px;height:20px;flex-shrink:0}
.group-header h2{font-family:'IBM Plex Serif',Georgia,serif;font-size:19px;font-weight:600}
.group-count{font-size:11px;color:var(--muted);margin-left:auto}
.bird-card{background:var(--card);border:1px solid var(--border);overflow:hidden;margin-bottom:16px;display:flex;flex-direction:row;align-items:flex-start}
.card-image{width:320px;aspect-ratio:4/3;flex-shrink:0;position:relative;background:#e8e8e8}
.img-layer{position:absolute;top:0;left:0;width:100%;height:100%;background-size:cover;background-position:center;opacity:0;transition:opacity .3s}
.img-layer.active{opacity:1}
.flip-btn{position:absolute;bottom:8px;right:8px;width:28px;height:28px;background:rgba(0,0,0,.55);color:#fff;border:none;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .2s}
.card-image:hover .flip-btn{opacity:1}
.card-body{flex:1;padding:18px 22px;display:flex;flex-direction:column;min-width:0}
.card-top{margin-bottom:8px}
.card-top h3{font-family:'IBM Plex Serif',Georgia,serif;font-size:18px;font-weight:600;margin-bottom:1px}
.latin{font-size:12px;color:var(--muted);display:block;margin-bottom:5px;font-family:'IBM Plex Mono',monospace;letter-spacing:.1px}
.conservation{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.4px;padding:2px 8px;text-transform:uppercase;border:1px solid}
.alert-orange{color:#c45000;border-color:#c45000}
.alert-yellow{color:#a06800;border-color:#a06800}
.alert-watch{color:#8a6000;border-color:#8a6000}
.alert-decline{color:#a03020;border-color:#a03020}
.alert-low{color:#2a6830;border-color:#2a6830}
.occ-abundant{color:#2a6830;border-color:#2a6830}
.occ-common{color:#4a7830;border-color:#4a7830}
.occ-occasional{color:#8a6000;border-color:#8a6000}
.occ-uncommon{color:#707070;border-color:#707070}
.season-bar{display:flex;gap:1px;margin-bottom:10px;align-items:flex-end;height:20px}
.season-bar .mo{display:flex;flex-direction:column;align-items:center;gap:1px;flex:1}
.season-bar .mo-bar{width:100%;border-radius:1px}
.season-bar .mo-lbl{font-size:8px;font-family:'IBM Plex Mono',monospace;color:var(--muted);line-height:1}
.s0 .mo-bar{height:2px;background:#e8e8e8}
.s1 .mo-bar{height:5px;background:#b8cfb0}
.s2 .mo-bar{height:11px;background:#6ea85e}
.s3 .mo-bar{height:18px;background:#2d7a1e}
.season-bar .mo.now{outline:1.5px solid var(--text);outline-offset:-1px;border-radius:2px}
.meta-row{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:10px}
.meta-tag{font-size:10px;padding:2px 8px;border:1px solid var(--border);color:var(--muted);font-weight:500;text-transform:uppercase;letter-spacing:.3px}
.description{font-size:13px;line-height:1.7;color:#333;margin-bottom:8px}
.find-bird{font-size:12px;line-height:1.6;color:var(--muted);margin-bottom:8px}
.find-bird strong{color:var(--text);font-weight:600}
.field-ids{margin-bottom:8px}
.field-meas{font-size:11px;font-family:'IBM Plex Mono',monospace;color:var(--muted);margin-bottom:6px;letter-spacing:.1px}
.field-id{font-size:12px;line-height:1.55;color:#444;margin-bottom:4px}
.field-id strong{color:var(--text);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.3px;margin-right:4px}
.card-footer{margin-top:auto;padding-top:8px;border-top:1px solid var(--border)}
.taxonomy{font-size:10px;color:var(--muted);letter-spacing:.2px;font-family:'IBM Plex Mono',monospace}
.back-top{position:fixed;bottom:24px;right:24px;background:var(--text);color:var(--bg);width:36px;height:36px;display:flex;align-items:center;justify-content:center;font-size:18px;cursor:pointer;border:none;opacity:.3;transition:opacity .2s}
.back-top:hover{opacity:.8}
.panel{display:none}
.panel.active{display:block}
@media(max-width:900px){
  .layout{flex-direction:column}
  .sidebar{position:relative;width:100%;height:auto;border-right:none;border-bottom:1px solid var(--border)}
  .main{padding:16px}
  .bird-card{flex-direction:column}
  .card-image{width:100%;aspect-ratio:16/9}
  .trip-grid{flex-direction:column}
}"""

MONTH_LABELS = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]


def build_season_bar_html(seasonality: list[int], current_month_0: int) -> str:
    parts = ['<div class="season-bar">']
    for mi in range(12):
        level = month_level(seasonality, mi)
        now_cls = " now" if mi == current_month_0 else ""
        parts.append(
            f'<div class="mo s{level}{now_cls}">'
            f'<div class="mo-bar"></div>'
            f'<span class="mo-lbl">{MONTH_LABELS[mi]}</span></div>'
        )
    parts.append("</div>")
    return "".join(parts)


def conservation_badge_class(text: str) -> str:
    t = text.lower()
    if "orange" in t or "tipping" in t:
        return "alert-orange"
    if "yellow" in t or "watch" in t:
        return "alert-yellow"
    if "decline" in t:
        return "alert-decline"
    if "low" in t:
        return "alert-low"
    return "alert-low"


def build_bird_card(bird: dict, current_month_0: int, cfg: dict) -> str:
    name = esc(bird.get("common_name", ""))
    sci = esc(bird.get("sci_name", ""))
    desc = esc(bird.get("description", ""))
    find = esc(bird.get("find_this_bird", ""))
    conservation = bird.get("conservation", "")
    habitat = bird.get("habitat", "")
    food = bird.get("food", "")
    nesting = bird.get("nesting", "")
    behavior_sidebar = bird.get("behavior", "")

    asset1 = bird.get("asset_id", "")
    asset2 = bird.get("asset_id_2", "")

    if asset1:
        img1_url = CORNELL_CDN.format(asset_id=asset1)
        layer1 = f'<div class="img-layer active" style="background-image:url({esc(img1_url)})"></div>'
    else:
        layer1 = '<div class="img-layer active" style="background:#ddd"></div>'

    if asset2:
        img2_url = CORNELL_CDN.format(asset_id=asset2)
        layer2 = f'<div class="img-layer" style="background-image:url({esc(img2_url)})"></div>'
        flip_btn = '<button class="flip-btn" onclick="flipImg(this)">&#8644;</button>'
    else:
        layer2 = ""
        flip_btn = ""

    badge_cls = conservation_badge_class(conservation)
    badge = f'<span class="conservation {badge_cls}">{esc(conservation)}</span>' if conservation else ""

    season_html = build_season_bar_html(bird.get("seasonality", [0] * 12), current_month_0)

    meta_tags = ""
    for label, val in [("Habitat", habitat), ("Food", food), ("Nesting", nesting), ("Behavior", behavior_sidebar)]:
        if val:
            meta_tags += f'<span class="meta-tag" title="{label}">{esc(val)}</span>'

    field_ids_html = ""
    meas = bird.get("measurements", "")
    size_shape = bird.get("size_shape", "")
    color_pattern = bird.get("color_pattern", "")
    if meas or size_shape or color_pattern:
        field_ids_html = '<div class="field-ids">'
        if meas:
            field_ids_html += f'<div class="field-meas">{esc(meas)}</div>'
        if size_shape:
            field_ids_html += f'<div class="field-id"><strong>Size &amp; Shape</strong> {esc(size_shape)}</div>'
        if color_pattern:
            field_ids_html += f'<div class="field-id"><strong>Color Pattern</strong> {esc(color_pattern)}</div>'
        field_ids_html += "</div>"

    order = esc(bird.get("order", ""))
    family = esc(bird.get("family", ""))
    taxonomy = f"{order} &middot; {family}" if order and family else order or family

    return f"""<div class="bird-card">
<div class="card-image">{layer1}{layer2}{flip_btn}</div>
<div class="card-body">
<div class="card-top">
<h3>{name}</h3>
<span class="latin">{sci}</span>
{badge}
</div>
{season_html}
<div class="meta-row">{meta_tags}</div>
{"<p class='description'>" + desc + "</p>" if desc else ""}
{"<p class='find-bird'><strong>Where to look:</strong> " + find + "</p>" if find else ""}
{field_ids_html}
<div class="card-footer">
<span class="taxonomy">{taxonomy}</span>
</div>
</div>
</div>"""


def build_plant_card(plant: dict, current_month_0: int, cfg: dict) -> str:
    name = esc(plant.get("common_name", ""))
    sci = esc(plant.get("scientific_name", ""))
    desc = esc(plant.get("facts", ""))
    habitat = esc(plant.get("habitat", ""))
    family = esc(plant.get("family", ""))
    inat_count = plant.get("inat_count", 0)

    img1 = plant.get("image_1", "")
    img2 = plant.get("image_2", "")

    if img1:
        layer1 = f'<div class="img-layer active" style="background-image:url(\'{esc(img1)}\')"></div>'
    else:
        layer1 = '<div class="img-layer active" style="background:#ddd"></div>'
    if img2:
        layer2 = f'<div class="img-layer" style="background-image:url(\'{esc(img2)}\')"></div>'
        flip_btn = '<button class="flip-btn" onclick="flipImg(this)">&#8644;</button>'
    else:
        layer2 = ""
        flip_btn = ""

    season_html = build_season_bar_html(plant.get("seasonality", [0] * 12), current_month_0)

    meta_tags = ""
    if inat_count:
        meta_tags += f'<span class="meta-tag">iNat: {inat_count} obs</span>'

    return f"""<div class="bird-card">
<div class="card-image">{layer1}{layer2}{flip_btn}</div>
<div class="card-body">
<div class="card-top">
<h3>{name}</h3>
<span class="latin">{sci}</span>
</div>
{season_html}
<div class="meta-row">{meta_tags}</div>
{"<p class='description'>" + desc + "</p>" if desc else ""}
{"<p class='find-bird'><strong>Habitat:</strong> " + habitat + "</p>" if habitat else ""}
<div class="card-footer">
<span class="taxonomy">{family}</span>
</div>
</div>
</div>"""


def build_grouped_html(records: list[dict], group_order: list[str],
                       group_colors: dict, card_fn, current_month_0: int,
                       cfg: dict, prefix: str) -> tuple[str, str]:
    """Build nav links and card sections for a set of records.

    Returns (nav_html, cards_html).
    """
    groups: dict[str, list] = {}
    for rec in records:
        groups.setdefault(rec.get("group", "Other"), []).append(rec)

    nav_parts = []
    card_parts = []

    for g in group_order:
        if g not in groups:
            continue
        gid = prefix + g.lower().replace(" ", "-").replace("&", "and")
        color = group_colors.get(g, "#666")
        count = len(groups[g])
        nav_parts.append(
            f'<a class="nav-link" href="#{gid}">'
            f'<span class="nav-dot" style="background:{color}"></span>'
            f'{esc(g)}<span class="nav-count">{count}</span></a>'
        )
        card_parts.append(
            f'<div class="group-section" id="{gid}">'
            f'<div class="group-header">'
            f'<div class="group-bar" style="background:{color}"></div>'
            f'<h2>{esc(g)}</h2>'
            f'<span class="group-count">{count} species</span>'
            f'</div>'
        )
        for rec in groups[g]:
            card_parts.append(card_fn(rec, current_month_0, cfg))
        card_parts.append("</div>")

    ungrouped = [g for g in groups if g not in group_order]
    for g in ungrouped:
        gid = prefix + re.sub(r"[^a-z0-9]+", "-", g.lower()).strip("-")
        color = "#666"
        count = len(groups[g])
        nav_parts.append(
            f'<a class="nav-link" href="#{gid}">'
            f'<span class="nav-dot" style="background:{color}"></span>'
            f'{esc(g)}<span class="nav-count">{count}</span></a>'
        )
        card_parts.append(
            f'<div class="group-section" id="{gid}">'
            f'<div class="group-header">'
            f'<div class="group-bar" style="background:{color}"></div>'
            f'<h2>{esc(g)}</h2>'
            f'<span class="group-count">{count} species</span>'
            f'</div>'
        )
        for rec in groups[g]:
            card_parts.append(card_fn(rec, current_month_0, cfg))
        card_parts.append("</div>")

    return "\n".join(nav_parts), "\n".join(card_parts)


def generate_html(birds: list[dict], plants: list[dict], cfg: dict):
    """Generate the combined index.html with Birds/Plants toggle."""
    current_month_0 = cfg["date"].month - 1
    date_str = cfg["date"].strftime("%B %d, %Y")
    lat_str = f"{abs(cfg['lat']):.4f} {'N' if cfg['lat'] >= 0 else 'S'}"
    lng_str = f"{abs(cfg['lng']):.4f} {'W' if cfg['lng'] < 0 else 'E'}"

    has_birds = len(birds) > 0
    has_plants = len(plants) > 0
    has_both = has_birds and has_plants

    bird_nav, bird_cards = "", ""
    plant_nav, plant_cards = "", ""

    if has_birds:
        bird_nav, bird_cards = build_grouped_html(
            birds, BIRD_GROUP_ORDER, BIRD_GROUP_COLORS,
            build_bird_card, current_month_0, cfg, "b-",
        )
    if has_plants:
        plant_nav, plant_cards = build_grouped_html(
            plants, PLANT_GROUP_ORDER, PLANT_GROUP_COLORS,
            build_plant_card, current_month_0, cfg, "p-",
        )

    trip_items = ""
    if cfg.get("moon"):
        trip_items += f'<div class="trip-item"><strong>Moon</strong> {esc(cfg["moon"])}</div>'
    if cfg.get("tides"):
        trip_items += f'<div class="trip-item"><strong>Tides</strong> {esc(cfg["tides"])}</div>'

    trip_html = ""
    if trip_items:
        trip_html = f'<div class="trip-info"><div class="trip-grid">{trip_items}</div></div>'

    toggle_html = ""
    if has_both:
        toggle_html = (
            '<div class="mode-toggle">'
            '<button class="mode-btn active" id="btn-birds" onclick="switchMode(\'birds\')">Birds</button>'
            '<button class="mode-btn" id="btn-plants" onclick="switchMode(\'plants\')">Plants</button>'
            '</div>'
        )

    bird_nav_block = f'<div class="nav-links" id="nav-birds">{bird_nav}</div>' if has_birds else ""
    plant_nav_display = ' style="display:none"' if has_both else ""
    plant_nav_block = f'<div class="nav-links" id="nav-plants"{plant_nav_display}>{plant_nav}</div>' if has_plants else ""

    stat_text = ""
    if has_both:
        stat_text = f'{len(birds)} Bird Species'
    elif has_birds:
        stat_text = f'{len(birds)} Bird Species'
    else:
        stat_text = f'{len(plants)} Plant Species'

    bird_panel_cls = "panel active" if has_birds else "panel"
    plant_panel_cls = "panel" if has_both else ("panel active" if has_plants else "panel")

    bird_panel = ""
    if has_birds:
        bird_panel = f"""<div class="{bird_panel_cls}" id="panel-birds">
<div class="page-header">
<h1>{esc(cfg['place'])} Bird Checklist</h1>
<div class="sub">{len(birds)} Species</div>
<div class="location">{lat_str}, {lng_str}</div>
<div class="locations">Sources: eBird, Cornell All About Birds, iNaturalist</div>
</div>
{trip_html}
{bird_cards}
</div>"""

    plant_panel = ""
    if has_plants:
        plant_panel = f"""<div class="{plant_panel_cls}" id="panel-plants">
<div class="page-header">
<h1>{esc(cfg['place'])} Plant Checklist</h1>
<div class="sub">{len(plants)} Species</div>
<div class="location">{lat_str}, {lng_str}</div>
<div class="locations">Sources: iNaturalist, Go Botany (Native Plant Trust)</div>
</div>
{trip_html}
{plant_cards}
</div>"""

    switch_js = ""
    if has_both:
        switch_js = f"""
function switchMode(mode){{
  var birds=mode==='birds';
  document.getElementById('panel-birds').classList.toggle('active',birds);
  document.getElementById('panel-plants').classList.toggle('active',!birds);
  document.getElementById('nav-birds').style.display=birds?'':'none';
  document.getElementById('nav-plants').style.display=birds?'none':'';
  document.getElementById('btn-birds').classList.toggle('active',birds);
  document.getElementById('btn-plants').classList.toggle('active',!birds);
  document.getElementById('stat-text').textContent=birds?'{len(birds)} Bird Species':'{len(plants)} Plant Species';
  scrollTo({{top:0}});
}}"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(cfg['place'])} Field Checklist</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Serif:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Sans:wght@300;400;500;600&family=IBM+Plex+Mono:wght@400&display=swap" rel="stylesheet">
<style>
{CSS}
</style>
</head>
<body>
<div class="layout">
<nav class="sidebar">
<div class="sidebar-head">
<h1>{esc(cfg['place'])}</h1>
<div class="subtitle">Field Checklist</div>
<div class="date">{date_str}</div>
<div class="stat" id="stat-text">{stat_text}</div>
</div>
{toggle_html}
{bird_nav_block}
{plant_nav_block}
</nav>
<main class="main">
{bird_panel}
{plant_panel}
</main>
</div>
<button class="back-top" onclick="scrollTo({{top:0,behavior:'smooth'}})">&uarr;</button>
<script>
function flipImg(btn){{
  var card=btn.parentElement;
  var layers=card.querySelectorAll('.img-layer');
  if(layers.length<2)return;
  layers[0].classList.toggle('active');
  layers[1].classList.toggle('active');
}}
{switch_js}
</script>
</body>
</html>"""

    out_path = cfg["output_dir"] / "index.html"
    out_path.write_text(html, encoding="utf-8")
    log.info("Wrote %s (%d KB)", out_path, len(html) // 1024)


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Build a bird + plant field checklist for any location and date.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Example:
  python3 field_checklist.py \\
    --place "Chappaquiddick Island" \\
    --date 2026-04-28 \\
    --lat 41.3636 --lng -70.5025 \\
    --ebird-key YOUR_KEY \\
    --moon "Waxing gibbous, 88%%. Full moon May 1." \\
    --tides "Low 5:00 AM ... High 10:30 AM ..."
""",
    )
    parser.add_argument("--place", required=True, help="Place name for the checklist header")
    parser.add_argument("--date", required=True, help="Target date (YYYY-MM-DD)")
    parser.add_argument("--lat", type=float, required=True, help="Latitude")
    parser.add_argument("--lng", type=float, required=True, help="Longitude")
    parser.add_argument("--radius", type=int, default=20, help="Search radius in km (default: 20)")
    parser.add_argument("--ebird-key", default=os.environ.get("EBIRD_API_KEY", ""),
                        help="eBird API key (or set EBIRD_API_KEY env var)")
    parser.add_argument("--moon", default="", help="Moon phase text for trip info")
    parser.add_argument("--tides", default="", help="Tide data text for trip info")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output directory (default: output/<place-slug>)")
    parser.add_argument("--skip-images", action="store_true", help="Skip downloading images")
    parser.add_argument("--birds-only", action="store_true", help="Only run bird pipeline")
    parser.add_argument("--plants-only", action="store_true", help="Only run plant pipeline")

    args = parser.parse_args()

    try:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        parser.error("Date must be YYYY-MM-DD format")

    do_birds = not args.plants_only
    do_plants = not args.birds_only

    if do_birds and not args.ebird_key:
        parser.error("--ebird-key is required for birds (or set EBIRD_API_KEY env var). "
                      "Get a free key at https://ebird.org/api/keygen")

    output_dir = args.output or Path("output") / place_slug(args.place)
    output_dir.mkdir(parents=True, exist_ok=True)

    cfg = {
        "place": args.place,
        "date": target_date,
        "lat": args.lat,
        "lng": args.lng,
        "radius": args.radius,
        "ebird_key": args.ebird_key,
        "moon": args.moon,
        "tides": args.tides,
        "output_dir": output_dir,
        "skip_images": args.skip_images,
    }

    log.info("Field Checklist Builder")
    log.info("  Place:  %s", cfg["place"])
    log.info("  Date:   %s", target_date.strftime("%Y-%m-%d"))
    log.info("  Coords: %.4f, %.4f (radius %d km)", cfg["lat"], cfg["lng"], cfg["radius"])
    log.info("  Output: %s", output_dir)
    log.info("")

    birds = []
    plants = []

    if do_birds:
        birds = run_birds(cfg)

    if do_plants:
        plants = run_plants(cfg)

    log.info("\n" + "=" * 60)
    log.info("Generating combined HTML...")
    generate_html(birds, plants, cfg)

    log.info("\n" + "=" * 60)
    log.info("Done!")
    if birds:
        log.info("  Birds:  %d species", len(birds))
    if plants:
        log.info("  Plants: %d species", len(plants))
    log.info("  Output: %s", output_dir)
    log.info("  Open:   %s", output_dir / "index.html")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
