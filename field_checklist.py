#!/usr/bin/env python3
"""
Gulf Islands Field Checklist Builder

Generates a combined bird + plant field checklist for any location and date.

Data sources:
  Birds  — eBird API (species list) + Cornell All About Birds (detail, images)
  Plants — iNaturalist (species list, images) + Go Botany + USDA PLANTS +
           Missouri Botanical Garden Plant Finder
  Both   — iNaturalist histograms (seasonality bars)

Produces:
  <output>/<place-slug>/
    images/Birds/<Group>/<Species>.jpg
    images/Plants/<Group>/<Species>_1.jpg, _2.jpg
    birds.csv
    plants.csv
    index.html   (combined page with Birds/Plants/Map toggle)

Usage:
  python3 field_checklist.py \\
    --place "Gulf Islands" \\
    --date 2026-04-28 \\
    --lat 30.3298 --lng -86.1650 \\
    --ebird-key YOUR_KEY
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
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

# ── Constants ──────────────────────────────────────────────────────────

CORNELL_CDN = "https://cdn.download.ams.birds.cornell.edu/api/v1/asset/{asset_id}/640"
CORNELL_GUIDE = "https://www.allaboutbirds.org/guide/{slug}"
CORNELL_ID_URL = "https://www.allaboutbirds.org/guide/{slug}/id"
CORNELL_SOUNDS_URL = "https://www.allaboutbirds.org/guide/{slug}/sounds"
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
    "Conservation Concern",
    "Shorebirds", "Waterbirds", "Wading Birds", "Raptors",
    "Swallows", "Warblers", "Sparrows", "Songbirds",
]

BIRD_GROUP_COLORS = {
    "Conservation Concern": "#B8860B",
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

PLANT_GROUP_ORDER = ["Conservation Concern", "Wildflowers & Herbs", "Ferns", "Lichens & Mosses", "Shrubs", "Trees", "Vines"]

PLANT_GROUP_COLORS = {
    "Conservation Concern": "#B8860B",
    "Wildflowers & Herbs": "#6a8e3f",
    "Ferns": "#3a7a5a",
    "Lichens & Mosses": "#7a9a7a",
    "Shrubs": "#8a6a3a",
    "Trees": "#5a6a3a",
    "Vines": "#7a5a6a",
}

SEA_LIFE_GROUP_ORDER = ["Crustaceans", "Seaweed & Algae", "Shells", "Fish", "Rays & Skates", "Jellyfish & Corals", "Echinoderms", "Marine Mammals", "Mollusks", "Marine Reptiles"]

SEA_LIFE_GROUP_COLORS = {
    "Fish": "#2E6B94",
    "Shells": "#C4956A",
    "Mollusks": "#8B7348",
    "Crustaceans": "#B85C38",
    "Jellyfish & Corals": "#7B5EA7",
    "Echinoderms": "#5A8A6A",
    "Seaweed & Algae": "#3a7a5a",
    "Rays & Skates": "#4A7B8C",
    "Marine Reptiles": "#6B7820",
    "Marine Mammals": "#505060",
}

SEAWEED_EDIBILITY: dict[str, str] = {
    "Sargassum fluitans":          "edible",
    "Gracilaria cervicornis":      "edible",
    "Gracilaria tikvahiae":        "edible",
    "Hypnea musciformis":          "edible",
    "Hypnea spinella":             "edible",
    "Gelidium crinale":            "edible",
    "Gelidium pusillum":           "edible",
    "Gelidium spinosum":           "edible",
    "Laurencia obtusa":            "edible",
    "Laurencia intricata":         "not edible",
    "Acanthophora spicifera":      "edible",
    "Acanthophora muscoides":      "not edible",
    "Agardhiella subulata":        "edible",
    "Champia parvula":             "not edible",
    "Hildenbrandia rubra":         "not edible",
    "Amphiroa fragilissima":       "not edible",
    "Amphiroa tribulus":           "not edible",
    "Jania adhaerens":             "not edible",
    "Jania capillacea":            "not edible",
    "Chondria dasyphylla":         "not edible",
    "Spyridia filamentosa":        "not edible",
    "Crouania attenuata":          "not edible",
    "Erythrocladia irregularis":   "not edible",
    "Erythrotrichia carnea":       "not edible",
    "Catenella caespitosa":        "not edible",
    "Chroodactylon ornatum":       "not edible",
    "Stylonema alsidii":           "not edible",
    "Helminthocladia calvadosii":  "not edible",
    "Trichogloeopsis pedicellata": "not edible",
    "Bostrychia radicans":         "not edible",
    "Bostrychia tenella":          "not edible",
}

SEAWEED_CHARACTERISTICS: dict[str, str] = {
    "Sargassum fluitans": (
        "A free-floating brown alga forming large pelagic rafts in the Gulf of Mexico. "
        "Thallus bushy, 10\u201360 cm, with flattened blades bearing air bladders (pneumatocysts) "
        "on short stalks. Olive-brown to golden. Provides critical nursery habitat for juvenile "
        "fish, sea turtles, and invertebrates. One of the dominant species of the Sargasso Sea "
        "gyre, frequently washing ashore on Gulf beaches."
    ),
    "Gracilaria cervicornis": (
        "A cartilaginous red alga, 10\u201325 cm tall, with cylindrical to slightly flattened "
        "branches that fork dichotomously or irregularly. Dark red to brownish-purple. Found "
        "attached to hard substrates in shallow subtidal zones. Source of agar; widely "
        "harvested in tropical regions for food and phycocolloid production."
    ),
    "Gracilaria tikvahiae": (
        "A fleshy red alga, 10\u201330 cm, with cylindrical, irregularly branched thalli. "
        "Color ranges from dark red to green depending on light and nutrients. Common in "
        "estuaries and protected bays on shells, rocks, and mud. Important commercial species "
        "for agar extraction in the western Atlantic."
    ),
    "Hypnea musciformis": (
        "A wiry red alga, 5\u201320 cm, with cylindrical branches bearing characteristic "
        "hook-shaped (circinate) branch tips used for attachment to other algae. Pale red to "
        "yellowish. Epiphytic or free-living on rocky substrates in intertidal and shallow "
        "subtidal zones. Source of kappa-carrageenan."
    ),
    "Hypnea spinella": (
        "A small, densely branched red alga, 3\u201310 cm, with rigid spinous branchlets. "
        "Thallus cylindrical, stiff, pinkish-red to dark red. Grows on rocks and other algae "
        "in the lower intertidal to shallow subtidal. Distinguished from H. musciformis by its "
        "short, spine-like lateral branchlets rather than hooks."
    ),
    "Gelidium crinale": (
        "A small, tufted red alga forming dense mats, 1\u20134 cm tall. Thallus compressed to "
        "flattened, with pinnate branching. Dark brownish-red. Attaches firmly to rocks in the "
        "upper subtidal zone. Used commercially as an agar source. One of the most common "
        "Gelidium species worldwide."
    ),
    "Gelidium pusillum": (
        "A minute, creeping red alga forming dense turfs, typically 0.5\u20132 cm tall. "
        "Thallus flattened, irregularly branched, dark red to black. Encrusts rocks and shells "
        "in the intertidal zone, tolerating desiccation. Important contributor to intertidal "
        "turf communities."
    ),
    "Gelidium spinosum": (
        "An erect, wiry red alga, 3\u20138 cm tall, with stiff, sparsely branched cylindrical "
        "axes. Dark purplish-red. Attached to rocks in exposed intertidal and shallow subtidal "
        "habitats. Distinguished from other Gelidium species by its rigid, spine-tipped branches."
    ),
    "Laurencia obtusa": (
        "A fleshy red alga, 5\u201315 cm, with cylindrical, irregularly alternate branches that "
        "have characteristically truncated (blunt) tips. Purplish-red to yellowish-green. Grows "
        "on rocks and coral rubble in the lower intertidal and subtidal. Produces halogenated "
        "secondary metabolites with antimicrobial and anti-herbivore properties."
    ),
    "Laurencia intricata": (
        "A bushy red alga, 5\u201312 cm, with densely entangled cylindrical branches forming "
        "intricate clumps. Dark purplish-red to brown. Attached to rocks in intertidal pools "
        "and shallow subtidal areas. Rich in bioactive halogenated terpenes."
    ),
    "Acanthophora spicifera": (
        "An erect, spiny red alga, 5\u201320 cm tall, with cylindrical main axes bearing "
        "numerous short, spine-like branchlets in a spiral arrangement. Color ranges from "
        "greenish-brown to dark red. Attached to rocks, shells, and mangrove roots in "
        "shallow bays and lagoons. Edible; consumed in Hawaiian and Pacific Island cuisine."
    ),
    "Acanthophora muscoides": (
        "A coarsely branched red alga, 5\u201315 cm, similar to A. spicifera but with shorter, "
        "more densely packed spine-like branchlets giving a rough texture. Brownish-red. "
        "Grows on hard substrates in sheltered intertidal and shallow subtidal habitats."
    ),
    "Agardhiella subulata": (
        "A soft, fleshy red alga, 10\u201325 cm, with cylindrical, subulate (awl-shaped) "
        "branches tapering to fine points. Bright to dark red. Common in estuaries and "
        "protected bays attached to shells and rocks. An important agar-producing species "
        "in the western Atlantic."
    ),
    "Champia parvula": (
        "A delicate, hollow red alga, 2\u20138 cm tall, with segmented, barrel-shaped "
        "branchlets giving a beaded appearance. Translucent pink to rosy-red. Grows "
        "epiphytically on larger algae and seagrasses in the subtidal zone."
    ),
    "Hildenbrandia rubra": (
        "A crustose red alga forming thin, smooth, blood-red to dark maroon crusts on "
        "rocks, shells, and pebbles. Thallus firmly adnate, only 0.1\u20130.5 mm thick. "
        "Common in the intertidal and shallow subtidal; one of the most widespread "
        "crustose red algae worldwide. Perennial and extremely slow-growing."
    ),
    "Amphiroa fragilissima": (
        "A calcareous red alga, 3\u20138 cm, with slender, dichotomously branched segments "
        "joined by flexible, uncalcified joints (genicula). Pale pink to whitish due to "
        "heavy calcium carbonate deposition. Brittle when dry. Found on reef rubble and "
        "in seagrass beds in tropical-subtropical waters."
    ),
    "Amphiroa tribulus": (
        "A robust, calcareous red alga, 3\u201310 cm, with thick, dichotomously branched "
        "segments heavily encrusted with calcium carbonate. Pale pink to grayish-white. "
        "Intergenicula cylindrical to slightly compressed with distinct, dark genicula. "
        "Grows on reef substrates and rocky bottoms in the shallow subtidal."
    ),
    "Jania adhaerens": (
        "A tiny, calcareous red alga, 1\u20133 cm, forming delicate tufts with very fine, "
        "dichotomously branched segments separated by conspicuous genicula. Pale pink to "
        "white. Typically epiphytic on larger fleshy algae in the intertidal and subtidal."
    ),
    "Jania capillacea": (
        "A minute, hair-like calcareous red alga, 0.5\u20132 cm, with extremely fine, "
        "dichotomous branching and pronounced genicula. White to pale pink. Forms soft, "
        "feathery tufts on other algae and seagrasses. The finest-branched species in "
        "the genus, common in warm waters."
    ),
    "Chondria dasyphylla": (
        "An erect red alga, 5\u201315 cm tall, with cylindrical, alternately branched axes. "
        "Branches taper to acute tips with a conspicuous apical pit (trichoblast tuft). "
        "Yellowish-brown to dark red. Grows on rocks and epiphytically in the lower "
        "intertidal and subtidal. Distinguished by its club-shaped branch tips."
    ),
    "Spyridia filamentosa": (
        "A delicate, filamentous red alga, 5\u201320 cm, with a main axis bearing whorls of "
        "fine, banded cortical filaments giving a fuzzy appearance. Pale pink to rosy-red. "
        "Widely distributed in warm waters, growing on rocks, other algae, and seagrass "
        "blades. Often abundant in eutrophic conditions."
    ),
    "Crouania attenuata": (
        "A soft, filamentous red alga, 2\u20138 cm, with delicate axes bearing whorls of "
        "dichotomously branched cortical filaments that taper towards branch tips. Rosy-pink "
        "to translucent. Grows epiphytically on larger algae and seagrasses in protected "
        "subtidal habitats. A graceful, lace-like alga."
    ),
    "Erythrocladia irregularis": (
        "A microscopic to very small crustose red alga forming thin, irregular, one-cell-thick "
        "discs on other algae and smooth substrates. Cells irregularly arranged in a monostromatic "
        "layer. Pinkish-red. Common epiphyte in intertidal and subtidal zones, often overlooked "
        "due to its minute size (typically < 1 mm)."
    ),
    "Erythrotrichia carnea": (
        "A simple, unbranched filamentous red alga, 0.5\u20132 cm tall, consisting of a "
        "uniseriate (single cell row) filament attached by a basal disc. Pale pink to bright "
        "red. One of the most common and widespread microscopic red algae, epiphytic on "
        "larger algae, seagrasses, and artificial substrates."
    ),
    "Catenella caespitosa": (
        "A small, dark-colored red alga, 1\u20133 cm, forming dense, wiry mats on mangrove "
        "roots, salt marsh plants, and upper intertidal rocks. Thallus consists of compressed, "
        "bead-like segments connected at narrow constrictions. Dark olive-brown to blackish-red. "
        "Highly tolerant of desiccation and brackish conditions."
    ),
    "Chroodactylon ornatum": (
        "A microscopic red alga forming small, branched filaments, 1\u20135 mm tall. Cells "
        "embedded in a gelatinous sheath, giving the thallus a beaded appearance. Pink to "
        "rose-colored. Epiphytic on larger algae and hard surfaces in the intertidal zone. "
        "A member of the primitive order Stylonematales."
    ),
    "Stylonema alsidii": (
        "A minute, unbranched to sparsely branched filamentous red alga, 1\u20135 mm long. "
        "Cells embedded in a firm mucilaginous sheath, uniseriate. Reddish to brownish-purple. "
        "Common worldwide as an epiphyte on larger algae and other marine surfaces."
    ),
    "Helminthocladia calvadosii": (
        "A soft, gelatinous red alga, 5\u201320 cm, with cylindrical, sparsely branched axes "
        "that are slippery to the touch. Brownish-red to dark purple. Grows on rocks in the "
        "lower intertidal and shallow subtidal. Thallus multiaxial with a medulla of loosely "
        "arranged filaments surrounded by a cortex of radiating assimilatory filaments."
    ),
    "Trichogloeopsis pedicellata": (
        "A soft, gelatinous red alga, 3\u201312 cm, with cylindrical, dichotomously branched "
        "axes that feel lubricous. Brownish-red to dark rose. Multiaxial construction with "
        "a loose medullary core. Grows on coral rubble and hard substrates in the subtidal "
        "zone in tropical-subtropical waters."
    ),
    "Bostrychia radicans": (
        "A small, dark red alga, 2\u20135 cm, with prostrate axes bearing erect, pinnately "
        "branched fronds. Branch tips often incurved (scorpioid). Attaches via haptera to "
        "mangrove pneumatophores, salt marsh stems, and intertidal rocks. Tolerates wide "
        "salinity ranges. A key indicator species in mangrove ecosystems."
    ),
    "Bostrychia tenella": (
        "A small, filamentous red alga, 1\u20134 cm, similar to B. radicans but with finer, "
        "more delicate branching and conspicuous polysiphonous axes. Dark brownish-red. "
        "Grows on mangrove roots and pneumatophores. Distinguished by its ecorticate "
        "(uncovered) stichidia and terete branch tips."
    ),
}

SEA_LIFE_TAXON_IDS = {
    47178: "Fish",
    47114: "Shells",            # Gastropoda (sea snails, conchs, whelks)
    47584: "Shells",            # Bivalvia (clams, oysters, scallops)
    47115: "Mollusks",          # broader Mollusca (cephalopods, nudibranchs, etc.)
    85493: "Crustaceans",
    47534: "Jellyfish & Corals",
    47549: "Echinoderms",
    48222: "Seaweed & Algae",   # Chromista / Phaeophyceae + others
    57774: "Seaweed & Algae",   # Rhodophyta (red algae)
    47533: "Seaweed & Algae",   # Chlorophyta (green algae)
    73863: "Marine Reptiles",
    40151: "Marine Mammals",
}

GULF_FISH_SPECIES: list[dict] = [
    {"common_name": "Red Snapper", "scientific_name": "Lutjanus campechanus",
     "facts": "A reef-dependent demersal fish found across the Gulf of Mexico at depths of 10\u2013190 m. Can live over 50 years and reach 1 m in length. Highly valued in commercial and recreational fisheries; the species was determined overfished prior to 2010. Juveniles prefer shell and sand substrates, transitioning to reef habitats with age. Batch spawner with a 180-day spawning season peaking in summer."},
    {"common_name": "Red Grouper", "scientific_name": "Epinephelus morio",
     "facts": "A large, bottom-dwelling serranid common over rocky reefs and hard-bottom habitats in the eastern Gulf of Mexico. Reddish-brown with pale blotches. Protogynous hermaphrodite\u2014born female, some transition to male. Slow-growing, long-lived (25+ years), reaching 125 cm. Excavates seafloor sediment to create habitat used by other reef species. Commercially and recreationally important; assessed as overfished in some subpopulations."},
    {"common_name": "Atlantic Bluefin Tuna", "scientific_name": "Thunnus thynnus",
     "facts": "One of the largest bony fishes, reaching over 3 m and 680 kg. Highly migratory, entering the Gulf of Mexico to spawn from mid-April through June. Endothermic physiology enables high-speed pursuit of prey across temperature gradients. The Gulf of Mexico is the only known western Atlantic spawning ground. Severely overfished for decades; subject to strict international quotas."},
    {"common_name": "Atlantic Blue Marlin", "scientific_name": "Makaira nigricans",
     "facts": "A spectacular pelagic billfish reaching 5 m and 900 kg, the largest Atlantic istiophorid. Cobalt-blue dorsal surface with silver-white flanks. Feeds on tunas, mackerels, dolphinfish, and squids. Uses its bill to slash through schools of prey. Highly migratory throughout the tropical and temperate Atlantic. Prized game fish; the species was assessed as overfished prior to 2010."},
    {"common_name": "Atlantic Swordfish", "scientific_name": "Xiphias gladius",
     "facts": "A large, highly migratory pelagic fish with a distinctive elongated, flattened bill. Reaches 4.5 m and 650 kg. Unlike marlins, swordfish lack pelvic fins and have a single keel on each side of the caudal peduncle. Feeds at great depths during the day, ascending to surface waters at night. Commercially harvested by longline fleets in the Gulf; stocks recovered after management measures in the 2000s."},
    {"common_name": "Atlantic Sailfish", "scientific_name": "Istiophorus albicans",
     "facts": "The fastest fish in the ocean, capable of bursts over 110 km/h. Distinguished by an enormous dorsal fin (sail) that can be raised or lowered. Dark blue above, silver-white below with pale blue vertical bars. Feeds on sardines, anchovies, and squids near the surface. Common in warm Gulf of Mexico waters, especially off the Florida Panhandle. Primarily a catch-and-release sport fish."},
    {"common_name": "Red Drum", "scientific_name": "Sciaenops ocellatus",
     "facts": "A coastal demersal sciaenid reaching 1.5 m and 45 kg. Bronze to coppery color with one or more distinctive black spots at the base of the tail. Juveniles inhabit estuaries, seagrass beds, and marsh edges; adults move to nearshore Gulf waters. Produces drumming sounds using swim bladder muscles during spawning. Popular recreational species; managed under strict slot limits after severe overfishing in the 1980s."},
    {"common_name": "Tilefish", "scientific_name": "Lopholatilus chamaeleonticeps",
     "facts": "A large, colorful demersal fish inhabiting deep continental shelf and slope waters at 80\u2013540 m. Bluish-green and gold with yellow spots. Constructs and occupies burrows in clay substrate along the outer shelf, creating vertical habitat. Slow-growing, long-lived (40+ years). Flesh is prized for its delicate, sweet flavor. Commercial harvest by longline."},
    {"common_name": "King Mackerel", "scientific_name": "Scomberomorus cavalla",
     "facts": "A fast-swimming coastal pelagic scombrid reaching 1.8 m and 45 kg. Streamlined body, iridescent blue-green above, silvery sides. Highly migratory along the Gulf coast, moving inshore in spring and offshore in fall. Feeds on herrings, sardines, and squids. Supports major commercial and recreational fisheries; managed under separate Gulf and Atlantic migratory groups."},
    {"common_name": "Dolphinfish", "scientific_name": "Coryphaena hippurus",
     "facts": "Also called mahi-mahi. A brilliantly colored pelagic fish\u2014golden-green and blue\u2014reaching 2 m. Among the fastest-growing fish, reaching maturity in 4\u20135 months. Found near floating Sargassum, debris, and weedlines in warm Gulf waters. Feeds on flyingfish, squids, and crustaceans. Highly valued in both commercial and recreational fisheries for its excellent flesh."},
    {"common_name": "Striped Mullet", "scientific_name": "Mugil cephalus",
     "facts": "A torpedo-shaped coastal fish reaching 120 cm. Olive-green above with silvery sides bearing dark longitudinal stripes. Unique among Gulf fishes as a detritivore/herbivore, feeding on microalgae, organic detritus, and biofilms by ingesting sediment. Occurs in estuaries, rivers, and nearshore marine waters. Supports one of the most important commercial cast-net fisheries in the Gulf. Roe is considered a delicacy."},
    {"common_name": "Greater Amberjack", "scientific_name": "Seriola dumerili",
     "facts": "The largest of the jacks in the Atlantic, reaching 2 m and 80 kg. Amber-colored stripe from nose through eye to dorsal fin; dark olive above, silvery below. Coastal pelagic, commonly found near reefs, wrecks, and oil platforms at 18\u2013100 m. Aggressive predator of fish and invertebrates. Important commercial and recreational species; assessed as overfished and undergoing management rebuilding."},
    {"common_name": "Gulf Menhaden", "scientific_name": "Brevoortia patronus",
     "facts": "A small, filter-feeding clupeid critical to Gulf ecosystem energy transfer, consuming phytoplankton and zooplankton. Flat body, dark spot behind gill cover. Supports one of the largest fisheries in the U.S. by weight (400\u2013600 kilotons annually), used for fishmeal, fish oil, and bait rather than direct consumption. Estuarine-dependent; spawns offshore in winter, juveniles develop in bays."},
    {"common_name": "Spotted Seatrout", "scientific_name": "Cynoscion nebulosus",
     "facts": "A popular inshore game fish reaching 100 cm, with silvery sides marked by distinctive round black spots on the upper body and dorsal fins. Inhabits seagrass beds, oyster reefs, and tidal creeks. Males produce drumming sounds to attract mates during spring and summer spawning. Ambush predator feeding on shrimp, crabs, and small fish. Among the top recreational species in the Gulf."},
    {"common_name": "Cobia", "scientific_name": "Rachycentron canadum",
     "facts": "A large, pelagic-coastal fish reaching 2 m and 68 kg. Dark brown with a distinctive flat head and bold black lateral stripe. Often found near buoys, pilings, and floating debris, and will follow large rays, sharks, and turtles. Solitary or in small groups. Fast-growing, reaching sexual maturity in 2\u20133 years. Excellent table fare with firm, white flesh. Increasingly aquacultured worldwide."},
    {"common_name": "Gray Triggerfish", "scientific_name": "Balistes capriscus",
     "facts": "A distinctive reef-associated fish with an oval, laterally compressed body and rough skin, reaching 60 cm. Gray to olive with blue markings. Named for the locking spine mechanism on the first dorsal fin. Inhabits natural and artificial reefs, especially oil platforms. Feeds on crabs, sea urchins, and mollusks using powerful jaw. Popular pan fish; managed under the GMFMC Reef Fish FMP."},
    {"common_name": "Wahoo", "scientific_name": "Acanthocybium solandri",
     "facts": "An extremely fast pelagic scombrid reaching 2.5 m and 83 kg, capable of speeds over 95 km/h. Iridescent blue above with vivid blue vertical bars on silvery sides. Elongated, torpedo-shaped body with a long, pointed snout. Solitary or in loose aggregations near current edges, temperature breaks, and blue water. Feeds on squid and fish. Prized as one of the finest-eating offshore game fish."},
    {"common_name": "Southern Flounder", "scientific_name": "Paralichthys lethostigma",
     "facts": "The largest Gulf flatfish, reaching 90 cm and 9 kg. Left-eyed, with cryptic olive-brown coloration on the eyed side. Masters of camouflage, lying partially buried on sandy and muddy bottoms in estuaries, bays, and nearshore Gulf waters. Ambush predators of shrimp and small fish. Important recreational gigging and hook-and-line species. Migrates offshore to spawn in fall and winter."},
    {"common_name": "Black Drum", "scientific_name": "Pogonias cromis",
     "facts": "A large, heavy-bodied sciaenid reaching 170 cm and 51 kg, the largest member of the drum family. Grayish-black with barbels on the lower jaw. Juveniles bear 4\u20136 dark vertical bars. Pharyngeal teeth crush oysters, clams, and crabs. Found in estuaries, nearshore reefs, and around bridge pilings. Produces resonant drumming with swim bladder muscles. Long-lived, reaching 60+ years."},
    {"common_name": "Sheepshead", "scientific_name": "Archosargus probatocephalus",
     "facts": "A distinctive sparid with 5\u20137 bold black vertical bars on silvery sides, reaching 90 cm and 10 kg. Human-like incisors and heavy molars for crushing barnacles, oysters, clams, and crabs. Found near pilings, jetties, bridge supports, and oyster reefs. Spawns offshore in late winter to early spring. Excellent eating with firm, sweet white flesh."},
    {"common_name": "Vermilion Snapper", "scientific_name": "Rhomboplites aurorubens",
     "facts": "A mid-sized snapper reaching 60 cm. Vermilion-red above fading to pale pink below, with yellow streaks along scale rows. Occurs in large schools over hard-bottom reefs and rocky ledges at 40\u2013300 m. Fast-growing compared to red snapper, reaching maturity by age 2. Feeds on small fish, shrimp, crabs, and cephalopods. Commercially harvested by hook-and-line; managed under the GMFMC Reef Fish FMP."},
    {"common_name": "Lane Snapper", "scientific_name": "Lutjanus synagris",
     "facts": "A colorful, mid-sized snapper reaching 60 cm with a rosy body marked by yellow horizontal stripes and a prominent dark spot below the soft dorsal fin. Common over reefs, seagrass beds, and sandy bottoms in nearshore and offshore waters. Spawns from March through September. Feeds on fish, shrimp, crabs, and worms. Popular with recreational anglers and valued commercially."},
    {"common_name": "Gag Grouper", "scientific_name": "Mycteroperca microlepis",
     "facts": "A large serranid reaching 145 cm and 36 kg. Gray-brown with darker kiss-shaped markings forming a reticulated pattern. Protogynous hermaphrodite. Juveniles inhabit seagrass beds; adults transition to offshore reefs and hard-bottom habitats. Forms spawning aggregations on the continental shelf edge in winter. Excellent food fish; one of the most important commercially harvested groupers in the eastern Gulf."},
    {"common_name": "Tripletail", "scientific_name": "Lobotes surinamensis",
     "facts": "A distinctive coastal pelagic fish reaching 110 cm and 20 kg. Named for its elongated dorsal and anal fins that together with the caudal fin give the appearance of three tails. Dark olive to brown, sometimes mottled. Floats on its side near the surface alongside floating debris, buoys, and crab trap floats\u2014mimicking a dead leaf. Excellent table fare with firm, white flesh."},
    {"common_name": "Florida Pompano", "scientific_name": "Trachinotus carolinus",
     "facts": "A deep-bodied, laterally compressed jack reaching 64 cm and 3.6 kg. Silvery overall with a greenish-blue dorsal surface and yellow on the throat, belly, and fin margins. Inhabits surf zones, sandy beaches, and passes. Feeds on sand fleas (mole crabs), clams, and small crustaceans. Among the highest-priced commercial food fish in the Gulf due to its sweet, delicate flesh. Popular surf fishing target."},
    {"common_name": "Spanish Mackerel", "scientific_name": "Scomberomorus maculatus",
     "facts": "A streamlined, fast-swimming coastal pelagic reaching 91 cm. Iridescent blue-green above with silvery sides covered in golden-orange oval spots. Highly migratory along the coast, schooling in large numbers near beaches, passes, and piers. Feeds on small fish and shrimp. Important commercial and recreational species; flesh is oily and best prepared smoked or grilled."},
]

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

TREE_SUBGROUPS = {
    "Arecaceae": "Palms",
    "Fagaceae": "Oaks",
    "Pinaceae": "Pines & Conifers",
    "Cupressaceae": "Pines & Conifers",
    "Taxaceae": "Pines & Conifers",
    "Taxodiaceae": "Pines & Conifers",
    "Podocarpaceae": "Pines & Conifers",
    "Magnoliaceae": "Magnolias",
    "Annonaceae": "Magnolias",
}
TREE_SUBGROUP_ORDER = ["Palms", "Oaks", "Pines & Conifers", "Magnolias", "Other Broadleaf"]

FERN_ANCESTOR_ID = 121943  # Polypodiopsida
LICHEN_ANCESTOR_ID = 54743  # Lecanoromycetes (lichenized fungi)
BRYOPHYTE_ANCESTOR_ID = 311295  # Bryophyta (mosses)
FUNGI_ANCESTOR_ID = 47170  # Fungi

INAT_FAMILY_IDS: dict[int, str] = {
    47562: "Pinaceae", 47374: "Cupressaceae", 47852: "Fagaceae",
    48867: "Arecaceae", 53581: "Magnoliaceae", 48809: "Lauraceae",
    47733: "Oleaceae", 58321: "Sapindaceae", 54497: "Juglandaceae",
    49155: "Betulaceae", 47851: "Ulmaceae", 50898: "Platanaceae",
    47855: "Malvaceae", 53566: "Nyssaceae", 56020: "Hamamelidaceae",
    49159: "Altingiaceae", 47860: "Salicaceae", 51227: "Moraceae",
    52389: "Simaroubaceae", 48833: "Bignoniaceae", 51771: "Annonaceae",
    48846: "Rhizophoraceae", 49185: "Clusiaceae", 136334: "Podocarpaceae",
    48798: "Ericaceae", 50927: "Caprifoliaceae", 48099: "Aquifoliaceae",
    48862: "Myricaceae", 56058: "Clethraceae", 60771: "Grossulariaceae",
    48828: "Adoxaceae", 48096: "Cornaceae", 55574: "Cistaceae",
    49178: "Rubiaceae", 52281: "Verbenaceae",
    48895: "Vitaceae", 49990: "Smilacaceae", 72736: "Menispermaceae",
    47706: "Convolvulaceae",
}

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


_TITLE_CASE_LOWER = {"a", "an", "and", "as", "at", "but", "by", "for", "in",
                      "nor", "of", "on", "or", "so", "the", "to", "up", "yet"}


def title_case_common_name(name: str) -> str:
    """Title-case a common name, keeping articles/prepositions lowercase
    except at the start. Handles hyphens and apostrophes correctly."""
    if not name:
        return name
    words = name.split()
    result = []
    for i, word in enumerate(words):
        if "-" in word:
            parts = word.split("-")
            parts = [p[:1].upper() + p[1:] if p else p for p in parts]
            word = "-".join(parts)
            result.append(word)
        elif i == 0:
            result.append(word[:1].upper() + word[1:])
        elif word.lower() in _TITLE_CASE_LOWER:
            result.append(word.lower())
        elif "'" in word:
            result.append(word[:1].upper() + word[1:])
        else:
            result.append(word[:1].upper() + word[1:])
    return " ".join(result)


def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def esc_img_path(path: str) -> str:
    """Encode a local image path for use in HTML/CSS url(), handling apostrophes."""
    return path.replace("'", "%27").replace("&", "&amp;").replace('"', "%22")


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


def scrape_cornell_sounds(slug: str) -> dict:
    """Scrape the All About Birds sounds page for the first audio ML asset ID."""
    url = CORNELL_SOUNDS_URL.format(slug=quote(slug, safe="/_-"))
    info = {"audio_ml_id": "", "sounds_url": url}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return info
        html = resp.text
    except Exception:
        return info

    ml_ids = re.findall(r'asset/(\d+)', html)
    for ml_id in ml_ids:
        if int(ml_id) > 10000:
            info["audio_ml_id"] = ml_id
            break
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
                           radius_km: int, months: str,
                           require_native: bool = True) -> dict:
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
            "per_page": 200,
            "page": page,
        }
        if require_native:
            params["native"] = "true"
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
        if urls:
            return urls
    except Exception:
        pass
    # Fallback: fetch default_photo from the taxa detail endpoint
    try:
        resp = requests.get(f"{INAT_API}/taxa/{taxon_id}",
                            headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            for t in resp.json().get("results", []):
                dp = t.get("default_photo", {})
                url = dp.get("medium_url") or dp.get("url", "")
                if url:
                    return [url.replace("/medium.", "/large.").replace("/square.", "/large.")]
    except Exception:
        pass
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
        raw = strip_tags(m.group(1))
        raw = re.sub(r"(?i)exact status definitions.*?check with your state\.?\s*", "", raw)
        info["conservation"] = raw.strip()[:300]

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


USDA_PLANTS_URL = "https://plantsservices.sc.egov.usda.gov/api/PlantProfile?symbol={symbol}"
USDA_SEARCH_URL = "https://plantsservices.sc.egov.usda.gov/api/PlantSearch"


def scrape_usda_plants(genus: str, species_epithet: str) -> dict:
    """Query USDA PLANTS Database for supplemental plant data."""
    info = {
        "facts": "", "habitat": "", "native_status": "", "wetland_indicator": "",
        "growth_habit": "", "duration": "", "family": "", "source": "USDA PLANTS",
    }
    sci_name = f"{genus} {species_epithet}"
    try:
        resp = requests.get(
            USDA_SEARCH_URL,
            params={"SearchText": sci_name, "Rone": "1", "pagesize": "5"},
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            return info
        html = resp.text
    except Exception:
        return info

    symbol_match = re.search(
        rf'href="/home/plantProfile\?symbol=([A-Z0-9]+)"[^>]*>\s*'
        rf'{re.escape(genus)}[^<]*{re.escape(species_epithet)}',
        html, re.IGNORECASE,
    )
    if not symbol_match:
        return info
    symbol = symbol_match.group(1)

    try:
        resp = requests.get(
            f"https://plants.usda.gov/home/plantProfile?symbol={symbol}",
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            return info
        html = resp.text
    except Exception:
        return info

    for label, key in [
        ("Native Status", "native_status"),
        ("Wetland Indicator", "wetland_indicator"),
        ("Growth Habit", "growth_habit"),
        ("Duration", "duration"),
    ]:
        m = re.search(
            rf'{re.escape(label)}\s*</(?:dt|th|td|span|div)>\s*<(?:dd|td|span|div)[^>]*>(.*?)</(?:dd|td|span|div)>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m:
            info[key] = strip_tags(m.group(1))[:200]

    m = re.search(r'Family\s*</(?:dt|th|td|span)>\s*<(?:dd|td|span)[^>]*>(.*?)</', html, re.DOTALL | re.IGNORECASE)
    if m:
        info["family"] = strip_tags(m.group(1))[:80]

    parts = []
    if info["growth_habit"]:
        parts.append(info["growth_habit"])
    if info["duration"]:
        parts.append(info["duration"])
    if info["native_status"]:
        parts.append(info["native_status"])
    if info["wetland_indicator"]:
        parts.append(f"Wetland indicator: {info['wetland_indicator']}")
    if parts:
        info["facts"] = ". ".join(parts) + "."

    return info


MOBOT_URL = "https://www.missouribotanicalgarden.org/PlantFinder/PlantFinderDetails.aspx?taxonid={taxon_id}"
MOBOT_SEARCH = "https://www.missouribotanicalgarden.org/PlantFinder/PlantFinderSearch.aspx"


def scrape_mobot(genus: str, species_epithet: str) -> dict:
    """Query Missouri Botanical Garden Plant Finder for plant descriptions."""
    info = {"facts": "", "habitat": "", "family": "", "source": "Missouri Botanical Garden"}
    sci_name = f"{genus} {species_epithet}"
    try:
        resp = requests.get(
            MOBOT_SEARCH,
            params={"SearchText": sci_name},
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            return info
        html = resp.text
    except Exception:
        return info

    tid_match = re.search(r'taxonid=(\d+)', html, re.IGNORECASE)
    if not tid_match:
        return info
    taxon_id = tid_match.group(1)

    try:
        resp = requests.get(
            MOBOT_URL.format(taxon_id=taxon_id),
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            return info
        html = resp.text
    except Exception:
        return info

    m = re.search(
        r'(?:Plant\s*Basics|General\s*Description|Noteworthy\s*Characteristics).*?<(?:p|div|span)[^>]*>(.*?)</(?:p|div|span)>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m:
        text = strip_tags(m.group(1))
        if len(text) > 30:
            info["facts"] = text[:600]

    m = re.search(r'Culture.*?<(?:p|div|span)[^>]*>(.*?)</(?:p|div|span)>', html, re.DOTALL | re.IGNORECASE)
    if m:
        info["habitat"] = strip_tags(m.group(1))[:300]

    m = re.search(r'Family.*?<(?:a|span|td|dd)[^>]*>(.*?)</', html, re.DOTALL | re.IGNORECASE)
    if m:
        info["family"] = strip_tags(m.group(1))[:80]

    return info


UF_TREES_SERVLET = "https://floridatrees.ifas.ufl.edu/TREESServlet"


def _load_uf_trees_index(cache_dir: Path) -> dict[str, dict]:
    """Load or return empty UF/IFAS Florida Trees classoid index."""
    idx_path = cache_dir / ".uf_trees_index.json"
    if not idx_path.exists():
        return {}
    return json.loads(idx_path.read_text())


def scrape_uf_trees(genus: str, species_epithet: str,
                    uf_index: dict[str, dict] | None = None) -> dict:
    """Fetch tree/shrub data from UF/IFAS Florida Trees (floridatrees.ifas.ufl.edu).

    Primary reference for Florida / southeast US trees and shrubs; see
    https://floridatrees.ifas.ufl.edu/FloridaTrees/ . Returns keys: facts, family,
    habitat, growth_habit, source.
    """
    info: dict[str, str] = {"facts": "", "family": "", "source": "UF/IFAS Florida Trees"}
    sci_key = f"{genus.lower()} {species_epithet.lower()}"
    if uf_index is None or sci_key not in uf_index:
        return info
    classoid = uf_index[sci_key]["classoid"]
    try:
        resp = requests.get(UF_TREES_SERVLET,
                            params={"command": "getTreeXML", "classoid": str(classoid)},
                            headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return info
        root = ET.fromstring(resp.text)
    except Exception:
        return info

    info["family"] = root.findtext(".//family", "")

    parts: list[str] = []
    dt = root.findtext("descriptivetext", "")
    if dt:
        text = re.sub(r'<[^>]+>', ' ', dt).strip()
        text = re.sub(r'\s+', ' ', text).strip()
        common = root.findtext(".//commonname", "")
        text = re.sub(
            rf'^{re.escape(genus)}\s+{re.escape(species_epithet)}\s+'
            rf'{re.escape(common)}\s*', '', text,
        ).strip()
        if text:
            parts.append(text)

    lifespan = root.findtext("lifespan", "")
    height = root.findtext(".//matureheight", "")
    spread = root.findtext(".//maturespread", "")
    growth = root.findtext(".//growthrate", "")
    if height:
        parts.append(f"Reaches {height} tall, {spread} spread.")
    if growth and lifespan:
        parts.append(f"{growth.capitalize()} growth rate; lifespan {lifespan}.")

    light = root.findtext(".//lightrequirements", "")
    drought = root.findtext(".//droughttolerance", "")
    soil_txt = root.findtext(".//soiltexturetolerance", "")
    salt = root.findtext(".//saltspraytolerance", "")
    habitat_bits = []
    if light:
        habitat_bits.append(light)
    if drought:
        habitat_bits.append(f"drought tolerance: {drought}")
    if soil_txt:
        habitat_bits.append(f"tolerates {soil_txt} soils")
    if salt and salt != "unknown":
        habitat_bits.append(f"salt spray tolerance: {salt}")
    if habitat_bits:
        info["habitat"] = "; ".join(habitat_bits)

    native_states = root.findtext(".//nativestate", "")
    if native_states:
        parts.append(f"Native to {native_states}.")

    info["facts"] = " ".join(parts)[:1200]

    ptype = root.findtext("planttype", "")
    if ptype:
        info["growth_habit"] = ptype

    native_fl = root.findtext(".//nativefloridacounty", "")
    if native_fl:
        info["native_counties"] = native_fl

    return info


FLORIDA_ATLAS_URL = "https://florida.plantatlas.usf.edu/plant/species/{species_id}"


def _load_atlas_index(cache_dir: Path) -> dict[str, int]:
    """Load or return empty Florida Plant Atlas name->ID reverse index."""
    idx_path = cache_dir / ".atlas_index.json"
    if not idx_path.exists():
        return {}
    raw = json.loads(idx_path.read_text())
    name_to_id: dict[str, int] = {}
    for sid, name in raw.items():
        if name:
            parts = name.split()
            if len(parts) >= 2:
                key = f"{parts[0].lower()} {parts[1].lower()}"
                name_to_id[key] = int(sid)
    return name_to_id


def scrape_florida_atlas(genus: str, species_epithet: str,
                         atlas_index: dict[str, int] | None = None) -> dict:
    """Scrape species data from the Atlas of Florida Plants (USF/ISB).

    Returns dict with keys: facts, family, status, growth_habit, source.
    Requires an atlas_index mapping 'genus species' -> atlas_id.
    """
    info: dict[str, str] = {"facts": "", "family": "", "source": "Atlas of Florida Plants"}
    sci_key = f"{genus.lower()} {species_epithet.lower()}"
    if atlas_index is None or sci_key not in atlas_index:
        return info
    species_id = atlas_index[sci_key]
    try:
        resp = requests.get(
            FLORIDA_ATLAS_URL.format(species_id=species_id),
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            return info
    except Exception:
        return info
    html = resp.text

    def _extract(label: str) -> str:
        pat = rf'<label[^>]*>{label}</label>\s*<div class="form-control-plaintext">(.*?)</div>'
        m = re.search(pat, html, re.DOTALL)
        return re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ""

    info["family"] = _extract("Family")
    common = _extract("Common Name")
    if common:
        info["common_name"] = common

    gh = _extract("Growth Habit")
    if gh and gh != "**" and "Not applicable" not in gh:
        info["growth_habit"] = gh

    # Status badges
    status_m = re.search(
        r'<label[^>]*>Status</label>\s*<div class="form-control-plaintext">(.*?)</div>',
        html, re.DOTALL,
    )
    if status_m:
        badges = re.findall(r'<span[^>]*>([^<]+)</span>', status_m.group(1))
        status_str = " ".join(b.strip() for b in badges if b.strip())
        if status_str:
            info["status"] = status_str

    # Plant Notes (expert-written descriptions)
    notes_m = re.search(
        r'<label[^>]*>Plant Notes</label>\s*<div class="form-control-plaintext">(.*?)</div>',
        html, re.DOTALL,
    )
    if notes_m:
        notes = re.sub(r'<[^>]+>', '', notes_m.group(1)).strip()
        notes = re.sub(r'\s+', ' ', notes).strip()
        if notes and "Not applicable" not in notes and len(notes) > 10:
            info["facts"] = notes[:800]

    info["atlas_id"] = str(species_id)
    return info


def scrape_wikipedia(sci_name: str, sentences: int = 5,
                     max_chars: int = 800) -> dict:
    """Fetch a plain-text intro extract from Wikipedia (species, then genus).

    Set sentences=0 to fetch the full intro without a sentence limit.
    """
    info = {"facts": "", "source": "Wikipedia"}

    for title in [sci_name, sci_name.split()[0] if " " in sci_name else None]:
        if not title:
            continue
        params = {
            "action": "query", "titles": title, "prop": "extracts",
            "exintro": "true", "explaintext": "true",
            "redirects": "1", "format": "json",
        }
        sen = sentences if title == sci_name else 3
        if sen > 0:
            params["exsentences"] = str(sen)
        try:
            resp = requests.get(
                "https://en.wikipedia.org/w/api.php", params=params,
                headers={"User-Agent": "GulfIslandsChecklist/1.0"},
                timeout=12,
            )
            if resp.status_code != 200:
                continue
            pages = resp.json().get("query", {}).get("pages", {})
            for pid, page in pages.items():
                if pid != "-1":
                    text = page.get("extract", "").strip()
                    if len(text) > 30:
                        info["facts"] = text[:max_chars]
                        return info
        except Exception:
            continue
    return info


WORMS_API = "https://www.marinespecies.org/rest"


def fetch_worms_record(sci_name: str) -> dict:
    """Look up a marine species in WoRMS and return taxonomy + attributes."""
    info: dict = {}
    try:
        r = requests.get(
            f"{WORMS_API}/AphiaRecordsByMatchNames",
            params={"scientificnames[]": sci_name, "marine_only": "false"},
            headers=HEADERS, timeout=15,
        )
        if r.status_code != 200:
            return info
        matches = r.json()
        if not matches or not matches[0]:
            return info
        rec = matches[0][0]
        aphia_id = rec.get("AphiaID")
        if not aphia_id:
            return info

        for field in ("phylum", "class", "order", "family"):
            if rec.get(field):
                info[field] = rec[field]
        if rec.get("isMarine"):
            info["marine"] = True
        if rec.get("isBrackish"):
            info["brackish"] = True

        attrs = requests.get(
            f"{WORMS_API}/AphiaAttributesByAphiaID/{aphia_id}",
            headers=HEADERS, timeout=15,
        )
        if attrs.status_code == 200:
            for attr in attrs.json():
                mtype = attr.get("measurementType", "")
                mval = attr.get("measurementValue", "")
                if mtype == "Body size" and mval:
                    unit = ""
                    btype = ""
                    for child in attr.get("children", []):
                        if child.get("measurementType") == "Unit":
                            unit = child.get("measurementValue", "")
                        if child.get("measurementType") == "Type":
                            btype = child.get("measurementValue", "")
                    size_str = f"{mval} {unit}".strip()
                    if btype:
                        size_str += f" ({btype})"
                    info.setdefault("body_sizes", []).append(size_str)
                elif "Functional group" in mtype and mval:
                    info.setdefault("functional_groups", []).append(mval)
                elif "habitat" in mtype.lower() and mval:
                    info.setdefault("habitats", []).append(mval)
                elif "depth" in mtype.lower() and mval:
                    info["depth"] = mval

        dists = requests.get(
            f"{WORMS_API}/AphiaDistributionsByAphiaID/{aphia_id}",
            headers=HEADERS, timeout=15,
        )
        if dists.status_code == 200:
            regions = []
            for d in dists.json():
                loc = d.get("locality", "")
                if loc and loc not in regions:
                    regions.append(loc)
            if regions:
                info["distribution"] = regions[:6]
    except Exception as exc:
        log.debug("WoRMS lookup failed for %s: %s", sci_name, exc)
    return info


# ── OBIS API (Ocean Biodiversity Information System) ──────────────────

OBIS_API = "https://api.obis.org/v3"

OBIS_SEAWEED_TAXA = [
    "Rhodophyta",     # red algae
    "Phaeophyceae",   # brown algae (class within Ochrophyta)
    "Chlorophyta",    # green algae
]

MACROALGAE_CLASSES = {
    "Florideophyceae", "Bangiophyceae", "Compsopogonophyceae",
    "Stylonematophyceae",
    "Phaeophyceae",
    "Ulvophyceae",
}


IDIGBIO_API = "https://search.idigbio.org/v2"


def idigbio_enrich_plants(species_list: list[dict], lat: float, lng: float,
                          cache: dict, cache_path: Path) -> int:
    """Supplement plant descriptions with habitat data from iDigBio specimen records."""
    bbox = {
        "type": "geo_bounding_box",
        "top_left": {"lat": lat + 0.7, "lon": lng - 2.0},
        "bottom_right": {"lat": lat - 0.7, "lon": lng + 2.0},
    }
    enriched = 0
    for entry in species_list:
        sci = entry.get("scientific_name", "")
        if not sci or " " not in sci:
            continue
        parts = sci.split()
        cache_key = f"idigbio_{parts[0]}_{parts[1]}"
        if cache_key in cache:
            habitat_note = cache[cache_key]
        else:
            habitat_note = ""
            try:
                resp = requests.post(
                    f"{IDIGBIO_API}/search/records/",
                    json={
                        "rq": {
                            "scientificname": sci,
                            "geopoint": bbox,
                            "kingdom": "Plantae",
                        },
                        "limit": 5,
                        "fields": ["data.dwc:habitat", "data.dwc:occurrenceRemarks",
                                   "data.dwc:locality"],
                    },
                    headers=HEADERS, timeout=20,
                )
                items = resp.json().get("items", [])
                snippets = []
                for item in items:
                    d = item.get("data", {})
                    for fld in ("dwc:habitat", "dwc:occurrenceRemarks"):
                        v = d.get(fld, "")
                        if v and len(v) > 15 and v not in snippets:
                            snippets.append(v)
                habitat_note = " | ".join(snippets[:3])
            except Exception as e:
                log.warning("  iDigBio error for %s: %s", sci, e)
            cache[cache_key] = habitat_note
            save_json(cache_path, cache)
            time.sleep(0.5)

        if habitat_note and not entry.get("idigbio_habitat"):
            entry["idigbio_habitat"] = habitat_note
            existing = entry.get("facts", "")
            if existing and habitat_note not in existing:
                entry["facts"] = existing.rstrip(". ") + ". Habitat: " + habitat_note
            elif not existing:
                entry["facts"] = "Habitat: " + habitat_note
            enriched += 1
    return enriched


def fetch_obis_seaweed(lat: float, lng: float, radius_deg: float = 1.0,
                       max_species: int = 30, cache: dict | None = None,
                       cache_path=None) -> list[dict]:
    """Fetch macroalgae/seaweed species from OBIS for the region.

    Returns a list of dicts with keys matching the sea-life pipeline shape:
    common_name, scientific_name, group, taxon_id (AphiaID), inat_count, etc.
    """
    cache = cache if cache is not None else {}
    cache_key = f"obis_seaweed_{lat:.2f}_{lng:.2f}_{radius_deg}"
    if cache_key in cache:
        log.info("  OBIS seaweed: using cached results (%d species)", len(cache[cache_key]))
        return cache[cache_key]

    bbox_w = lng - radius_deg
    bbox_e = lng + radius_deg
    bbox_s = lat - radius_deg
    bbox_n = lat + radius_deg
    wkt = (f"POLYGON(({bbox_w} {bbox_s},{bbox_e} {bbox_s},"
           f"{bbox_e} {bbox_n},{bbox_w} {bbox_n},{bbox_w} {bbox_s}))")

    all_results = []
    for taxon_name in OBIS_SEAWEED_TAXA:
        url = f"{OBIS_API}/checklist"
        params = {
            "scientificname": taxon_name,
            "geometry": wkt,
            "size": 500,
        }
        try:
            log.info("  OBIS query: %s in bounding box...", taxon_name)
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                log.warning("  OBIS returned %d for %s", resp.status_code, taxon_name)
                continue
            data = resp.json()
            results = data.get("results", [])
            log.info("    %d taxa returned for %s", len(results), taxon_name)
            all_results.extend(results)
        except Exception as e:
            log.warning("  OBIS request failed for %s: %s", taxon_name, e)
        time.sleep(0.3)

    seen: set[str] = set()
    species_entries = []
    for rec in all_results:
        rank = rec.get("taxonRank", "")
        if rank != "Species":
            continue
        sci = rec.get("scientificName", "")
        if not sci or sci in seen:
            continue
        algae_class = rec.get("class", "")
        if not algae_class or algae_class not in MACROALGAE_CLASSES:
            continue
        seen.add(sci)
        genus = sci.split()[0] if " " in sci else sci
        species_entries.append({
            "scientific_name": sci,
            "common_name": genus,
            "group": "Seaweed & Algae",
            "taxon_id": rec.get("taxonID"),
            "aphia_id": rec.get("taxonID"),
            "obis_records": rec.get("records", 0),
            "inat_count": 0,
            "phylum": rec.get("phylum", ""),
            "class": rec.get("class", ""),
            "order": rec.get("order", ""),
            "family": rec.get("family", ""),
            "data_source": "OBIS",
        })

    species_entries.sort(key=lambda e: -e["obis_records"])
    species_entries = species_entries[:max_species]

    log.info("  OBIS seaweed: %d macroalgae species (capped at %d)",
             len(species_entries), max_species)

    cache[cache_key] = species_entries
    if cache_path:
        save_json(cache_path, cache)

    return species_entries


NOAA_COOPS_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

# Nearest tide stations by region
TIDE_STATIONS = {
    "grayton_beach": {"id": "8729511", "name": "Destin East Pass (nearest to Grayton Beach)"},
    "panama_city": {"id": "8729210", "name": "Panama City Beach, FL"},
    "pensacola": {"id": "8729840", "name": "Pensacola, FL"},
}

# Apalachicola NERR bounding box (USGS OF 2006-1381)
ANERR_BBOX = "29.586522,-85.385000,29.867725,-84.572274"


def fetch_noaa_tides(station_id: str, begin_date: str, end_date: str) -> list[dict]:
    """Fetch hi/lo tide predictions from NOAA CO-OPS API.

    Returns list of dicts with keys: t (datetime string), v (height ft), type (H/L).
    """
    try:
        r = requests.get(
            NOAA_COOPS_API,
            params={
                "station": station_id,
                "product": "predictions",
                "datum": "MLLW",
                "begin_date": begin_date,
                "end_date": end_date,
                "interval": "hilo",
                "time_zone": "lst_ldt",
                "units": "english",
                "format": "json",
                "application": "FieldChecklist",
            },
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("predictions", [])
    except Exception as exc:
        log.warning("NOAA tide fetch failed: %s", exc)
        return []


def format_tide_html(predictions: list[dict], station_name: str) -> str:
    """Build an inline SVG tide trendline with smooth cubic curves."""
    if not predictions:
        return ""

    from datetime import datetime

    points: list[tuple[float, float]] = []
    labels: list[dict] = []
    t0 = None
    for p in predictions:
        dt_str = p.get("t", "")
        height = float(p.get("v", 0))
        tide_type = p.get("type", "")
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if t0 is None:
            t0 = dt
        hours = (dt - t0).total_seconds() / 3600
        points.append((hours, height))
        labels.append({
            "h": hours, "v": height, "type": tide_type,
            "date": dt.strftime("%b %d"), "time": dt.strftime("%-I:%M%p").lower(),
        })

    if len(points) < 2:
        return ""

    max_h = max(h for h, _ in points)
    min_v = min(v for _, v in points)
    max_v = max(v for _, v in points)
    v_range = max_v - min_v or 1.0

    W, H = 900, 200
    PAD_X, PAD_TOP, PAD_BOT = 12, 18, 30

    def sx(h: float) -> float:
        return PAD_X + (h / max_h) * (W - 2 * PAD_X)

    def sy(v: float) -> float:
        return PAD_TOP + (1 - (v - min_v) / v_range) * (H - PAD_TOP - PAD_BOT)

    px = [sx(h) for h, _ in points]
    py = [sy(v) for _, v in points]
    n = len(px)

    segs = [f"M{px[0]:.1f},{py[0]:.1f}"]
    for i in range(1, n):
        t = 0.35
        if i == 1:
            cp1x = px[0] + t * (px[1] - px[0])
            cp1y = py[0] + t * (py[1] - py[0])
        else:
            cp1x = px[i - 1] + t * (px[i] - px[i - 2])
            cp1y = py[i - 1] + t * (py[i] - py[i - 2])
        if i == n - 1:
            cp2x = px[i] - t * (px[i] - px[i - 1])
            cp2y = py[i] - t * (py[i] - py[i - 1])
        else:
            cp2x = px[i] - t * (px[i + 1] - px[i - 1])
            cp2y = py[i] - t * (py[i + 1] - py[i - 1])
        segs.append(f"C{cp1x:.1f},{cp1y:.1f} {cp2x:.1f},{cp2y:.1f} {px[i]:.1f},{py[i]:.1f}")
    path_d = " ".join(segs)

    fill_d = path_d + f" L{px[-1]:.1f},{H - PAD_BOT:.1f} L{px[0]:.1f},{H - PAD_BOT:.1f} Z"

    day_marks = []
    seen_dates: set[str] = set()
    for lb in labels:
        if lb["date"] not in seen_dates:
            seen_dates.add(lb["date"])
            x = sx(lb["h"])
            day_marks.append(
                f'<line x1="{x:.1f}" y1="{PAD_TOP}" x2="{x:.1f}" y2="{H - PAD_BOT}" '
                f'stroke="#e0e0e0" stroke-width="0.5" stroke-dasharray="2,2"/>'
                f'<text x="{x:.1f}" y="{H - 2}" text-anchor="middle" '
                f'font-size="11" fill="#999">{lb["date"]}</text>'
            )

    dots = []
    for lb in labels:
        x, y = sx(lb["h"]), sy(lb["v"])
        color = "#2E6B94" if lb["type"] == "H" else "#8B7348"
        tip = f'{lb["time"]} {lb["v"]:.1f}ft'
        ty = y - 9 if lb["type"] == "H" else y + 14
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>'
            f'<text x="{x:.1f}" y="{ty:.1f}" text-anchor="middle" '
            f'font-size="10" fill="{color}">{tip}</text>'
        )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'style="width:100%;max-width:{W}px;height:auto;display:block;min-height:150px">'
        f'{"".join(day_marks)}'
        f'<path d="{fill_d}" fill="rgba(90,158,192,0.08)" stroke="none"/>'
        f'<path d="{path_d}" fill="none" stroke="#5a9ec0" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'{"".join(dots)}'
        f'</svg>'
    )

    return (
        f'<div class="tide-table" style="margin:10px 0;max-width:940px">'
        f'<div style="font-size:14px;font-weight:600;margin-bottom:4px;color:#555">'
        f'Tides &middot; {esc(station_name)}</div>'
        f'{svg}'
        f'<div style="font-size:8px;color:#bbb;margin-top:2px">'
        f'NOAA CO-OPS &middot; MLLW datum</div></div>'
    )


USNO_MOON_API = "https://aa.usno.navy.mil/api/moon/phases/year"

PHASE_FRACTION = {
    "New Moon": 0.0,
    "First Quarter": 0.25,
    "Full Moon": 0.5,
    "Last Quarter": 0.75,
}


def _fetch_usno_phases(year: int) -> list[dict]:
    """Fetch primary moon phases for *year* from USNO API."""
    try:
        r = requests.get(USNO_MOON_API, params={"year": year},
                         headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.json().get("phasedata", [])
    except Exception as exc:
        log.warning("USNO moon API failed for %d: %s", year, exc)
        return []


def compute_moon_phases(start_date: str, end_date: str) -> str:
    """Fetch actual USNO moon-phase dates, interpolate daily illumination,
    and return inline SVG icons for each day in the range."""
    from datetime import datetime, timedelta, date
    import math

    try:
        d0 = datetime.strptime(start_date, "%Y%m%d").date()
        d1 = datetime.strptime(end_date, "%Y%m%d").date()
    except ValueError:
        return ""

    years = set(range(d0.year, d1.year + 1))
    raw_phases: list[tuple[date, float]] = []
    for yr in sorted(years):
        for pd in _fetch_usno_phases(yr):
            try:
                pdate = date(pd["year"], pd["month"], pd["day"])
                frac = PHASE_FRACTION.get(pd["phase"])
                if frac is not None:
                    raw_phases.append((pdate, frac))
            except (KeyError, ValueError):
                continue

    raw_phases.sort(key=lambda x: x[0])

    if len(raw_phases) < 2:
        log.warning("Insufficient USNO phase data, falling back")
        return ""

    phase_labels: dict[date, str] = {}
    for pd in raw_phases:
        for name, frac in PHASE_FRACTION.items():
            if frac == pd[1]:
                phase_labels[pd[0]] = name
                break

    def _interp_phase(d: date) -> float:
        """Interpolate lunation fraction (0=new, 0.5=full) for date *d*."""
        for i in range(len(raw_phases) - 1):
            da, fa = raw_phases[i]
            db, fb = raw_phases[i + 1]
            if da <= d <= db:
                span = (db - da).days or 1
                t = (d - da).days / span
                diff = fb - fa
                if diff < -0.5:
                    diff += 1.0
                elif diff > 0.5:
                    diff -= 1.0
                val = fa + t * diff
                return val % 1.0
        if d <= raw_phases[0][0]:
            return raw_phases[0][1]
        return raw_phases[-1][1]

    def _moon_svg(phase_frac: float, r: int = 12, cx: int = 14, cy: int = 14) -> str:
        """Render moon disc by drawing the lit area on a dark base.

        Northern-hemisphere convention:
          waxing (0<frac<0.5): right side lit
          waning (0.5<frac<1): left side lit

        The lit shape is bounded by two arcs from top to bottom:
          1. A semicircular arc (rx=r) on the lit side
          2. An elliptical terminator arc (rx=dx) whose sweep depends
             on whether we are in crescent (<50% lit) or gibbous (>50%)
        """
        illum = 0.5 * (1 - math.cos(2 * math.pi * phase_frac))
        if illum < 0.005:
            return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#222" stroke="#555" stroke-width="0.5"/>'
        if illum > 0.995:
            return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#F5E6B8" stroke="#DAC68D" stroke-width="0.5"/>'

        waxing = phase_frac <= 0.5
        dx = r * abs(2 * illum - 1)

        top = f"{cx},{cy - r}"
        bot = f"{cx},{cy + r}"

        if waxing:
            semi = f"A{r},{r} 0 0,1 {bot}"
            if illum <= 0.5:
                term = f"A{dx:.1f},{r} 0 0,0 {top}"
            else:
                term = f"A{dx:.1f},{r} 0 0,1 {top}"
        else:
            semi = f"A{r},{r} 0 0,0 {bot}"
            if illum <= 0.5:
                term = f"A{dx:.1f},{r} 0 0,1 {top}"
            else:
                term = f"A{dx:.1f},{r} 0 0,0 {top}"

        return (
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#222" stroke="#555" stroke-width="0.5"/>'
            f'<path d="M{top} {semi} {term}" fill="#F5E6B8"/>'
        )

    icons = []
    d = d0
    while d <= d1:
        p = _interp_phase(d)
        lbl = d.strftime("%b %-d")
        phase_name = phase_labels.get(d, "")
        name_html = (f'<div style="font-size:7px;color:#666;font-weight:600;'
                     f'margin-top:0">{phase_name}</div>' if phase_name else "")
        icons.append(
            f'<div style="text-align:center;flex-shrink:0;width:34px">'
            f'<svg width="28" height="28" viewBox="0 0 28 28">{_moon_svg(p)}</svg>'
            f'<div style="font-size:8px;color:#999;margin-top:1px">{lbl}</div>'
            f'{name_html}</div>'
        )
        d += timedelta(days=1)

    if not icons:
        return ""

    log.info("  USNO moon phases: %d days rendered, %d primary phases in range",
             len(icons), sum(1 for d2 in phase_labels if d0 <= d2 <= d1))

    return (
        '<div style="margin:10px 0;max-width:960px">'
        '<div style="font-size:14px;font-weight:600;margin-bottom:6px;color:#555">'
        'Moon Phases <span style="font-size:10px;font-weight:400;color:#aaa">'
        '(U.S. Naval Observatory)</span></div>'
        '<div style="display:flex;gap:4px;flex-wrap:wrap">'
        + "".join(icons)
        + '</div></div>'
    )


BEAUFORT_SCALE = [
    (1, "Calm"),       (3, "Light"),     (7, "Light"),
    (12, "Gentle"),    (18, "Moderate"), (24, "Fresh"),
    (31, "Strong"),    (38, "Near Gale"), (46, "Gale"),
    (54, "Strong Gale"), (63, "Storm"),  (72, "Violent Storm"),
    (999, "Hurricane"),
]

WMO_WEATHER = {
    0: ("Clear", "\u2600\ufe0f"),
    1: ("Mostly Clear", "\U0001f324\ufe0f"),
    2: ("Partly Cloudy", "\u26c5"),
    3: ("Overcast", "\u2601\ufe0f"),
    45: ("Fog", "\U0001f32b\ufe0f"),
    48: ("Rime Fog", "\U0001f32b\ufe0f"),
    51: ("Light Drizzle", "\U0001f326\ufe0f"),
    53: ("Drizzle", "\U0001f326\ufe0f"),
    55: ("Heavy Drizzle", "\U0001f326\ufe0f"),
    61: ("Light Rain", "\U0001f327\ufe0f"),
    63: ("Rain", "\U0001f327\ufe0f"),
    65: ("Heavy Rain", "\U0001f327\ufe0f"),
    80: ("Rain Showers", "\U0001f326\ufe0f"),
    81: ("Mod Rain Showers", "\U0001f327\ufe0f"),
    82: ("Violent Showers", "\u26c8\ufe0f"),
    95: ("Thunderstorm", "\u26c8\ufe0f"),
    96: ("T-Storm w/ Hail", "\u26c8\ufe0f"),
    99: ("T-Storm w/ Hail", "\u26c8\ufe0f"),
}


def beaufort_label(wind_kmh: float) -> str:
    for limit, label in BEAUFORT_SCALE:
        if wind_kmh < limit:
            return label
    return "Hurricane"


def compute_weather_forecast(lat: float, lng: float,
                             start_date: str, end_date: str) -> str:
    """Fetch daily weather + marine seas from Open-Meteo and return HTML widget."""
    from datetime import datetime as _dt
    d0 = _dt.strptime(start_date, "%Y%m%d").date()
    d1 = _dt.strptime(end_date, "%Y%m%d").date()

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lng}"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        f"wind_speed_10m_max,sunrise,sunset"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
        f"&timezone=America%2FChicago"
        f"&forecast_days=16"
    )

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json().get("daily", {})
    except Exception as e:
        log.warning("Weather fetch failed: %s", e)
        return ""

    all_dates = data.get("time", [])
    if not all_dates:
        return ""

    marine_waves: dict[str, float] = {}
    try:
        marine_url = (
            f"https://marine-api.open-meteo.com/v1/marine?"
            f"latitude={lat}&longitude={lng}"
            f"&daily=wave_height_max"
            f"&timezone=America%2FChicago"
            f"&forecast_days=16"
        )
        mr = requests.get(marine_url, headers=HEADERS, timeout=15)
        mr.raise_for_status()
        md = mr.json().get("daily", {})
        m_dates = md.get("time", [])
        m_waves = md.get("wave_height_max", [])
        last_valid_wave = None
        for k, dt_str in enumerate(m_dates):
            if k < len(m_waves) and m_waves[k] is not None:
                marine_waves[dt_str] = m_waves[k]
                last_valid_wave = m_waves[k]
        if last_valid_wave is not None:
            for dt_str in m_dates:
                if dt_str not in marine_waves:
                    marine_waves[dt_str] = last_valid_wave
        log.info("  Marine forecast: %d days of wave data (last valid: %s m)",
                 len(marine_waves), last_valid_wave)
    except Exception as e:
        log.warning("Marine forecast fetch failed: %s", e)

    dates = []
    indices = []
    for i, ds in enumerate(all_dates):
        dd = _dt.strptime(ds, "%Y-%m-%d").date()
        if d0 <= dd <= d1:
            dates.append(ds)
            indices.append(i)

    partial_note = ""
    if not dates and all_dates:
        n = min(len(all_dates), (d1 - d0).days + 1, 8)
        dates = all_dates[-n:]
        indices = list(range(len(all_dates) - n, len(all_dates)))
        partial_note = (
            '<div style="font-size:10px;color:#999;margin-top:4px">'
            f'Extended forecast not yet available for {d0.strftime("%b %-d")}\u2013'
            f'{d1.strftime("%b %-d")}; showing nearest available days.</div>'
        )

    if not dates:
        return ""

    codes = data.get("weather_code", [])
    t_max = data.get("temperature_2m_max", [])
    t_min = data.get("temperature_2m_min", [])
    wind_max = data.get("wind_speed_10m_max", [])
    sunrise = data.get("sunrise", [])
    sunset = data.get("sunset", [])

    def _fmt12(iso_str):
        hm = iso_str.split("T")[1][:5]
        h, m = int(hm[:2]), hm[3:]
        suffix = "am" if h < 12 else "pm"
        h = h % 12 or 12
        return f"{h}:{m}{suffix}"

    cards = []
    for j, d in enumerate(dates):
        i = indices[j]
        dd = _dt.strptime(d, "%Y-%m-%d").date()
        day_label = dd.strftime("%a %b %-d")
        code = codes[i] if i < len(codes) else 0
        desc, icon = WMO_WEATHER.get(code, WMO_WEATHER.get(0))
        hi = f"{t_max[i]:.0f}" if i < len(t_max) and t_max[i] is not None else "\u2014"
        lo = f"{t_min[i]:.0f}" if i < len(t_min) and t_min[i] is not None else "\u2014"
        w = wind_max[i] if i < len(wind_max) and wind_max[i] is not None else 0
        sr = _fmt12(sunrise[i]) if i < len(sunrise) and sunrise[i] else "\u2014"
        ss = _fmt12(sunset[i]) if i < len(sunset) and sunset[i] else "\u2014"

        wave_m = marine_waves.get(d)
        seas_line = ""
        if wave_m is not None:
            wave_ft = wave_m * 3.281
            seas_line = f'<div class="wx-seas">\U0001f30a Seas {wave_ft:.1f} ft</div>'

        cards.append(
            f'<div class="wx-day">'
            f'<div class="wx-date">{day_label}</div>'
            f'<div class="wx-icon">{icon}</div>'
            f'<div class="wx-desc">{desc}</div>'
            f'<div class="wx-temp">{hi}\u00b0 / {lo}\u00b0F</div>'
            f'<div class="wx-wind">\U0001f4a8 {w:.0f} mph</div>'
            f'{seas_line}'
            f'<div class="wx-sun">\u2600 {sr} \u2013 {ss}</div>'
            f'</div>'
        )

    log.info("  Weather forecast: %d days fetched", len(cards))

    return (
        '<div class="wx-forecast">'
        '<div style="font-size:14px;font-weight:600;margin-bottom:6px;color:#555">'
        'Weather Forecast <span style="font-size:10px;font-weight:400;color:#aaa">'
        '(Open-Meteo)</span></div>'
        '<div class="wx-grid">'
        + "".join(cards)
        + '</div>'
        + partial_note
        + '</div>'
    )


PLANT_REFERENCE_ATTRIBUTION = (
    "Go Botany / Native Plant Trust, USDA PLANTS Database & "
    "National Wetland Plant List, Missouri Botanical Garden Plant Finder, "
    "Wikipedia, Flora Novae Angliae (Haines), Dirr\u2019s Manual of Woody "
    "Landscape Plants, Florida Natural Heritage Program, NatureServe"
)


# ── Bird pipeline ──────────────────────────────────────────────────────

_CONSERVATION_ELEVATED_KW = {"orange", "tipping", "yellow", "watch", "decline",
                             "endangered", "threatened", "vulnerable", "critical"}


def is_conservation_elevated(text: str) -> bool:
    """Return True if conservation text indicates elevated concern."""
    if not text:
        return False
    t = text.lower()
    if "low concern" in t:
        return False
    return any(kw in t for kw in _CONSERVATION_ELEVATED_KW)


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

        audio_cache_key = f"audio_{slug}"
        if audio_cache_key in cache and cache[audio_cache_key].get("audio_ml_id"):
            sounds = cache[audio_cache_key]
        elif audio_cache_key in cache:
            sounds = cache[audio_cache_key]
        else:
            sounds = scrape_cornell_sounds(slug)
            cache[audio_cache_key] = sounds
            save_json(bird_cache_path, cache)
            time.sleep(0.4)

        info["audio_ml_id"] = sounds.get("audio_ml_id", "")
        info["sounds_url"] = sounds.get("sounds_url", "")

        group = assign_bird_group(info.get("order", ""), info.get("family", ""))
        if is_conservation_elevated(info.get("conservation", "")):
            group = "Conservation Concern"
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

    log.info("\nStep 4: Bird seasonality from eBird presence...")
    target_month = cfg["date"].month
    adj_prev = ((target_month - 2) % 12)
    adj_next = target_month % 12
    for bird in birds:
        seas = [1] * 12
        seas[target_month - 1] = 2
        seas[adj_prev] = 2
        seas[adj_next] = 2
        bird["seasonality"] = seas

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
    iconic = taxon_info.get("iconic_taxon_name", "")
    if BRYOPHYTE_ANCESTOR_ID in ancestor_ids:
        return "Lichens & Mosses"
    if iconic == "Fungi" or FUNGI_ANCESTOR_ID in ancestor_ids:
        if LICHEN_ANCESTOR_ID in ancestor_ids:
            return "Lichens & Mosses"
        return "Lichens & Mosses"
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
    if not family_clean:
        for aid in ancestor_ids:
            if aid in INAT_FAMILY_IDS:
                family_clean = INAT_FAMILY_IDS[aid]
                break
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

    log.info("  Querying iNaturalist for lichens & mosses...")
    lichen_data = inat_species_for_month(
        47170, cfg["lat"], cfg["lng"], cfg["radius"], months_str,
    )
    lichen_count = 0
    for sci, info in lichen_data.items():
        if sci not in inat_data:
            aids = set(info.get("ancestor_ids", []))
            if 54743 in aids or 311295 in aids:
                inat_data[sci] = info
                lichen_count += 1
    log.info("  Found %d lichens & mosses", lichen_count)

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

    uf_index = _load_uf_trees_index(cfg["output_dir"])
    if uf_index:
        log.info(
            "\nStep 1b: UF/IFAS Florida Trees (https://floridatrees.ifas.ufl.edu/FloridaTrees/) "
            "early fetch for indexed taxa (%d in local database)...",
            len(uf_index),
        )
        uf_hits = 0
        for i, entry in enumerate(species_list):
            sci = entry["scientific_name"]
            parts = sci.split()
            if len(parts) < 2:
                continue
            genus, sp_epithet = parts[0], parts[1]
            cache_key = f"uftree_{genus}_{sp_epithet}"

            if cache_key in cache and cache[cache_key].get("facts"):
                uf = cache[cache_key]
            else:
                uf = scrape_uf_trees(genus, sp_epithet, uf_index)
                if uf.get("facts"):
                    cache[cache_key] = uf
                    save_json(plant_cache_path, cache)
                    time.sleep(0.2)
                else:
                    continue

            if uf.get("facts"):
                entry["facts"] = uf["facts"]
                entry["habitat"] = uf.get("habitat", "")
                entry["family"] = uf.get("family", "")
                entry["desc_source"] = "UF/IFAS Florida Trees"
                if uf.get("growth_habit"):
                    entry.setdefault("gobotany", {})["growth_form"] = uf["growth_habit"].lower()
                uf_hits += 1
                if (i + 1) % 50 == 0:
                    log.info("  [%d/%d] UF Trees hits: %d", i + 1, len(species_list), uf_hits)
        log.info("  UF/IFAS Florida Trees: enriched %d species", uf_hits)
    else:
        log.info("\nStep 1b: Skipped UF/IFAS (no .uf_trees_index.json)")

    log.info("\nStep 2: Scraping Go Botany for remaining descriptions...")
    for i, entry in enumerate(species_list):
        sci = entry["scientific_name"]
        parts = sci.split()
        if len(parts) < 2:
            entry.setdefault("gobotany", {})
            continue
        if entry.get("facts"):
            entry.setdefault("gobotany", {})
            continue
        genus, sp_epithet = parts[0], parts[1]
        cache_key = f"{genus}_{sp_epithet}"
        log.info("  [%d/%d] %s (%s)", i + 1, len(species_list), entry["common_name"], sci)

        if cache_key in cache and cache[cache_key].get("facts") and cache[cache_key]["facts"] not in ("N/A", "n/a"):
            gb = cache[cache_key]
            log.info("    (cached)")
        else:
            gb = scrape_gobotany(genus, sp_epithet)
            if gb.get("facts") in ("N/A", "n/a"):
                gb["facts"] = ""
            cache[cache_key] = gb
            save_json(plant_cache_path, cache)
            time.sleep(0.4)

        entry["gobotany"] = gb
        if not entry.get("facts"):
            entry["facts"] = gb.get("facts", "")
        if not entry.get("habitat"):
            entry["habitat"] = gb.get("habitat", "")
        if not entry.get("family"):
            entry["family"] = gb.get("family", "")
        entry["conservation"] = ""
        if not entry.get("desc_source") and gb.get("facts"):
            entry["desc_source"] = "Go Botany / Native Plant Trust"

        if gb.get("facts"):
            log.info("    %s", gb["facts"][:70] + "...")
        else:
            log.info("    No Go Botany page")

    atlas_index = _load_atlas_index(cfg["output_dir"])
    if atlas_index:
        needs_atlas = [e for e in species_list if not e.get("facts")]
        log.info("\nStep 2-FL: Atlas of Florida Plants for %d species without Go Botany data...",
                 len(needs_atlas))
        atlas_hits = 0
        for i, entry in enumerate(needs_atlas):
            sci = entry["scientific_name"]
            parts = sci.split()
            if len(parts) < 2:
                continue
            genus, sp_epithet = parts[0], parts[1]
            cache_key = f"fatlas_{genus}_{sp_epithet}"
            if cache_key in cache and cache[cache_key].get("facts"):
                fa = cache[cache_key]
            else:
                fa = scrape_florida_atlas(genus, sp_epithet, atlas_index)
                cache[cache_key] = fa
                save_json(plant_cache_path, cache)
                if fa.get("facts"):
                    time.sleep(0.3)

            if fa.get("facts"):
                entry["facts"] = fa["facts"]
                entry["desc_source"] = "Atlas of Florida Plants"
                if fa.get("family"):
                    entry["family"] = fa["family"]
                if fa.get("growth_habit"):
                    entry["gobotany"]["growth_form"] = fa["growth_habit"].lower()
                atlas_hits += 1
                if (i + 1) % 50 == 0 or i == len(needs_atlas) - 1:
                    log.info("  [%d/%d] Atlas hits: %d", i + 1, len(needs_atlas), atlas_hits)
            elif fa.get("family") and not entry.get("family"):
                entry["family"] = fa["family"]
                if fa.get("growth_habit"):
                    entry["gobotany"]["growth_form"] = fa["growth_habit"].lower()
        log.info("  Florida Atlas: enriched %d species with descriptions", atlas_hits)

        enriched_fam = 0
        for entry in species_list:
            if entry.get("family"):
                continue
            sci = entry["scientific_name"]
            parts = sci.split()
            if len(parts) < 2:
                continue
            cache_key = f"fatlas_{parts[0]}_{parts[1]}"
            fa = cache.get(cache_key, {})
            if fa.get("family"):
                entry["family"] = fa["family"]
                enriched_fam += 1
        if enriched_fam:
            log.info("  Florida Atlas: enriched %d species with family data", enriched_fam)
    else:
        log.info("\nStep 2-FL: Skipped (no .atlas_index.json found)")

    def _is_tree_candidate(entry):
        gb = entry.get("gobotany", {})
        gf = gb.get("growth_form", "")
        if "tree" in gf:
            return True
        family = gb.get("family", "")
        fam_clean = family.split("(")[0].strip().split(",")[0].strip()
        if fam_clean in TREE_FAMILIES:
            return True
        for aid in entry.get("ancestor_ids", []):
            if aid in INAT_FAMILY_IDS and INAT_FAMILY_IDS[aid] in TREE_FAMILIES:
                return True
        return False

    def _uf_trees_cached_for_entry(e: dict) -> bool:
        ps = e.get("scientific_name", "").split()
        if len(ps) < 2:
            return False
        ck = f"uftree_{ps[0]}_{ps[1]}"
        return bool(cache.get(ck, {}).get("facts"))

    tree_entries_need_wp = [
        e for e in species_list
        if _is_tree_candidate(e)
        and e.get("desc_source") != "UF/IFAS Florida Trees"
        and not _uf_trees_cached_for_entry(e)
    ]
    if tree_entries_need_wp:
        log.info("\nStep 2a: Wikipedia for %d tree species (skipping %d with UF/IFAS data)...",
                 len(tree_entries_need_wp),
                 sum(1 for e in species_list if _is_tree_candidate(e)) - len(tree_entries_need_wp))
        for i, entry in enumerate(tree_entries_need_wp):
            sci = entry["scientific_name"]
            cache_key = f"wiki_tree_{sci.replace(' ', '_')}"
            log.info("  [%d/%d] %s", i + 1, len(tree_entries_need_wp), entry["common_name"])

            if cache_key in cache and cache[cache_key].get("facts") and len(cache[cache_key]["facts"]) > 500:
                wp = cache[cache_key]
                log.info("    (cached, %d chars)", len(wp["facts"]))
            else:
                wp = scrape_wikipedia(sci, sentences=0, max_chars=2000)
                cache[cache_key] = wp
                save_json(plant_cache_path, cache)
                time.sleep(0.5)

            if wp.get("facts"):
                entry["facts"] = wp["facts"]
                entry["desc_source"] = "Wikipedia"
                log.info("    Wikipedia (%d chars): %s", len(wp["facts"]), wp["facts"][:70] + "...")
            else:
                log.info("    No Wikipedia data, keeping existing")

    needs_desc = [e for e in species_list if not e.get("facts")]
    if needs_desc:
        log.info("\nStep 2b: USDA PLANTS Database for %d species without Go Botany data...",
                 len(needs_desc))
        for i, entry in enumerate(needs_desc):
            sci = entry["scientific_name"]
            parts = sci.split()
            if len(parts) < 2:
                continue
            genus, sp_epithet = parts[0], parts[1]
            cache_key = f"usda_{genus}_{sp_epithet}"
            log.info("  [%d/%d] %s", i + 1, len(needs_desc), entry["common_name"])

            if cache_key in cache and cache[cache_key].get("facts"):
                usda = cache[cache_key]
                log.info("    (cached)")
            else:
                usda = scrape_usda_plants(genus, sp_epithet)
                cache[cache_key] = usda
                save_json(plant_cache_path, cache)
                time.sleep(0.4)

            if usda.get("facts"):
                entry["facts"] = usda["facts"]
                entry["desc_source"] = "USDA PLANTS Database"
                if not entry.get("habitat") and usda.get("habitat"):
                    entry["habitat"] = usda["habitat"]
                if not entry.get("family") and usda.get("family"):
                    entry["family"] = usda["family"]
                log.info("    USDA: %s", usda["facts"][:70] + "...")
            else:
                log.info("    No USDA data")

    still_needs_desc = [e for e in species_list if not e.get("facts")]
    if still_needs_desc:
        log.info("\nStep 2c: Missouri Botanical Garden for %d remaining species...",
                 len(still_needs_desc))
        for i, entry in enumerate(still_needs_desc):
            sci = entry["scientific_name"]
            parts = sci.split()
            if len(parts) < 2:
                continue
            genus, sp_epithet = parts[0], parts[1]
            cache_key = f"mobot_{genus}_{sp_epithet}"
            log.info("  [%d/%d] %s", i + 1, len(still_needs_desc), entry["common_name"])

            if cache_key in cache and cache[cache_key].get("facts"):
                mb = cache[cache_key]
                log.info("    (cached)")
            else:
                mb = scrape_mobot(genus, sp_epithet)
                cache[cache_key] = mb
                save_json(plant_cache_path, cache)
                time.sleep(0.4)

            if mb.get("facts"):
                entry["facts"] = mb["facts"]
                entry["desc_source"] = "Missouri Botanical Garden"
                if not entry.get("habitat") and mb.get("habitat"):
                    entry["habitat"] = mb["habitat"]
                if not entry.get("family") and mb.get("family"):
                    entry["family"] = mb["family"]
                log.info("    MoBotGarden: %s", mb["facts"][:70] + "...")
            else:
                log.info("    No MoBotGarden data")

    wiki_needs = [e for e in species_list if not e.get("facts")]
    if wiki_needs:
        log.info("\nStep 2d: Wikipedia for %d remaining species...",
                 len(wiki_needs))
        for i, entry in enumerate(wiki_needs):
            sci = entry["scientific_name"]
            cache_key = f"wiki_{sci.replace(' ', '_')}"
            log.info("  [%d/%d] %s", i + 1, len(wiki_needs), entry["common_name"])

            if cache_key in cache and cache[cache_key].get("facts"):
                wp = cache[cache_key]
                log.info("    (cached)")
            else:
                wp = scrape_wikipedia(sci, sentences=7, max_chars=1000)
                cache[cache_key] = wp
                save_json(plant_cache_path, cache)
                time.sleep(0.5)

            if wp.get("facts"):
                entry["facts"] = wp["facts"]
                entry["desc_source"] = "Wikipedia"
                log.info("    Wikipedia: %s", wp["facts"][:70] + "...")
            else:
                log.info("    No Wikipedia data")

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

    log.info("\nStep 2d: Fetching Florida/US conservation status from iNaturalist...")
    fl_statuses_fetched = 0
    for i, entry in enumerate(species_list):
        tid = entry.get("taxon_id")
        if not tid:
            continue
        cache_key = f"fl_conservation_{tid}"
        cached = cache.get(cache_key)
        if cached is not None:
            if cached:
                entry["conservation"] = cached
                fl_statuses_fetched += 1
            continue
        try:
            r = requests.get(f"{INAT_API}/taxa/{tid}",
                             headers=HEADERS, timeout=15)
            if r.status_code == 200:
                taxon_data = r.json().get("results", [{}])[0]
                statuses = taxon_data.get("conservation_statuses", [])
                fl_parts = []
                for cs in statuses:
                    place = cs.get("place", {})
                    place_name = place.get("display_name", "") if place else ""
                    is_florida = "Florida" in place_name
                    is_us = place_name == "United States"
                    is_global = not place  # global IUCN
                    if not (is_florida or is_us or is_global):
                        continue
                    status_name = cs.get("status_name", "")
                    status_code = cs.get("status", "").upper()
                    authority = cs.get("authority", "")
                    label = status_name or status_code
                    if not label:
                        continue
                    if is_florida:
                        fl_parts.insert(0, f"FL: {label}")
                    elif is_us:
                        fl_parts.append(f"US: {label}")
                    elif is_global and authority:
                        fl_parts.append(f"{authority}: {label}")
                    else:
                        fl_parts.append(label)
                fl_text = " · ".join(fl_parts) if fl_parts else ""
                cache[cache_key] = fl_text
                save_json(plant_cache_path, cache)
                if fl_text:
                    entry["conservation"] = fl_text
                    fl_statuses_fetched += 1
            time.sleep(0.3)
        except Exception:
            cache[cache_key] = ""
            save_json(plant_cache_path, cache)
    log.info("  Found Florida/US status for %d species", fl_statuses_fetched)

    for entry in species_list:
        inat_fam = inat_families.get(entry.get("taxon_id", 0), "")
        group = infer_plant_group(entry, entry.get("gobotany", {}), inat_fam)
        if is_conservation_elevated(entry.get("conservation", "")):
            group = "Conservation Concern"
        entry["group"] = group

    uf_index_core = _load_uf_trees_index(cfg["output_dir"])
    if uf_index_core:
        log.info(
            "\nStep 2e: UF/IFAS Florida Trees (https://floridatrees.ifas.ufl.edu/FloridaTrees/) "
            "as primary description for Trees & Shrubs where the database lists the species...",
        )
        uf_core_hits = 0
        for entry in species_list:
            if entry.get("group") not in ("Trees", "Shrubs"):
                continue
            sci = entry["scientific_name"]
            parts = sci.split()
            if len(parts) < 2:
                continue
            genus, sp_epithet = parts[0], parts[1]
            cache_key = f"uftree_{genus}_{sp_epithet}"
            if cache_key in cache and cache[cache_key].get("facts"):
                uf = cache[cache_key]
            else:
                uf = scrape_uf_trees(genus, sp_epithet, uf_index_core)
                if uf.get("facts"):
                    cache[cache_key] = uf
                    save_json(plant_cache_path, cache)
                    time.sleep(0.2)
            if not uf.get("facts"):
                continue
            entry["facts"] = uf["facts"]
            entry["desc_source"] = "UF/IFAS Florida Trees"
            if uf.get("habitat"):
                entry["habitat"] = uf["habitat"]
            if uf.get("family"):
                entry["family"] = uf["family"]
            if uf.get("growth_habit"):
                entry.setdefault("gobotany", {})["growth_form"] = uf["growth_habit"].lower()
            uf_core_hits += 1
        log.info("  Applied UF/IFAS Florida Trees as core text for %d trees & shrubs",
                 uf_core_hits)

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
            base = safe_filename(entry["common_name"])
            group_dir = out_dir / safe_filename(entry.get("group", ""))
            d1 = group_dir / f"{base}_1.jpg"
            d2 = group_dir / f"{base}_2.jpg"
            if not d1.exists() and out_dir.exists():
                for alt in out_dir.iterdir():
                    if alt.is_dir():
                        alt_d1 = alt / f"{base}_1.jpg"
                        if alt_d1.exists():
                            d1 = alt_d1
                            d2 = alt / f"{base}_2.jpg"
                            break
            entry["image_1"] = str(d1.relative_to(cfg["output_dir"])) if d1.exists() else ""
            entry["image_2"] = str(d2.relative_to(cfg["output_dir"])) if d2.exists() else ""

    log.info("\nStep 4: Enriching plant descriptions via iDigBio...")
    idigbio_enriched = idigbio_enrich_plants(
        species_list, cfg["lat"], cfg["lng"], cache, plant_cache_path,
    )
    log.info("  Enriched %d plants with iDigBio habitat data", idigbio_enriched)

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


def run_sea_life(cfg: dict) -> list[dict]:
    """Run the sea life pipeline. Returns enriched marine species records."""
    log.info("\n" + "=" * 60)
    log.info("Sea Life Pipeline — %s", cfg["place"])
    log.info("=" * 60)

    out_dir = cfg["output_dir"] / "images" / "SeaLife"
    seasonality_path = cfg["output_dir"] / ".seasonality.json"

    target_month = cfg["date"].month
    m_prev = ((target_month - 2) % 12) + 1
    m_next = (target_month % 12) + 1
    months_str = ",".join(str(m) for m in sorted({m_prev, target_month, m_next}))

    seasonality = load_json(seasonality_path)
    species_list = []

    seen_species: set[str] = set()
    for taxon_id, group_name in SEA_LIFE_TAXON_IDS.items():
        if taxon_id == 47178:
            log.info("\nSkipping iNat Fish query; using curated Gulf fish list (%d species)",
                     len(GULF_FISH_SPECIES))
            continue
        log.info("\nQuerying iNaturalist for %s (taxon %d)...", group_name, taxon_id)
        inat_data = inat_species_for_month(
            taxon_id, cfg["lat"], cfg["lng"], cfg["radius"], months_str,
            require_native=False,
        )
        log.info("  Found %d %s species", len(inat_data), group_name)
        for sci, info in inat_data.items():
            if info["count"] < 1 or sci in seen_species:
                continue
            seen_species.add(sci)
            species_list.append({
                "common_name": info["common_name"] or sci.split()[0],
                "scientific_name": sci,
                "inat_count": info["count"],
                "taxon_id": info["taxon_id"],
                "ancestor_ids": info.get("ancestor_ids", []),
                "default_photo": info.get("default_photo"),
                "group": group_name,
            })
        time.sleep(0.5)

    log.info("\nResolving iNaturalist taxon IDs for curated Gulf fish...")
    for fish in GULF_FISH_SPECIES:
        sci = fish["scientific_name"]
        if sci in seen_species:
            continue
        seen_species.add(sci)
        tid = None
        try:
            tr = requests.get(
                f"{INAT_API}/taxa",
                params={"q": sci, "per_page": 3, "is_active": "true"},
                headers=HEADERS, timeout=15,
            )
            for t in tr.json().get("results", []):
                if t.get("name", "").lower() == sci.lower():
                    tid = t["id"]
                    break
            if not tid:
                results = tr.json().get("results", [])
                if results:
                    tid = results[0]["id"]
        except Exception:
            pass
        species_list.append({
            "common_name": fish["common_name"],
            "scientific_name": sci,
            "inat_count": 0,
            "taxon_id": tid,
            "ancestor_ids": [],
            "default_photo": None,
            "group": "Fish",
            "facts": fish.get("facts", ""),
            "desc_source": "Curated",
            "seasonality": [2] * 12,
        })
        log.info("  %s -> iNat taxon %s", fish["common_name"], tid or "not found")
        time.sleep(0.3)

    # ── Supplement Seaweed & Algae from OBIS ──────────────────────────
    sea_cache_path = cfg["output_dir"] / ".sea_cache.json"
    sea_cache = load_json(sea_cache_path)

    log.info("\nQuerying OBIS for additional Seaweed & Algae species...")
    obis_species = fetch_obis_seaweed(
        cfg["lat"], cfg["lng"], radius_deg=1.0, max_species=30,
        cache=sea_cache, cache_path=sea_cache_path,
    )
    obis_added = 0
    for entry in obis_species:
        sci = entry["scientific_name"]
        if sci in seen_species:
            continue
        seen_species.add(sci)
        species_list.append(entry)
        obis_added += 1
    log.info("  Added %d new seaweed species from OBIS (total sea life: %d)",
             obis_added, len(species_list))

    # Resolve common names for OBIS species via WoRMS vernacular names
    log.info("\nResolving common names for OBIS-sourced seaweed...")
    for entry in species_list:
        if entry.get("data_source") != "OBIS":
            continue
        aphia = entry.get("aphia_id")
        if not aphia:
            continue
        vern_key = f"obis_vern_{aphia}"
        if vern_key in sea_cache:
            vname = sea_cache[vern_key]
        else:
            vname = ""
            try:
                r = requests.get(
                    f"{WORMS_API}/AphiaVernacularsByAphiaID/{aphia}",
                    headers=HEADERS, timeout=10,
                )
                if r.status_code == 200:
                    for vn in r.json():
                        if vn.get("language_code") == "eng" and vn.get("vernacular"):
                            vname = vn["vernacular"]
                            break
            except Exception:
                pass
            sea_cache[vern_key] = vname
            save_json(sea_cache_path, sea_cache)
            time.sleep(0.25)
        if vname:
            entry["common_name"] = vname
            log.info("    %s -> %s", entry["scientific_name"], vname)

    # Resolve OBIS species to iNaturalist taxon IDs for image fetching
    log.info("\nResolving iNaturalist taxon IDs for OBIS species...")
    for entry in species_list:
        if entry.get("data_source") != "OBIS":
            continue
        aphia = entry.get("aphia_id")
        sci = entry["scientific_name"]
        inat_key = f"obis_inat_{aphia}"
        if inat_key in sea_cache:
            inat_tid = sea_cache[inat_key]
        else:
            inat_tid = None
            try:
                resp = requests.get(
                    f"{INAT_API}/taxa",
                    params={"q": sci, "rank": "species", "is_active": "true", "per_page": 5},
                    headers=HEADERS, timeout=10,
                )
                for t in resp.json().get("results", []):
                    if t.get("name", "").lower() == sci.lower():
                        inat_tid = t["id"]
                        break
                if not inat_tid:
                    results = resp.json().get("results", [])
                    if results:
                        inat_tid = results[0]["id"]
            except Exception:
                pass
            sea_cache[inat_key] = inat_tid
            save_json(sea_cache_path, sea_cache)
            time.sleep(0.3)
        if inat_tid:
            entry["inat_taxon_id"] = inat_tid
            log.info("    %s -> iNat %d", sci, inat_tid)

    # Disambiguate duplicate common names (genus-only) with species epithet
    name_counts: dict[str, int] = {}
    for entry in species_list:
        cn = entry["common_name"]
        name_counts[cn] = name_counts.get(cn, 0) + 1
    for entry in species_list:
        cn = entry["common_name"]
        if name_counts.get(cn, 0) > 1:
            parts = entry["scientific_name"].split()
            if len(parts) >= 2:
                entry["common_name"] = f"{cn} ({parts[1]})"

    # Tag seaweed species with edibility
    for entry in species_list:
        if entry.get("group") == "Seaweed & Algae":
            sci = entry.get("scientific_name", "")
            entry["edible"] = SEAWEED_EDIBILITY.get(sci, "")

    log.info("\nSea life seasonality from iNaturalist...")
    for i, entry in enumerate(species_list):
        if entry.get("data_source") == "OBIS":
            entry["seasonality"] = [1] * 12
            continue
        if entry.get("desc_source") == "Curated" and entry.get("seasonality"):
            continue
        tid = entry.get("taxon_id")
        if not tid:
            entry["seasonality"] = [0] * 12
            continue
        cache_key = f"sea:{tid}"
        if cache_key in seasonality:
            entry["seasonality"] = seasonality[cache_key]
            continue
        log.info("  [%d/%d] %s (taxon %d)", i + 1, len(species_list), entry["common_name"], tid)
        hist = inat_monthly_histogram(tid, cfg["lat"], cfg["lng"], cfg["radius"])
        seasonality[cache_key] = hist
        entry["seasonality"] = hist
        save_json(seasonality_path, seasonality)
        time.sleep(0.3)

    log.info("\nFetching Wikipedia descriptions for sea life (full intro)...")
    fetched_wp = 0
    for i, entry in enumerate(species_list):
        if entry.get("desc_source") == "Curated" and entry.get("facts"):
            continue
        sci = entry["scientific_name"]
        cache_key = f"wiki_{sci.replace(' ', '_')}"
        cached = sea_cache.get(cache_key, {})
        if cached.get("facts") and len(cached["facts"]) >= 300:
            entry["facts"] = cached["facts"]
            entry["desc_source"] = "Wikipedia"
            continue
        log.info("  [%d/%d] %s", i + 1, len(species_list), entry["common_name"])
        wp = scrape_wikipedia(sci, sentences=0, max_chars=1500)
        sea_cache[cache_key] = wp
        save_json(sea_cache_path, sea_cache)
        fetched_wp += 1
        if wp.get("facts"):
            entry["facts"] = wp["facts"]
            entry["desc_source"] = "Wikipedia"
            log.info("    Wikipedia (%d chars): %s", len(wp["facts"]), wp["facts"][:70] + "...")
        else:
            entry["facts"] = ""
            entry["desc_source"] = ""
            log.info("    No Wikipedia data")
        time.sleep(0.4)
    log.info("  Fetched %d new Wikipedia descriptions", fetched_wp)

    sw_overrides = 0
    for entry in species_list:
        if entry.get("group") == "Seaweed & Algae":
            sci = entry.get("scientific_name", "")
            curated = SEAWEED_CHARACTERISTICS.get(sci)
            if curated:
                entry["facts"] = curated
                entry["desc_source"] = "Curated"
                sw_overrides += 1
    log.info("  Applied curated seaweed descriptions: %d species", sw_overrides)

    log.info("\nEnriching sea life with WoRMS taxonomy & attributes...")
    worms_hits = 0
    for i, entry in enumerate(species_list):
        sci = entry["scientific_name"]
        worms_key = f"worms_{sci.replace(' ', '_')}"
        if worms_key in sea_cache:
            wd = sea_cache[worms_key]
        else:
            log.info("  [%d/%d] %s", i + 1, len(species_list), entry["common_name"])
            wd = fetch_worms_record(sci)
            sea_cache[worms_key] = wd
            save_json(sea_cache_path, sea_cache)
            time.sleep(0.35)
        if wd:
            worms_hits += 1
            for f in ("phylum", "class", "order", "family"):
                if f in wd and f not in entry:
                    entry[f] = wd[f]
            if "body_sizes" in wd:
                entry["body_size"] = wd["body_sizes"][0]
            if "functional_groups" in wd:
                entry["functional_group"] = ", ".join(wd["functional_groups"][:3])
            if "habitats" in wd:
                entry["habitat"] = ", ".join(wd["habitats"][:4])
            if "depth" in wd:
                entry["depth"] = wd["depth"]
            if "distribution" in wd:
                entry["distribution"] = ", ".join(wd["distribution"])
    log.info("  WoRMS data for %d / %d species", worms_hits, len(species_list))

    if not cfg.get("skip_images"):
        log.info("\nDownloading sea life images...")
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
            tid = entry.get("inat_taxon_id") or entry.get("taxon_id")
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

        retry_count = 0
        for entry in species_list:
            if entry.get("image_1") or entry.get("group") != "Seaweed & Algae":
                continue
            sci = entry["scientific_name"]
            name = entry["common_name"]
            group_dir = out_dir / safe_filename(entry["group"])
            base = safe_filename(name)
            dest1 = group_dir / f"{base}_1.jpg"
            if dest1.exists() and dest1.stat().st_size > 1000:
                entry["image_1"] = str(dest1.relative_to(cfg["output_dir"]))
                continue
            log.info("  Seaweed image retry: %s (%s)", name, sci)
            photo_urls = []
            try:
                sr = requests.get(
                    f"{INAT_API}/taxa",
                    params={"q": sci, "per_page": 5, "is_active": "true"},
                    headers=HEADERS, timeout=15,
                )
                for t in sr.json().get("results", []):
                    dp = t.get("default_photo", {})
                    url = dp.get("medium_url") or dp.get("url", "")
                    if url:
                        photo_urls.append(url.replace("/medium.", "/large.").replace("/square.", "/large."))
                        break
            except Exception:
                pass
            if not photo_urls:
                try:
                    wmc = requests.get(
                        "https://commons.wikimedia.org/w/api.php",
                        params={
                            "action": "query", "generator": "search",
                            "gsrsearch": f"filetype:bitmap {sci}",
                            "gsrlimit": "3", "prop": "imageinfo",
                            "iiprop": "url", "iiurlwidth": "800",
                            "format": "json",
                        },
                        headers=HEADERS, timeout=15,
                    )
                    pages = wmc.json().get("query", {}).get("pages", {})
                    for pid in sorted(pages.keys()):
                        ii = pages[pid].get("imageinfo", [{}])[0]
                        thumb = ii.get("thumburl") or ii.get("url", "")
                        if thumb and not thumb.endswith(".svg"):
                            photo_urls.append(thumb)
                            break
                except Exception:
                    pass
            for url in photo_urls:
                if download_image(url, dest1):
                    entry["image_1"] = str(dest1.relative_to(cfg["output_dir"]))
                    retry_count += 1
                    success += 1
                    break
            time.sleep(0.4)
        if retry_count:
            log.info("  Seaweed image retry recovered %d images", retry_count)

        log.info("  Images for %d / %d sea life species", success, len(species_list))
    else:
        for entry in species_list:
            base = safe_filename(entry["common_name"])
            group_dir = out_dir / safe_filename(entry.get("group", ""))
            d1 = group_dir / f"{base}_1.jpg"
            d2 = group_dir / f"{base}_2.jpg"
            entry["image_1"] = str(d1.relative_to(cfg["output_dir"])) if d1.exists() else ""
            entry["image_2"] = str(d2.relative_to(cfg["output_dir"])) if d2.exists() else ""

    species_list.sort(key=lambda e: (
        SEA_LIFE_GROUP_ORDER.index(e["group"]) if e["group"] in SEA_LIFE_GROUP_ORDER else 99,
        -(e.get("inat_count", 0) or e.get("obis_records", 0)),
    ))

    log.info("  Total sea life: %d species", len(species_list))
    return species_list


def build_sea_life_card(entry: dict, current_month_0: int, cfg: dict) -> str:
    name = esc(title_case_common_name(entry.get("common_name", "")))
    sci = esc(entry.get("scientific_name", ""))
    desc = esc(entry.get("facts", ""))
    inat_count = entry.get("inat_count", 0)

    img1 = entry.get("image_1", "")
    img2 = entry.get("image_2", "")

    if img1:
        layer1 = f'<div class="img-layer active" style="background-image:url(\'{esc_img_path(img1)}\')"></div>'
    else:
        layer1 = '<div class="img-layer active" style="background:#ddd"></div>'
    if img2:
        layer2 = f'<div class="img-layer" style="background-image:url(\'{esc_img_path(img2)}\')"></div>'
        flip_btn = '<button class="flip-btn" onclick="flipImg(this)">&#8644;</button>'
    else:
        layer2 = ""
        flip_btn = ""

    season_html = build_season_bar_html(entry.get("seasonality", [0] * 12), current_month_0)

    meta_tags = ""
    if inat_count:
        meta_tags += f'<span class="meta-tag">iNat: {inat_count} obs</span>'
    if entry.get("family"):
        meta_tags += f'<span class="meta-tag">{esc(entry["family"])}</span>'
    edible = entry.get("edible", "")
    if edible == "edible":
        meta_tags += '<span class="meta-tag" style="color:#2d7a3a;border-color:#2d7a3a">Edible</span>'
    elif edible == "not edible":
        meta_tags += '<span class="meta-tag" style="color:#999;border-color:#ccc">Not Edible</span>'

    seas = entry.get("seasonality", [0] * 12)
    apr_may = 1 if (month_level(seas, 3) > 0 or month_level(seas, 4) > 0) else 0

    group_label = esc(entry.get("group", ""))
    taxonomy_parts = []
    for rank in ("phylum", "class", "order"):
        val = entry.get(rank)
        if val:
            taxonomy_parts.append(esc(val))
    taxonomy_str = " · ".join(taxonomy_parts) if taxonomy_parts else ""
    family_tag = ""
    if taxonomy_str:
        family_tag = (f'<div class="family-label" style="font-size:10px;color:var(--muted);'
                      f'text-transform:uppercase;letter-spacing:.5px;font-weight:500;'
                      f'margin-bottom:2px">{taxonomy_str}</div>')
    elif group_label:
        family_tag = (f'<div class="family-label" style="font-size:10px;color:var(--muted);'
                      f'text-transform:uppercase;letter-spacing:.5px;font-weight:500;'
                      f'margin-bottom:2px">{group_label}</div>')

    details_parts = []
    if entry.get("body_size"):
        details_parts.append(f"<strong>Size:</strong> {esc(entry['body_size'])}")
    if entry.get("habitat"):
        details_parts.append(f"<strong>Habitat:</strong> {esc(entry['habitat'])}")
    if entry.get("depth"):
        details_parts.append(f"<strong>Depth:</strong> {esc(entry['depth'])} m")
    if entry.get("functional_group"):
        details_parts.append(f"<strong>Ecology:</strong> {esc(entry['functional_group'])}")
    if entry.get("distribution"):
        details_parts.append(f"<strong>Range:</strong> {esc(entry['distribution'])}")
    details_html = ""
    if details_parts:
        details_html = ('<div class="field-ids">'
                        + "".join(f'<div class="field-id">{p}</div>' for p in details_parts)
                        + '</div>')

    return f"""<div class="bird-card" data-apr-may="{apr_may}">
<div class="card-image">{layer1}{layer2}{flip_btn}</div>
<div class="card-body">
<div class="card-top">
{family_tag}
<h3>{name}</h3>
<span class="latin">{sci}</span>
</div>
{season_html}
<div class="meta-row">{meta_tags}</div>
{"<p class='description'>" + desc + "</p>" if desc else ""}
{details_html}
</div>
</div>"""


# ── HTML generation ────────────────────────────────────────────────────

CSS = """\
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#fff;--card:#fff;--text:#1a1a1a;--muted:#707070;--border:#d0d0d0;--accent:#444}
body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans','Helvetica Neue',system-ui,sans-serif;font-size:15px;line-height:1.6}
a{color:inherit;text-decoration:none}
.layout{display:flex;min-height:100vh}
.sidebar{position:sticky;top:0;height:100vh;width:220px;flex-shrink:0;background:#fafafa;border-right:1px solid var(--border);overflow-y:auto;padding:10px 0}
.sidebar-head{padding:0 16px 10px}
.mode-toggle{display:flex;margin:18px 16px 8px;border:1px solid var(--border);border-radius:6px;overflow:hidden}
.mode-toggle .mode-btn{flex:1}
.mode-btn{padding:7px 0;font-size:9px;font-weight:600;letter-spacing:.5px;text-align:center;cursor:pointer;border:none;background:transparent;color:var(--muted);font-family:'IBM Plex Sans','Helvetica Neue',system-ui,sans-serif;transition:background .2s,color .2s}
.mode-btn.active{background:var(--text);color:#fff}
.nav-links{padding:12px 8px}
.nav-link{display:flex;align-items:center;gap:6px;padding:6px 10px;font-size:12px;font-weight:500;color:var(--muted)}
.nav-link:hover{color:var(--text)}
.nav-link.active{color:var(--text);font-weight:600}
.nav-count{margin-left:auto;font-size:10px;opacity:.5}
.main{flex:1;max-width:1200px;padding:8px 40px 80px}
.page-header{text-align:left;padding:8px 0 12px;border-bottom:1px solid var(--border);margin-bottom:12px}
.page-header h1{font-family:'IBM Plex Serif',Georgia,serif;font-size:26px;font-weight:600;letter-spacing:-.2px}
.page-header .sub{font-size:13px;color:var(--muted);margin-top:2px}
.page-header .meta-line{font-size:12px;color:var(--muted);margin-top:4px;font-family:'IBM Plex Mono',monospace;display:flex;gap:16px;flex-wrap:wrap}
.page-header .locations{font-size:11px;color:var(--muted);margin-top:3px;letter-spacing:.2px}
.trip-info{padding:0;margin-bottom:12px}
.wx-forecast{margin-bottom:12px}
.wx-grid{display:flex;gap:6px;flex-wrap:wrap}
.wx-day{flex:0 0 auto;width:130px;padding:8px;border:1px solid var(--border);border-radius:6px;text-align:center;font-size:11px;line-height:1.4;background:#fafafa}
.wx-date{font-weight:600;font-size:11px;margin-bottom:2px}
.wx-icon{font-size:24px;line-height:1.2}
.wx-desc{font-size:10px;color:#555;margin-bottom:2px}
.wx-temp{font-weight:600;font-size:12px;margin-bottom:1px}
.wx-wind{font-size:9px;color:var(--muted)}
.wx-seas{font-size:9px;color:var(--muted)}
.wx-sun{font-size:9px;color:var(--muted)}
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
.bird-audio{margin:8px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.bird-audio audio{height:32px;flex:1;min-width:180px;max-width:320px}
.sounds-link{font-size:11px;color:#2E6B94;text-decoration:none;font-weight:500;letter-spacing:.2px;white-space:nowrap}
.sounds-link:hover{text-decoration:underline}
.card-footer{margin-top:auto;padding-top:8px;border-top:1px solid var(--border)}
.taxonomy{font-size:10px;color:var(--muted);letter-spacing:.2px;font-family:'IBM Plex Mono',monospace}
.back-top{position:fixed;bottom:24px;right:24px;background:var(--text);color:var(--bg);width:36px;height:36px;display:flex;align-items:center;justify-content:center;font-size:18px;cursor:pointer;border:none;opacity:.3;transition:opacity .2s}
.back-top:hover{opacity:.8}
.panel{display:none}
.panel.active{display:block}
.apr-may-badge{display:inline-block;font-size:10px;font-weight:600;padding:1px 6px;border-radius:8px;margin-left:6px;vertical-align:middle;letter-spacing:.3px}
.badge-present{background:#e8f5e9;color:#2e7d32}
.badge-absent{background:#fce4ec;color:#c62828}
.filter-bar{display:flex;align-items:center;gap:10px;padding:6px 0;margin-bottom:8px;flex-wrap:wrap}
.filter-btn{font-family:'IBM Plex Sans',sans-serif;font-size:12px;padding:4px 12px;border-radius:14px;border:1.5px solid var(--muted);background:none;color:var(--text);cursor:pointer;transition:all .15s}
.filter-btn:hover{border-color:var(--accent)}
.filter-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.bird-card.season-hidden{display:none}
.season-bar .mo.am-hl{outline:1.5px solid #2e7d32;outline-offset:-1px;border-radius:2px}
.season-bar .mo.am-hl-l{border-top-right-radius:0;border-bottom-right-radius:0}
.season-bar .mo.am-hl-r{border-top-left-radius:0;border-bottom-left-radius:0}
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
    name = esc(title_case_common_name(bird.get("common_name", "")))
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

    seas = bird.get("seasonality", [0] * 12)
    apr_may = 1 if (month_level(seas, 3) > 0 or month_level(seas, 4) > 0) else 0

    family_tag = f'<div class="family-label" style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;font-weight:500;margin-bottom:2px">{taxonomy}</div>' if taxonomy else ""

    audio_ml_id = bird.get("audio_ml_id", "")
    sounds_url = bird.get("sounds_url", "")
    audio_html = ""
    if audio_ml_id:
        audio_src = f"https://cdn.download.ams.birds.cornell.edu/api/v2/asset/{esc(audio_ml_id)}/mp4/1280"
        audio_html = (
            f'<div class="bird-audio">'
            f'<audio controls preload="none" src="{audio_src}">'
            f'<a href="{audio_src}">Listen</a></audio>'
        )
        if sounds_url:
            audio_html += (
                f'<a href="{esc(sounds_url)}" target="_blank" rel="noopener" '
                f'class="sounds-link">All sounds &#8599;</a>'
            )
        audio_html += '</div>'
    elif sounds_url:
        audio_html = (
            f'<div class="bird-audio">'
            f'<a href="{esc(sounds_url)}" target="_blank" rel="noopener" '
            f'class="sounds-link">Listen on All About Birds &#8599;</a>'
            f'</div>'
        )

    return f"""<div class="bird-card" data-apr-may="{apr_may}">
<div class="card-image">{layer1}{layer2}{flip_btn}</div>
<div class="card-body">
<div class="card-top">
{family_tag}
<h3>{name}</h3>
<span class="latin">{sci}</span>
{badge}
</div>
{season_html}
<div class="meta-row">{meta_tags}</div>
{"<p class='description'>" + desc + "</p>" if desc else ""}
{"<p class='find-bird'><strong>Where to look:</strong> " + find + "</p>" if find else ""}
{audio_html}
{field_ids_html}
</div>
</div>"""


def build_plant_card(plant: dict, current_month_0: int, cfg: dict) -> str:
    name = esc(title_case_common_name(plant.get("common_name", "")))
    sci = esc(plant.get("scientific_name", ""))
    desc = esc(plant.get("facts", ""))
    habitat = esc(plant.get("habitat", ""))
    family = esc(plant.get("family", ""))
    conservation = plant.get("conservation", "")
    inat_count = plant.get("inat_count", 0)

    img1 = plant.get("image_1", "")
    img2 = plant.get("image_2", "")

    if img1:
        layer1 = f'<div class="img-layer active" style="background-image:url(\'{esc_img_path(img1)}\')"></div>'
    else:
        layer1 = '<div class="img-layer active" style="background:#ddd"></div>'
    if img2:
        layer2 = f'<div class="img-layer" style="background-image:url(\'{esc_img_path(img2)}\')"></div>'
        flip_btn = '<button class="flip-btn" onclick="flipImg(this)">&#8644;</button>'
    else:
        layer2 = ""
        flip_btn = ""

    season_html = build_season_bar_html(plant.get("seasonality", [0] * 12), current_month_0)

    meta_tags = ""
    if inat_count:
        meta_tags += f'<span class="meta-tag">iNat: {inat_count} obs</span>'

    seas = plant.get("seasonality", [0] * 12)
    apr_may = 1 if (month_level(seas, 3) > 0 or month_level(seas, 4) > 0) else 0

    badge_cls = conservation_badge_class(conservation) if conservation else ""
    badge = f'<span class="conservation {badge_cls}">{esc(conservation)}</span>' if conservation else ""

    family_tag = f'<div class="family-label" style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;font-weight:500;margin-bottom:2px">{family}</div>' if family else ""

    return f"""<div class="bird-card" data-apr-may="{apr_may}">
<div class="card-image">{layer1}{layer2}{flip_btn}</div>
<div class="card-body">
<div class="card-top">
{family_tag}
<h3>{name}</h3>
<span class="latin">{sci}</span>
{badge}
</div>
{season_html}
<div class="meta-row">{meta_tags}</div>
{"<p class='description'>" + desc + "</p>" if desc else ""}
{"<p class='find-bird'><strong>Habitat:</strong> " + habitat + "</p>" if habitat else ""}
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
        if g == "Trees":
            sub = {}
            for rec in groups[g]:
                fam = rec.get("family", "")
                sg = TREE_SUBGROUPS.get(fam, "Other Broadleaf")
                sub.setdefault(sg, []).append(rec)
            for sg_name in TREE_SUBGROUP_ORDER:
                if sg_name not in sub:
                    continue
                card_parts.append(
                    f'<div class="subgroup-header" style="font-size:13px;font-weight:600;'
                    f'color:var(--muted);padding:12px 0 4px;border-bottom:1px solid #eee;'
                    f'margin:8px 0 12px;letter-spacing:.3px">{esc(sg_name)}</div>'
                )
                for rec in sub[sg_name]:
                    card_parts.append(card_fn(rec, current_month_0, cfg))
        else:
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


def generate_html(birds: list[dict], plants: list[dict], cfg: dict, sea_life=None):
    """Generate the combined index.html with Birds/Plants/Sea Life toggle."""
    sea_life = sea_life or []
    fish_count = sum(1 for s in sea_life if s.get("group") == "Fish")
    thresholds = {"Birds": (len(birds), 100), "Plants": (len(plants), 300),
                  "Sea Life": (len(sea_life), 100), "Fish": (fish_count, 20)}
    for label, (actual, minimum) in thresholds.items():
        if actual < minimum:
            log.error("VALIDATION FAILED: %s has %d species (minimum %d). "
                      "Aborting to prevent deploying incomplete data.", label, actual, minimum)
            sys.exit(1)
    log.info("  Validation passed: Birds=%d, Plants=%d, Sea=%d, Fish=%d",
             len(birds), len(plants), len(sea_life), fish_count)

    current_month_0 = cfg["date"].month - 1
    date_str = cfg["date"].strftime("%B %d, %Y")
    lat_str = f"{abs(cfg['lat']):.4f} {'N' if cfg['lat'] >= 0 else 'S'}"
    lng_str = f"{abs(cfg['lng']):.4f} {'W' if cfg['lng'] < 0 else 'E'}"

    has_birds = len(birds) > 0
    has_plants = len(plants) > 0
    has_sea = bool(sea_life)
    has_both = has_birds and has_plants

    bird_nav, bird_cards = "", ""
    plant_nav, plant_cards = "", ""
    sea_nav, sea_cards = "", ""

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
    if has_sea:
        sea_nav, sea_cards = build_grouped_html(
            sea_life, SEA_LIFE_GROUP_ORDER, SEA_LIFE_GROUP_COLORS,
            build_sea_life_card, current_month_0, cfg, "s-",
        )

    moon_html = cfg.get("moon_html", "")
    weather_html = cfg.get("weather_html", "")

    trip_html = ""
    trip_parts = []
    if moon_html:
        trip_parts.append(moon_html)
    if weather_html:
        trip_parts.append(weather_html)
    if trip_parts:
        trip_html = '<div class="trip-info">' + "".join(trip_parts) + '</div>'

    num_modes = sum([has_birds, has_plants, has_sea])
    toggle_html = ""
    if num_modes > 0:
        btns = []
        if has_birds:
            btns.append('<button class="mode-btn active" id="btn-birds" onclick="switchMode(\'birds\')">Birds</button>')
        if has_plants:
            btns.append('<button class="mode-btn" id="btn-plants" onclick="switchMode(\'plants\')">Plants</button>')
        if has_sea:
            btns.append('<button class="mode-btn" id="btn-sea" onclick="switchMode(\'sea\')">Sea</button>')
        toggle_html = '<div class="mode-toggle">' + "".join(btns) + '</div>'

    bird_nav_block = f'<div class="nav-links" id="nav-birds">{bird_nav}</div>' if has_birds else ""
    plant_nav_display = ' style="display:none"' if (has_birds and has_plants) else ""
    plant_nav_block = f'<div class="nav-links" id="nav-plants"{plant_nav_display}>{plant_nav}</div>' if has_plants else ""
    sea_nav_display = ' style="display:none"' if (has_birds or has_plants) else ""
    sea_nav_block = f'<div class="nav-links" id="nav-sea"{sea_nav_display}>{sea_nav}</div>' if has_sea else ""

    total_species = len(birds) + len(plants) + len(sea_life)
    stat_text = ""
    if has_birds:
        stat_text = f'{len(birds)} Bird Species'
    elif has_plants:
        stat_text = f'{len(plants)} Plant Species'
    elif has_sea:
        stat_text = f'{len(sea_life)} Sea Life Species'

    bird_panel_cls = "panel active" if has_birds else "panel"
    plant_panel_cls = "panel" if has_birds else ("panel active" if has_plants else "panel")
    sea_panel_cls = "panel" if (has_birds or has_plants) else ("panel active" if has_sea else "panel")

    filter_bar = ('<div class="filter-bar">'
                  '<span style="font-size:12px;color:var(--muted)">Filter:</span>'
                  '<button class="filter-btn" onclick="toggleAprMay(this)">Apr / May Only</button>'
                  '</div>')

    bird_panel = ""
    if has_birds:
        bird_panel = f"""<div class="{bird_panel_cls}" id="panel-birds">
<div class="page-header">
<h1>{esc(cfg['place'])} Bird Checklist</h1>
<div class="sub">{len(birds)} Species</div>
<div class="meta-line"><span>{date_str}</span><span>{lat_str}, {lng_str}</span></div>
<div class="locations">Sources: eBird, Cornell All About Birds, iNaturalist, Wikipedia, NOAA Tides &amp; Currents</div>
</div>
{filter_bar}
{trip_html}
{bird_cards}
</div>"""

    plant_panel = ""
    if has_plants:
        plant_panel = f"""<div class="{plant_panel_cls}" id="panel-plants">
<div class="page-header">
<h1>{esc(cfg['place'])} Plant Checklist</h1>
<div class="sub">{len(plants)} Species</div>
<div class="meta-line"><span>{date_str}</span><span>{lat_str}, {lng_str}</span></div>
<div class="locations">Sources: iNaturalist, {esc(PLANT_REFERENCE_ATTRIBUTION)}</div>
</div>
{filter_bar}
{trip_html}
{plant_cards}
</div>"""

    sea_panel = ""
    if has_sea:
        sea_panel = f"""<div class="{sea_panel_cls}" id="panel-sea">
<div class="page-header">
<h1>{esc(cfg['place'])} Sea Life Checklist</h1>
<div class="sub">{len(sea_life)} Species</div>
<div class="meta-line"><span>{date_str}</span><span>{lat_str}, {lng_str}</span></div>
<div class="locations">Sources: iNaturalist, OBIS, NOAA Fisheries, FWC, Wikipedia, PhycoLab</div>
</div>
{filter_bar}
{trip_html}
{sea_cards}
</div>"""

    switch_js = f"""
function switchMode(mode){{
  var panels=['birds','plants','sea','map'];
  var suffixes={{'birds':' Bird Species','plants':' Plant Species','sea':' Sea Life Species'}};
  panels.forEach(function(p){{
    var el=document.getElementById('panel-'+p);if(el){{el.classList.toggle('active',p===mode);el.style.display=p===mode?'block':'none';}}
    var nv=document.getElementById('nav-'+p);if(nv)nv.style.display=p===mode?'':'none';
    var bt=document.getElementById('btn-'+p);if(bt)bt.classList.toggle('active',p===mode);
  }});
  if(mode==='map'&&typeof initMap==='function'){{initMap();return;}}
  scrollTo({{top:0}});
  var panel=document.getElementById('panel-'+mode);
  if(panel){{
    var btn=panel.querySelector('.filter-btn');
    if(btn&&!btn.classList.contains('active'))toggleAprMay(btn);
    else{{
      var total=panel.querySelectorAll('.bird-card:not(.season-hidden)').length;
      var st=document.getElementById('stat-text');
      if(st)st.textContent=total+(suffixes[mode]||' Species');
      var sub=panel.querySelector('.page-header .sub');
      if(sub)sub.textContent=total+' Species';
    }}
  }}
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
{toggle_html}
{bird_nav_block}
{plant_nav_block}
{sea_nav_block}
</nav>
<main class="main">
{bird_panel}
{plant_panel}
{sea_panel}
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
function initAprMay(){{
  document.querySelectorAll('.bird-card').forEach(function(card){{
    var present=card.getAttribute('data-apr-may')==='1';
    var top=card.querySelector('.card-top');
    if(top){{
      var badge=document.createElement('span');
      badge.className='apr-may-badge '+(present?'badge-present':'badge-absent');
      badge.textContent=present?'Apr / May':'Not Apr / May';
      var latin=top.querySelector('.latin');
      if(latin)latin.after(badge);
      else top.appendChild(badge);
    }}
  }});
}}
function toggleAprMay(btn){{
  btn.classList.toggle('active');
  var on=btn.classList.contains('active');
  var panel=btn.closest('.panel');
  if(!panel)panel=document;
  panel.querySelectorAll('.bird-card').forEach(function(card){{
    if(on && card.getAttribute('data-apr-may')==='0'){{
      card.classList.add('season-hidden');
    }} else {{
      card.classList.remove('season-hidden');
    }}
  }});
  panel.querySelectorAll('.group-section').forEach(function(sec){{
    var vis=sec.querySelectorAll('.bird-card:not(.season-hidden)').length;
    var gc=sec.querySelector('.group-count');
    if(gc)gc.textContent=vis+' species';
  }});
  panel.querySelectorAll('.season-bar').forEach(function(bar){{
    var months=bar.querySelectorAll('.mo');
    if(months.length>=5){{
      var apr=months[3],may=months[4];
      if(on){{
        apr.classList.add('am-hl','am-hl-l');
        may.classList.add('am-hl','am-hl-r');
        apr.classList.remove('now');
        may.classList.remove('now');
      }} else {{
        apr.classList.remove('am-hl','am-hl-l');
        may.classList.remove('am-hl','am-hl-r');
      }}
    }}
  }});
  var total=panel.querySelectorAll('.bird-card:not(.season-hidden)').length;
  var stat=document.getElementById('stat-text');
  if(stat){{
    var suffix=' Species';
    if(panel.id==='panel-birds')suffix=' Bird Species';
    else if(panel.id==='panel-plants')suffix=' Plant Species';
    else if(panel.id==='panel-sea')suffix=' Sea Life Species';
    stat.textContent=total+suffix;
  }}
  var sub=panel.querySelector('.page-header .sub');
  if(sub){{sub.textContent=total+' Species';}}
  var navId='nav-birds';
  if(panel.id==='panel-plants')navId='nav-plants';
  else if(panel.id==='panel-sea')navId='nav-sea';
  var nav=document.getElementById(navId);
  if(nav){{
    nav.querySelectorAll('.nav-link').forEach(function(link){{
      var href=link.getAttribute('href');
      if(!href)return;
      var sec=document.querySelector(href);
      if(!sec)return;
      var cnt=sec.querySelectorAll('.bird-card:not(.season-hidden)').length;
      var nc=link.querySelector('.nav-count');
      if(nc)nc.textContent=cnt;
    }});
  }}
}}
{switch_js}
document.addEventListener('DOMContentLoaded',function(){{
  initAprMay();
  setTimeout(function(){{
    var activePanel=document.querySelector('.panel.active');
    if(activePanel){{
      var btn=activePanel.querySelector('.filter-btn');
      if(btn)toggleAprMay(btn);
    }}
  }},100);
}});
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
    --place "Gulf Islands" \\
    --date 2026-04-28 \\
    --lat 30.3298 --lng -86.1650 \\
    --ebird-key YOUR_KEY
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
    parser.add_argument("--tide-station", default="",
                        help="NOAA station ID for auto tide fetch (e.g. 8729511)")
    parser.add_argument("--tide-dates", default="",
                        help="Tide date range YYYYMMDD,YYYYMMDD (e.g. 20260425,20260502)")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output directory (default: output/<place-slug>)")
    parser.add_argument("--skip-images", action="store_true", help="Skip downloading images")
    parser.add_argument("--birds-only", action="store_true", help="Only run bird pipeline")
    parser.add_argument("--plants-only", action="store_true", help="Only run plant pipeline")
    parser.add_argument("--render-only", action="store_true",
                        help="Skip all API calls; rebuild HTML from cached snapshot")

    args = parser.parse_args()

    try:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        parser.error("Date must be YYYY-MM-DD format")

    output_dir = args.output or Path("output") / place_slug(args.place)
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / ".snapshot.json"

    # ── render-only: reload cached data, skip all API calls ──────────
    if args.render_only:
        if not snapshot_path.exists():
            parser.error(f"--render-only requires a snapshot file at {snapshot_path}. "
                         "Run a full build first.")
        log.info("Field Checklist Builder  [render-only mode]")
        log.info("  Loading snapshot from %s", snapshot_path)
        snap = json.loads(snapshot_path.read_text())
        birds = snap.get("birds", [])
        plants = snap.get("plants", [])
        sea_life = snap.get("sea_life", [])
        cfg = {
            "place": snap["cfg"]["place"],
            "date": datetime.strptime(snap["cfg"]["date"], "%Y-%m-%d"),
            "lat": snap["cfg"]["lat"],
            "lng": snap["cfg"]["lng"],
            "radius": snap["cfg"]["radius"],
            "ebird_key": snap["cfg"].get("ebird_key", ""),
            "moon": snap["cfg"].get("moon", ""),
            "tides": snap["cfg"].get("tides", ""),
            "output_dir": output_dir,
            "skip_images": True,
            "moon_html": snap["cfg"].get("moon_html", ""),
            "weather_html": snap["cfg"].get("weather_html", ""),
        }
        log.info("  Birds: %d  Plants: %d  Sea Life: %d",
                 len(birds), len(plants), len(sea_life))
        log.info("")
        log.info("Generating combined HTML...")
        generate_html(birds, plants, cfg, sea_life)
        log.info("Done!  Output: %s", output_dir / "index.html")
        return

    # ── full build: fetch everything from APIs ───────────────────────
    do_birds = not args.plants_only
    do_plants = not args.birds_only

    if do_birds and not args.ebird_key:
        parser.error("--ebird-key is required for birds (or set EBIRD_API_KEY env var). "
                      "Get a free key at https://ebird.org/api/keygen")

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

    sea_life = []
    if do_plants:
        sea_life = run_sea_life(cfg)

    if args.tide_dates:
        parts = args.tide_dates.split(",")
        if len(parts) == 2:
            log.info("\nFetching moon phase data for %s – %s...", parts[0], parts[1])
            cfg["moon_html"] = compute_moon_phases(parts[0], parts[1])
            log.info("Fetching weather forecast for %s – %s...", parts[0], parts[1])
            cfg["weather_html"] = compute_weather_forecast(
                cfg["lat"], cfg["lng"], parts[0], parts[1]
            )

    # ── save snapshot for future --render-only runs ──────────────────
    def _serializable(obj):
        if isinstance(obj, (datetime, Path)):
            return str(obj)
        raise TypeError(f"Not serializable: {type(obj)}")

    snap_cfg = {
        "place": cfg["place"],
        "date": target_date.strftime("%Y-%m-%d"),
        "lat": cfg["lat"],
        "lng": cfg["lng"],
        "radius": cfg["radius"],
        "moon_html": cfg.get("moon_html", ""),
        "weather_html": cfg.get("weather_html", ""),
    }
    snapshot = {"cfg": snap_cfg, "birds": birds, "plants": plants, "sea_life": sea_life}
    snapshot_path.write_text(json.dumps(snapshot, default=_serializable, ensure_ascii=False))
    log.info("\nSaved data snapshot to %s (%.1f MB)",
             snapshot_path, snapshot_path.stat().st_size / 1e6)

    log.info("\n" + "=" * 60)
    log.info("Generating combined HTML...")
    generate_html(birds, plants, cfg, sea_life)

    log.info("\n" + "=" * 60)
    log.info("Done!")
    if birds:
        log.info("  Birds:  %d species", len(birds))
    if plants:
        log.info("  Plants: %d species", len(plants))
    if sea_life:
        log.info("  Sea Life: %d species", len(sea_life))
    log.info("  Output: %s", output_dir)
    log.info("  Open:   %s", output_dir / "index.html")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
