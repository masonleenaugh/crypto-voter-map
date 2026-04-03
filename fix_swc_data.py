#!/usr/bin/env python3
"""
fix_swc_data.py
Applies discrepancies.csv corrections to swc_embedded_data.json.
Adds visibility field (11th element): 1=visible, 0=hidden.

Corrections applied:
  - JSON candidate NOT on SWC     → visibility=0 (hidden)
  - SWC candidate NOT in JSON     → scrape stance/score, add with visibility=1
  - Name mismatch (same slug)     → update name to SWC value
  - Party mismatch                → update party to SWC value
  - Slug mismatch                 → update slug to SWC value
  - All other existing candidates → visibility=1
"""

import json
import re
import time
import csv
import sys
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR           = Path(__file__).parent
DATA_FILE          = BASE_DIR / "swc_embedded_data.json"
DISCREPANCIES_FILE = BASE_DIR / "discrepancies.csv"
SWC_BASE           = "https://www.standwithcrypto.org/us/races/state"
REQUEST_DELAY      = 0.5
TIMEOUT            = 20
HEADERS            = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Stance text → numeric score mapping
STANCE_TO_SCORE = {
    "strongly supportive":  100,
    "supportive":            75,
    "mixed":                 50,
    "against":               25,
    "strongly against":       0,
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    name = name.lower().strip()
    for suffix in [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv", " v"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


def build_url(state: str, district: str) -> str:
    """Return the SWC page URL given state abbr and district label."""
    d = district.strip()
    if d.lower() == "senate":
        return f"{SWC_BASE}/{state}/senate"
    dist_url = d.lower() if d.lower() == "at-large" else d
    return f"{SWC_BASE}/{state}/district/{dist_url}"


def extract_stance_score(text: str) -> int:
    """Extract a numeric score from card text based on stance keywords."""
    t = text.lower()
    for stance, score in sorted(STANCE_TO_SCORE.items(), key=lambda x: -len(x[0])):
        if stance in t:
            return score
    return -1  # unrated / unknown


def fetch_district_candidates(url: str) -> tuple[list[dict], str]:
    """
    Fetch SWC district page and return (candidates, error_msg).
    Each candidate: {"name", "party", "slug", "score"}
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return [], f"request error: {exc}"

    if resp.status_code == 500:
        return [], "HTTP 500 (page not found)"
    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}"

    soup = BeautifulSoup(resp.text, "lxml")
    seen: set[str] = set()
    candidates: list[dict] = []

    for card in soup.find_all("a", href=re.compile(r"/politicians/person/")):
        slug = card["href"].split("/")[-1]
        if slug in seen:
            continue

        img = card.find("img", alt=re.compile(r"^Profile picture of "))
        if not img:
            continue

        name  = img["alt"].replace("Profile picture of ", "").strip()
        text  = card.get_text(separator=" ", strip=True)
        pm    = re.search(r"\((R|D|I)\)", text)
        party = pm.group(1) if pm else "?"
        score = extract_stance_score(text)

        seen.add(slug)
        candidates.append({
            "name":  name,
            "party": party,
            "slug":  slug,
            "score": score,
            "norm":  normalize_name(name),
        })

    return candidates, ""


# ── Parse discrepancies CSV ────────────────────────────────────────────────────
print("Parsing discrepancies.csv…")

# Sets / maps of corrections
slugs_to_hide:   set[str]        = set()   # JSON slugs that are NOT on SWC
new_candidates:  list[dict]      = []      # SWC candidates NOT in JSON
name_updates:    dict[str, str]  = {}      # slug → new name
party_updates:   dict[str, str]  = {}      # slug → new party
slug_renames:    dict[str, str]  = {}      # old slug → new slug

with open(DISCREPANCIES_FILE, newline="") as f:
    for row in csv.DictReader(f):
        state  = row["STATE"]
        dist   = row["DISTRICT"]
        issue  = row["ISSUE"]

        # ── JSON candidate NOT on SWC → hide ─────────────────────────────────
        m = re.match(
            r"JSON candidate NOT on SWC: .+? \([RDIT\?]+\) \[slug: ([^\]]+)\]",
            issue,
        )
        if m:
            slugs_to_hide.add(m.group(1))
            continue

        # ── SWC candidate NOT in JSON → add ──────────────────────────────────
        m = re.match(
            r"SWC candidate NOT in JSON: (.+?) \(([RDIT\?]+)\) \[slug: ([^\]]+)\]",
            issue,
        )
        if m:
            new_candidates.append({
                "state":  state,
                "dist":   dist,
                "name":   m.group(1),
                "party":  m.group(2),
                "slug":   m.group(3),
            })
            continue

        # ── Slug mismatch → rename slug ───────────────────────────────────────
        m = re.match(
            r"Slug mismatch for '.*?': JSON slug='([^']+)' SWC slug='([^']+)'",
            issue,
        )
        if m:
            slug_renames[m.group(1)] = m.group(2)
            continue

        # ── Name mismatch → update name ───────────────────────────────────────
        m = re.match(
            r"Name mismatch \(same slug '([^']+)'\): JSON='.*?' SWC='([^']+)'",
            issue,
        )
        if m:
            name_updates[m.group(1)] = m.group(2)
            continue

        # ── Party mismatch → update party ─────────────────────────────────────
        m = re.match(
            r"Party mismatch for '.*?': JSON=([RDIT\?]+) SWC=([RDIT\?]+)",
            issue,
        )
        if m:
            slug_m = re.search(r"\[slug: ([^\]]+)\]", issue)
            # Slug isn't directly in the party mismatch message.
            # We'll look up the slug from the existing JSON later.
            # Store by name+state+dist for now.
            # Actually we need to find it a different way — store raw row.
            pass

        # ── Party mismatch (alternative: match by name lookup) ─────────────────
        # Handled below during JSON patching since the message has the candidate name
        # but not the slug. We'll resolve by looking up the JSON.

print(f"  Hide: {len(slugs_to_hide)} slugs")
print(f"  Add:  {len(new_candidates)} new candidates")
print(f"  Rename slug: {len(slug_renames)}")
print(f"  Name updates: {len(name_updates)}")

# Re-parse party mismatches with full row context
party_mismatches: list[dict] = []
with open(DISCREPANCIES_FILE, newline="") as f:
    for row in csv.DictReader(f):
        if "Party mismatch" not in row["ISSUE"]:
            continue
        m_name  = re.search(r"Party mismatch for '([^']+)'", row["ISSUE"])
        m_party = re.search(r"JSON=[RDIT\?]+ SWC=([RDIT\?]+)", row["ISSUE"])
        if m_name and m_party:
            party_mismatches.append({
                "state": row["STATE"],
                "dist":  row["DISTRICT"],
                "name":  m_name.group(1),
                "new_party": m_party.group(1),
            })
print(f"  Party updates: {len(party_mismatches)}")

# ── Load current JSON ──────────────────────────────────────────────────────────
print("\nLoading swc_embedded_data.json…")
with open(DATA_FILE) as f:
    raw: list[list] = json.load(f)
print(f"  {len(raw)} records loaded")

# ── Apply corrections to existing records ─────────────────────────────────────
print("\nApplying corrections to existing records…")

# Build a name→slug index for party mismatch lookups
# (party mismatch issues don't include the slug)
name_to_slugs: dict[str, list[int]] = defaultdict(list)
for i, r in enumerate(raw):
    name_to_slugs[normalize_name(r[0])].append(i)

hidden_count  = 0
renamed_count = 0
name_fix_count = 0
party_fix_count = 0

for r in raw:
    # Apply slug rename first (may affect slug_renames lookup)
    old_slug = r[8]
    if old_slug in slug_renames:
        r[8] = slug_renames[old_slug]
        renamed_count += 1

    # Apply name update (keyed on slug, post-rename)
    if r[8] in name_updates:
        r[0] = name_updates[r[8]]
        name_fix_count += 1

    # Set visibility: 0 if slug is in hide set, else 1
    if r[8] in slugs_to_hide or old_slug in slugs_to_hide:
        r.append(0)
        hidden_count += 1
    else:
        r.append(1)

# Apply party mismatches (look up by name + state + district)
for pm in party_mismatches:
    state   = pm["state"]
    dist    = pm["dist"]
    chamber = "S" if dist.lower() == "senate" else "H"
    dist_val = "" if chamber == "S" else (dist if dist.lower() != "at-large" else "AL")
    norm    = normalize_name(pm["name"])
    for i in name_to_slugs.get(norm, []):
        r = raw[i]
        if r[3] == state and r[4] == chamber:
            r[1] = pm["new_party"]
            party_fix_count += 1
            break

print(f"  Hidden:       {hidden_count}")
print(f"  Slug renames: {renamed_count}")
print(f"  Name fixes:   {name_fix_count}")
print(f"  Party fixes:  {party_fix_count}")

# ── Scrape scores for new candidates ──────────────────────────────────────────
print(f"\nFetching district pages for {len(new_candidates)} new candidates…")
print("  (101 unique pages, ~0.5s delay each ≈ ~50s)")

# Group new candidates by (state, dist)
by_page: dict[tuple, list[dict]] = defaultdict(list)
for c in new_candidates:
    by_page[(c["state"], c["dist"])].append(c)

# Fetch each page once, match candidates
slug_to_score: dict[str, int] = {}
slug_to_final: dict[str, dict] = {}  # full candidate data including score

total_pages = len(by_page)
for idx, ((state, dist), page_cands) in enumerate(sorted(by_page.items()), 1):
    url = build_url(state, dist)
    sys.stdout.write(f"\r  [{idx:3d}/{total_pages}] {state}/{dist:<12} ")
    sys.stdout.flush()

    swc_cands, err = fetch_district_candidates(url)
    if err:
        print(f"\n    WARN: {url} → {err}")
        for c in page_cands:
            c["score"] = -1
        time.sleep(REQUEST_DELAY)
        continue

    swc_by_slug = {c["slug"]: c for c in swc_cands}
    swc_by_norm = {c["norm"]: c for c in swc_cands}

    for c in page_cands:
        match = swc_by_slug.get(c["slug"]) or swc_by_norm.get(normalize_name(c["name"]))
        c["score"] = match["score"] if match else -1

    time.sleep(REQUEST_DELAY)

print(f"\n  Done fetching pages.")

# ── Determine chamber/district values for new candidates ─────────────────────
def dist_to_chamber_and_district(dist: str) -> tuple[str, str]:
    """Convert discrepancy DISTRICT label to (chamber, district) tuple."""
    if dist.lower() == "senate":
        return "S", ""
    if dist.lower() == "at-large":
        return "H", "AL"
    return "H", dist

# ── Build list of new records to add ──────────────────────────────────────────
new_records: list[list] = []
for c in new_candidates:
    chamber, dist_val = dist_to_chamber_and_district(c["dist"])
    rec = [
        c["name"],         # 0: name
        c["party"],        # 1: party
        c["score"],        # 2: score
        c["state"],        # 3: state
        chamber,           # 4: chamber
        dist_val,          # 5: district
        0,                 # 6: incumbent (unknown)
        0,                 # 7: wonState / senateRun (unknown)
        c["slug"],         # 8: slug
        None,              # 9: photo (set to null per instructions)
        1,                 # 10: visibility
    ]
    new_records.append(rec)

print(f"\nNew records to add: {len(new_records)}")

# ── Merge and write corrected JSON ────────────────────────────────────────────
corrected = raw + new_records
print(f"Total records after correction: {len(corrected)}")

with open(DATA_FILE, "w") as f:
    json.dump(corrected, f, separators=(",", ":"))

print(f"\nSaved corrected data to {DATA_FILE}")
print(f"\n{'='*50}")
print(f"Summary:")
print(f"  Original records:   {len(raw)}")
print(f"  Hidden (vis=0):     {hidden_count}")
print(f"  New candidates:     {len(new_records)}")
print(f"  Total records:      {len(corrected)}")
print(f"  Visible records:    {sum(1 for r in corrected if r[10] == 1)}")
print(f"  Slug renames:       {renamed_count}")
print(f"  Name fixes:         {name_fix_count}")
print(f"  Party fixes:        {party_fix_count}")
