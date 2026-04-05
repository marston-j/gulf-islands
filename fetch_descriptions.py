#!/usr/bin/env python3
"""
Fetch missing species descriptions for the Gulf Islands checklist.

Sources (cascading):
  1. Wikipedia MediaWiki API (species-level article)
  2. Wikipedia MediaWiki API (genus-level article)
  3. iNaturalist taxon wikipedia_summary

Caches results in .desc_cache.json. Injects <p class='description'> into
the target HTML files.

Usage:
    python3 fetch_descriptions.py
    python3 fetch_descriptions.py --dry-run   # fetch only, don't inject
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import time

import requests

CACHE_PATH = os.path.join(os.path.dirname(__file__),
                          "output", "gulf-islands", ".desc_cache.json")
INDEX_PATH = os.path.join(os.path.dirname(__file__),
                          "output", "gulf-islands", "index.html")
PREMAP_PATH = os.path.join(os.path.dirname(__file__),
                           "output", "gulf-islands", ".index_pre_map.html")
UA = {"User-Agent": "GulfIslandsChecklist/1.0 (field checklist project)"}
RATE = 0.55  # seconds between requests


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# ── Wikipedia ────────────────────────────────────────────────────────

def fetch_wikipedia(title, sentences=5):
    """Fetch a plain-text intro extract from Wikipedia."""
    params = {
        "action": "query",
        "titles": title,
        "prop": "extracts",
        "exintro": "true",
        "explaintext": "true",
        "exsentences": str(sentences),
        "redirects": "1",
        "format": "json",
    }
    try:
        r = requests.get("https://en.wikipedia.org/w/api.php",
                         params=params, headers=UA, timeout=12)
        if r.status_code != 200:
            return ""
        pages = r.json().get("query", {}).get("pages", {})
        for pid, page in pages.items():
            if pid != "-1":
                text = page.get("extract", "").strip()
                if len(text) > 30:
                    return text
    except Exception:
        pass
    return ""


def fetch_inat_summary(sci_name):
    """Try iNaturalist taxon endpoint for a wikipedia_summary."""
    try:
        r = requests.get("https://api.inaturalist.org/v1/taxa",
                         params={"q": sci_name, "rank": "species",
                                 "per_page": "1"},
                         headers=UA, timeout=12)
        if r.status_code != 200:
            return ""
        results = r.json().get("results", [])
        if results:
            return (results[0].get("wikipedia_summary") or "").strip()
    except Exception:
        pass
    return ""


def get_description(sci_name, cache):
    """Cascade: species Wikipedia → genus Wikipedia → iNaturalist."""
    key = sci_name.strip()
    if key in cache and cache[key]:
        return cache[key]

    # 1. Species-level Wikipedia
    desc = fetch_wikipedia(key)
    if desc:
        cache[key] = desc
        return desc
    time.sleep(RATE)

    # 2. Genus-level Wikipedia
    genus = key.split()[0] if " " in key else key
    desc = fetch_wikipedia(genus, sentences=3)
    if desc:
        cache[key] = desc
        return desc
    time.sleep(RATE)

    # 3. iNaturalist
    desc = fetch_inat_summary(key)
    if desc:
        cache[key] = desc
        return desc
    time.sleep(RATE)

    cache[key] = ""
    return ""


# ── HTML parsing ─────────────────────────────────────────────────────

def find_missing(html_text):
    """Return list of (common_name, sci_name) for plant/lichen cards
    that lack a <p class='description'> block."""
    plant_start = html_text.find('id="panel-plants"')
    plant_end = html_text.find('id="panel-map"')
    if plant_start == -1 or plant_end == -1:
        return []
    section = html_text[plant_start:plant_end]

    raw_cards = re.split(r'<div class="bird-card"', section)
    missing = []
    for card in raw_cards[1:]:
        if "class='description'" in card:
            continue
        m_name = re.search(r"<h3>(.*?)</h3>", card)
        m_sci = re.search(r'<span class="latin">(.*?)</span>', card)
        if m_name and m_sci:
            common = html_mod.unescape(m_name.group(1))
            sci = html_mod.unescape(m_sci.group(1))
            missing.append((common, sci))
    return missing


def inject_descriptions(html_text, desc_map):
    """Inject <p class='description'> into cards that are missing one.
    Returns (new_html, count_injected)."""
    injected = 0

    def _inject_card(match):
        nonlocal injected
        card = match.group(0)
        if "class='description'" in card:
            return card
        m_sci = re.search(r'<span class="latin">(.*?)</span>', card)
        if not m_sci:
            return card
        sci = html_mod.unescape(m_sci.group(1))
        desc = desc_map.get(sci, "")
        if not desc:
            return card
        safe = html_mod.escape(desc)
        tag = f"\n<p class='description'>{safe}</p>"
        # Insert after the last </div> of meta-row, before card-footer
        # Pattern: </div>\n\n<div class="card-footer"> OR </div>\n</div>
        # (lichens may lack card-footer)
        if '<div class="card-footer">' in card:
            card = card.replace(
                '<div class="card-footer">',
                f'{tag}\n\n<div class="card-footer">',
                1,
            )
            injected += 1
        elif card.rstrip().endswith("</div>"):
            card = card.rstrip() + tag + "\n"
            injected += 1
        return card

    plant_start = html_text.find('id="panel-plants"')
    plant_end = html_text.find('id="panel-map"')
    before = html_text[:plant_start]
    section = html_text[plant_start:plant_end]
    after = html_text[plant_end:]

    new_section = re.sub(
        r'<div class="bird-card".*?(?=<div class="bird-card"|<div class="panel"|$)',
        _inject_card,
        section,
        flags=re.DOTALL,
    )
    return before + new_section + after, injected


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch descriptions but do not modify HTML files")
    args = parser.parse_args()

    print("Loading cache...")
    cache = load_cache()

    print(f"Reading {INDEX_PATH}...")
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    missing = find_missing(html)
    print(f"Found {len(missing)} plant/lichen cards without descriptions.\n")

    already = sum(1 for _, sci in missing if cache.get(sci))
    to_fetch = [(c, s) for c, s in missing if not cache.get(s)]
    print(f"  Cached: {already}   To fetch: {len(to_fetch)}\n")

    for i, (common, sci) in enumerate(to_fetch, 1):
        print(f"  [{i}/{len(to_fetch)}] {sci} ({common})...", end=" ", flush=True)
        desc = get_description(sci, cache)
        if desc:
            print(f"OK ({len(desc)} chars)")
        else:
            print("NO DESC")
        save_cache(cache)
        time.sleep(RATE)

    desc_map = {sci: cache.get(sci, "") for _, sci in missing}
    filled = sum(1 for v in desc_map.values() if v)
    print(f"\nDescriptions available: {filled}/{len(missing)}")

    if args.dry_run:
        print("Dry-run mode — not modifying HTML files.")
        return

    for path in [INDEX_PATH, PREMAP_PATH]:
        if not os.path.exists(path):
            print(f"  Skipping {path} (not found)")
            continue
        print(f"\nInjecting into {os.path.basename(path)}...")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        new_content, count = inject_descriptions(content, desc_map)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"  Injected {count} descriptions.")

    save_cache(cache)
    print("\nDone.")


if __name__ == "__main__":
    main()
