#!/usr/bin/env python3
"""
apply_targeted_repairs.py

Applies a narrow set of data repairs derived from verify_comparison_report.csv:
  - hide stale visible candidates
  - move candidates whose live SWC race page differs from local placement
  - exclude invalid federal House districts from the visible map
  - add true missing candidates that appear on live SWC race pages
  - align local scores with live SWC race-page ratings where they differ

Also writes repair_overrides.json so future sync_from_api.py runs preserve
these targeted fixes.
"""

import csv
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "swc_embedded_data.json"
REPORT_FILE = BASE_DIR / "verify_comparison_report.csv"
OVERRIDES_FILE = BASE_DIR / "repair_overrides.json"


def parse_race(race: str) -> tuple[str, str, str]:
    race = race.strip()
    if not race:
        raise ValueError("Race string is empty")

    parts = race.split()
    state = parts[0]
    if len(parts) == 2 and parts[1] == "Senate":
        return state, "S", ""
    if len(parts) >= 3 and parts[1] == "House":
        district = " ".join(parts[2:])
        if district == "At-Large":
            district = "AL"
        return state, "H", district
    raise ValueError(f"Unrecognized race string: {race}")


def load_rows() -> list[list]:
    with open(DATA_FILE) as f:
        return json.load(f)


def load_report() -> list[dict]:
    with open(REPORT_FILE, newline="") as f:
        return list(csv.DictReader(f))


def build_extra_candidate(row: dict) -> dict:
    state, chamber, district = parse_race(row["PAGE_RACE"])
    return {
        "name": row["NAME"],
        "party": row["PAGE_PARTY"] or "?",
        "score": int(row["PAGE_SCORE"] or -1),
        "state": state,
        "chamber": chamber,
        "district": district,
        "incumbent": 0,
        "senate_run": 0,
        "slug": row["SLUG"],
        "photo": None,
        "visibility": 1,
    }


def main() -> int:
    rows = load_rows()
    report_rows = load_report()
    by_slug = {row[8]: row for row in rows if row[8]}
    existing_overrides = {}
    if OVERRIDES_FILE.exists():
        with open(OVERRIDES_FILE) as f:
            existing_overrides = json.load(f)

    hidden_stale = 0
    moved = 0
    excluded = 0
    added = 0
    deferred = 0
    score_fixed = 0

    race_overrides: dict[str, dict] = dict(existing_overrides.get("race_overrides", {}))
    excluded_slugs: dict[str, dict] = dict(existing_overrides.get("excluded_slugs", {}))
    extra_candidates: list[dict] = list(existing_overrides.get("extra_candidates", []))
    score_overrides: dict[str, dict] = dict(existing_overrides.get("score_overrides", {}))

    for report in report_rows:
        category = report["CATEGORY"]
        slug = report["SLUG"]
        local = by_slug.get(slug)

        if category == "stale_visible_candidate":
            if local and local[10] != 0:
                local[10] = 0
                hidden_stale += 1
            continue

        if category == "candidate_moved_race":
            if not local:
                continue
            subtype = report["SUBTYPE"]
            state, chamber, district = parse_race(report["PAGE_RACE"])
            local[3] = state
            local[4] = chamber
            local[5] = district
            local[10] = 1
            moved += 1
            override = {
                "state": state,
                "chamber": chamber,
                "district": district,
            }
            # When a candidate is moved into a different chamber or state, their
            # source-race incumbency no longer applies to the displayed race.
            if subtype in ("same_state_chamber_shift", "cross_state_shift"):
                local[6] = 0
                local[7] = 0
                override["incumbent"] = 0
                override["senate_run"] = 0
            race_overrides[slug] = override
            continue

        if category == "candidate_missing_from_visible_set":
            deferred += 1
            continue

        if category == "invalid_house_district":
            if local and local[10] != 0:
                local[10] = 0
                excluded += 1
            excluded_slugs[slug] = {
                "reason": "invalid_federal_house_district",
                "local_race": report["LOCAL_RACE"],
            }
            continue

        if category == "true_missing_candidate":
            if slug in by_slug:
                continue
            candidate = build_extra_candidate(report)
            rows.append([
                candidate["name"],
                candidate["party"],
                candidate["score"],
                candidate["state"],
                candidate["chamber"],
                candidate["district"],
                candidate["incumbent"],
                candidate["senate_run"],
                candidate["slug"],
                candidate["photo"],
                candidate["visibility"],
            ])
            by_slug[slug] = rows[-1]
            extra_candidates.append(candidate)
            added += 1
            continue

        if category == "page_rating_bucket_mismatch":
            if not local:
                continue
            new_score = int(report["PAGE_SCORE"] or -1)
            local[2] = new_score
            score_overrides[slug] = {
                "score": new_score,
                "reason": "match_live_swc_race_page",
                "api_score": int(report["API_SCORE"] or -1),
                "page_score": new_score,
            }
            score_fixed += 1

    overrides = {
        "version": 1,
        "generated_from": REPORT_FILE.name,
        "race_overrides": race_overrides,
        "excluded_slugs": excluded_slugs,
        "extra_candidates": extra_candidates,
        "score_overrides": score_overrides,
    }

    with open(DATA_FILE, "w") as f:
        json.dump(rows, f, separators=(",", ":"))

    with open(OVERRIDES_FILE, "w") as f:
        json.dump(overrides, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"Updated {DATA_FILE}")
    print(f"Wrote {OVERRIDES_FILE}")
    print(f"  Hidden stale candidates: {hidden_stale}")
    print(f"  Moved candidates:        {moved}")
    print(f"  Excluded invalid races:  {excluded}")
    print(f"  Added missing records:   {added}")
    print(f"  Score overrides applied: {score_fixed}")
    print(f"  Deferred ambiguous rows: {deferred}")
    print(f"  Race overrides saved:    {len(race_overrides)}")
    print(f"  Excluded slugs saved:    {len(excluded_slugs)}")
    print(f"  Extra candidates saved:  {len(extra_candidates)}")
    print(f"  Score overrides saved:   {len(score_overrides)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
