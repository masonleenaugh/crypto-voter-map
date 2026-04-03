#!/usr/bin/env python3
"""
sync_from_api.py
Rebuilds swc_embedded_data.json directly from the SWC public API.

No scraping needed. The API endpoint returns all candidates with scores,
party, state, district, slug, and photo in a single JSON payload.

Candidates with null scores are included and stored as -1 (shown on the
map as "Not Yet Rated" in gray).

Usage:
    python3 sync_from_api.py [--dry-run]
"""

import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────────
API_URL  = "https://www.standwithcrypto.org/api/us/public/partners/races/all-people"
OUT_FILE = Path(__file__).parent / "swc_embedded_data.json"
OVERRIDES_FILE = Path(__file__).parent / "repair_overrides.json"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.standwithcrypto.org/",
}

PARTY_MAP = {
    "US_REPUBLICAN": "R",
    "US_DEMOCRAT":   "D",
    "US_INDEPENDENT": "I",
}

DRY_RUN = "--dry-run" in sys.argv


def fetch_api() -> list[dict]:
    print(f"Fetching {API_URL} …")
    req = urllib.request.Request(API_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    people = data["people"]
    print(f"  {len(people)} candidates received")
    return people


def load_existing_visibility() -> dict[str, int]:
    if not OUT_FILE.exists():
        return {}
    with open(OUT_FILE) as f:
        rows = json.load(f)
    return {
        row[8]: row[10]
        for row in rows
        if len(row) >= 11 and row[8]
    }


def load_overrides() -> dict:
    if not OVERRIDES_FILE.exists():
        return {
            "race_overrides": {},
            "excluded_slugs": {},
            "extra_candidates": [],
            "score_overrides": {},
        }
    with open(OVERRIDES_FILE) as f:
        data = json.load(f)
    data.setdefault("race_overrides", {})
    data.setdefault("excluded_slugs", {})
    data.setdefault("extra_candidates", [])
    data.setdefault("score_overrides", {})
    return data


def build_record(p: dict):
    """Convert one API person object to compact array, or None to skip."""
    role = p.get("primaryRole") or {}
    category = role.get("roleCategory", "")

    # Only US Congress / Senate / Governor (only Congress+Senate for now)
    if category not in ("CONGRESS", "SENATE"):
        return None

    country = role.get("primaryCountryCode", "US")
    if country != "US":
        return None

    # Name
    first    = p.get("firstNickname") or p.get("firstName") or ""
    last     = p.get("lastName") or ""
    suffix   = p.get("nameSuffix") or ""
    name     = f"{first} {last}".strip()
    if suffix:
        name += f" {suffix}"

    # Party
    party = PARTY_MAP.get(p.get("politicalAffiliationCategoryV2") or "", "?")

    # Score: manual override takes priority; null → -1
    score = p.get("manuallyOverriddenStanceScore")
    if score is None:
        score = p.get("computedStanceScore")
    if score is None:
        score = -1

    # State / chamber / district
    state    = role.get("primaryState") or ""
    chamber  = "S" if category == "SENATE" else "H"
    district = ""
    if chamber == "H":
        d = role.get("primaryDistrict") or ""
        district = d if d else "AL"

    # Incumbent flag
    incumbent = 1 if role.get("status") == "HELD" else 0

    # Senate-run flag: 1 if this House HELD member also has a RUNNING_FOR SENATE role
    senate_run = 0
    if chamber == "H" and incumbent:
        for r in p.get("roles") or []:
            if (r.get("roleCategory") == "SENATE"
                    and r.get("status") == "RUNNING_FOR"):
                senate_run = r.get("primaryState") or state
                break

    slug  = p.get("slug") or ""
    photo = p.get("profilePictureUrl") or None

    return [name, party, score, state, chamber, district, incumbent, senate_run, slug, photo, 1]


def main():
    existing_visibility = load_existing_visibility()
    overrides = load_overrides()
    people  = fetch_api()
    records = []
    skipped = 0

    for p in people:
        rec = build_record(p)
        if rec is None:
            skipped += 1
        else:
            slug = rec[8]
            if slug in existing_visibility:
                rec[10] = existing_visibility[slug]

            race_override = overrides["race_overrides"].get(slug)
            if race_override:
                rec[3] = race_override["state"]
                rec[4] = race_override["chamber"]
                rec[5] = race_override["district"]
                if "incumbent" in race_override:
                    rec[6] = race_override["incumbent"]
                if "senate_run" in race_override:
                    rec[7] = race_override["senate_run"]

            if slug in overrides["excluded_slugs"]:
                rec[10] = 0

            score_override = overrides["score_overrides"].get(slug)
            if score_override:
                rec[2] = score_override["score"]

            records.append(rec)

    existing_slugs = {r[8] for r in records if r[8]}
    added_extras = 0
    for candidate in overrides["extra_candidates"]:
        slug = candidate["slug"]
        if slug in existing_slugs:
            continue
        visibility = existing_visibility.get(slug, candidate.get("visibility", 1))
        records.append([
            candidate["name"],
            candidate["party"],
            candidate["score"],
            candidate["state"],
            candidate["chamber"],
            candidate["district"],
            candidate.get("incumbent", 0),
            candidate.get("senate_run", 0),
            slug,
            candidate.get("photo"),
            visibility,
        ])
        existing_slugs.add(slug)
        added_extras += 1

    # Stats
    total     = len(records)
    rated     = sum(1 for r in records if r[2] != -1)
    unrated   = total - rated
    held      = sum(1 for r in records if r[6] == 1)
    running   = total - held
    senate    = sum(1 for r in records if r[4] == "S")
    house     = total - senate
    parties   = Counter(r[1] for r in records)

    print(f"\n{'='*50}")
    print(f"Records to write: {total}  (skipped {skipped} non-Congress)")
    print(f"  Incumbents (HELD):      {held}")
    print(f"  Challengers (RUNNING):  {running}")
    print(f"  Senate:                 {senate}")
    print(f"  House:                  {house}")
    print(f"  Rated (has score):      {rated}")
    print(f"  Unrated (score = -1):   {unrated}")
    print(f"  Party breakdown:        R={parties['R']} D={parties['D']} I={parties['I']} ?={parties['?']}")
    print(f"  Preserved visibility:   {len(existing_visibility)} slugs")
    print(f"  Race overrides:         {len(overrides['race_overrides'])}")
    print(f"  Excluded slugs:         {len(overrides['excluded_slugs'])}")
    print(f"  Score overrides:        {len(overrides['score_overrides'])}")
    print(f"  Added extra records:    {added_extras}")

    if DRY_RUN:
        print("\n[DRY RUN] Not writing file.")
        return

    with open(OUT_FILE, "w") as f:
        json.dump(records, f, separators=(",", ":"))

    size_kb = OUT_FILE.stat().st_size / 1024
    print(f"\nSaved → {OUT_FILE}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
