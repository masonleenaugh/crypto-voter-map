#!/usr/bin/env python3
"""
verify_swc_data.py

Audits swc_embedded_data.json against two live SWC sources:
1. The public API (exact party / score / race placement by slug)
2. The public race pages (which candidates appear on each race page)

Outputs:
  - verify_discrepancies.csv
      Legacy page-vs-visible-JSON mismatch list, kept for compatibility.
  - verify_comparison_report.csv
      Categorized comparison findings with source-specific details.
  - verify_comparison_summary.md
      Human-readable summary with counts and representative examples.

Exit code 0 = no findings.
Exit code 1 = one or more findings.
"""

import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "swc_embedded_data.json"
LEGACY_OUTPUT_FILE = BASE_DIR / "verify_discrepancies.csv"
REPORT_OUTPUT_FILE = BASE_DIR / "verify_comparison_report.csv"
SUMMARY_OUTPUT_FILE = BASE_DIR / "verify_comparison_summary.md"
OVERRIDES_FILE = BASE_DIR / "repair_overrides.json"

SWC_BASE = "https://www.standwithcrypto.org/us/races/state"
API_URL = "https://www.standwithcrypto.org/api/us/public/partners/races/all-people"
REQUEST_DELAY = 0.5
TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.standwithcrypto.org/",
}
PARTY_MAP = {
    "US_REPUBLICAN": "R",
    "US_DEMOCRAT": "D",
    "US_INDEPENDENT": "I",
}
HOUSE_SEAT_LIMITS = {
    "AL": 7, "AK": 1, "AS": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8, "CT": 5,
    "DC": 1, "DE": 1, "FL": 28, "GA": 14, "GU": 1, "HI": 2, "IA": 4, "ID": 2,
    "IL": 17, "IN": 9, "KS": 4, "KY": 6, "LA": 6, "MA": 9, "MD": 8, "ME": 2,
    "MI": 13, "MN": 8, "MO": 8, "MP": 1, "MS": 4, "MT": 2, "NC": 14, "ND": 1,
    "NE": 3, "NH": 2, "NJ": 12, "NM": 3, "NV": 4, "NY": 28, "OH": 15, "OK": 5,
    "OR": 6, "PA": 17, "PR": 1, "RI": 2, "SC": 7, "SD": 1, "TN": 9, "TX": 38,
    "UT": 4, "VA": 11, "VI": 1, "VT": 1, "WA": 10, "WI": 8, "WV": 2, "WY": 1,
}
STANCE_TO_SCORE = {
    "strongly supports crypto": 100,
    "somewhat supports crypto": 75,
    "strongly supportive": 100,
    "somewhat supportive": 75,
    "supportive": 75,
    "neutral": 50,
    "mixed": 50,
    "somewhat against crypto": 25,
    "against": 25,
    "strongly against crypto": 0,
    "strongly against": 0,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    name = name.lower().strip()
    for suffix in [" jr.", " jr", " sr.", " sr", " ii", " iii", " iv", " v"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


def build_url(state: str, chamber: str, district: str) -> str:
    if chamber == "S":
        return f"{SWC_BASE}/{state}/senate"
    dist = district.strip()
    dist_url = dist.lower() if dist.lower() in ("at-large", "al") else dist
    if dist_url == "al":
        dist_url = "at-large"
    return f"{SWC_BASE}/{state}/district/{dist_url}"


def extract_stance_score(text: str) -> Optional[int]:
    lowered = text.lower()
    for stance, score in sorted(STANCE_TO_SCORE.items(), key=lambda item: -len(item[0])):
        if stance in lowered:
            return score
    return None


def score_bucket(score: Optional[int]) -> str:
    if score is None or score == -1:
        return "UNRATED"
    if score >= 90:
        return "A"
    if score >= 70:
        return "B"
    if score >= 50:
        return "C"
    if score >= 25:
        return "D"
    return "F"


def race_string(candidate: Optional[dict]) -> str:
    if not candidate:
        return ""
    state = candidate.get("state") or ""
    chamber = candidate.get("chamber") or ""
    district = candidate.get("district") or ""
    if chamber == "S":
        return f"{state} Senate".strip()
    if chamber == "H":
        label = district if district and district != "AL" else "At-Large"
        return f"{state} House {label}".strip()
    return state


def is_valid_house_district(state: str, district: str) -> bool:
    if district in ("AL", "At-Large"):
        return HOUSE_SEAT_LIMITS.get(state, 999) == 1
    try:
        number = int(district)
    except (TypeError, ValueError):
        return False
    if number < 1:
        return False
    return number <= HOUSE_SEAT_LIMITS.get(state, 0)


def fetch_swc_candidates(url: str) -> tuple[list[dict], str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return [], f"request error: {exc}"

    if resp.status_code == 500:
        return [], "HTTP 500 (page not found on SWC)"
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

        name = img["alt"].replace("Profile picture of ", "").strip()
        text = card.get_text(separator=" ", strip=True)
        party_match = re.search(r"\((R|D|I)\)", text)
        party = party_match.group(1) if party_match else "?"
        score = extract_stance_score(text)

        seen.add(slug)
        candidates.append({
            "name": name,
            "party": party,
            "score": score if score is not None else -1,
            "slug": slug,
            "norm": normalize_name(name),
        })

    return candidates, ""


def fetch_api_candidates() -> dict[str, dict]:
    print(f"Fetching {API_URL} …")
    resp = requests.get(API_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    people = payload["people"]
    print(f"  {len(people)} API people received")

    candidates: dict[str, dict] = {}
    for person in people:
        role = person.get("primaryRole") or {}
        category = role.get("roleCategory") or ""
        if category not in ("CONGRESS", "SENATE"):
            continue
        if role.get("primaryCountryCode", "US") != "US":
            continue

        slug = person.get("slug") or ""
        if not slug:
            continue

        first = person.get("firstNickname") or person.get("firstName") or ""
        last = person.get("lastName") or ""
        suffix = person.get("nameSuffix") or ""
        name = f"{first} {last}".strip()
        if suffix:
            name += f" {suffix}"

        score = person.get("manuallyOverriddenStanceScore")
        if score is None:
            score = person.get("computedStanceScore")
        if score is None:
            score = -1

        chamber = "S" if category == "SENATE" else "H"
        district = ""
        if chamber == "H":
            district = role.get("primaryDistrict") or "AL"

        candidates[slug] = {
            "slug": slug,
            "name": name,
            "norm": normalize_name(name),
            "party": PARTY_MAP.get(person.get("politicalAffiliationCategoryV2") or "", "?"),
            "score": score,
            "state": role.get("primaryState") or "",
            "chamber": chamber,
            "district": district,
            "visibility": "",
        }

    print(f"  {len(candidates)} API Congress/Senate candidates indexed")
    return candidates


def local_candidate_from_row(row: list) -> dict:
    return {
        "name": row[0],
        "party": row[1],
        "score": row[2],
        "state": row[3],
        "chamber": row[4],
        "district": row[5],
        "incumbent": row[6],
        "senate_run": row[7],
        "slug": row[8],
        "photo": row[9],
        "visibility": row[10],
        "norm": normalize_name(row[0]),
    }


def make_finding(
    category: str,
    *,
    subtype: str = "",
    slug: str = "",
    name: str = "",
    details: str = "",
    local: Optional[dict] = None,
    page: Optional[dict] = None,
    api: Optional[dict] = None,
) -> dict:
    return {
        "CATEGORY": category,
        "SUBTYPE": subtype,
        "SLUG": slug,
        "NAME": name,
        "DETAILS": details,
        "LOCAL_VISIBLE": "" if not local else local.get("visibility", ""),
        "LOCAL_RACE": race_string(local),
        "LOCAL_PARTY": "" if not local else local.get("party", ""),
        "LOCAL_SCORE": "" if not local else local.get("score", ""),
        "PAGE_RACE": race_string(page),
        "PAGE_PARTY": "" if not page else page.get("party", ""),
        "PAGE_SCORE": "" if not page else page.get("score", ""),
        "PAGE_SCORE_BUCKET": "" if not page else score_bucket(page.get("score")),
        "API_RACE": race_string(api),
        "API_PARTY": "" if not api else api.get("party", ""),
        "API_SCORE": "" if not api else api.get("score", ""),
        "API_SCORE_BUCKET": "" if not api else score_bucket(api.get("score")),
    }


def classify_move(local: dict, page: dict) -> str:
    if local.get("state") != page.get("state"):
        return "cross_state_shift"
    if local.get("chamber") != page.get("chamber"):
        return "same_state_chamber_shift"
    if local.get("district") != page.get("district"):
        return "same_state_district_shift"
    return "same_race_shift"


def load_overrides() -> dict:
    if not OVERRIDES_FILE.exists():
        return {
            "excluded_slugs": {},
            "race_overrides": {},
            "extra_candidates": [],
            "score_overrides": {},
        }
    with open(OVERRIDES_FILE) as f:
        data = json.load(f)
    data.setdefault("excluded_slugs", {})
    data.setdefault("race_overrides", {})
    data.setdefault("extra_candidates", [])
    data.setdefault("score_overrides", {})
    return data


def write_summary(
    legacy_rows: list[dict],
    findings: list[dict],
    api_findings: list[dict],
    invalid_district_findings: list[dict],
) -> None:
    by_category = Counter(f["CATEGORY"] for f in findings)
    by_subtype = Counter(f["SUBTYPE"] for f in findings if f["SUBTYPE"])

    lines = [
        "# SWC Verification Summary",
        "",
        f"- Legacy page discrepancy rows: {len(legacy_rows)}",
        f"- Categorized findings: {len(findings)}",
        f"- API-specific findings: {len(api_findings)}",
        f"- Invalid district findings: {len(invalid_district_findings)}",
        "",
        "## Findings by Category",
        "",
    ]

    if by_category:
        for category, count in by_category.most_common():
            lines.append(f"- `{category}`: {count}")
    else:
        lines.append("- None")

    lines.extend(["", "## Findings by Subtype", ""])
    if by_subtype:
        for subtype, count in by_subtype.most_common():
            lines.append(f"- `{subtype}`: {count}")
    else:
        lines.append("- None")

    move_examples = [f for f in findings if f["CATEGORY"] == "candidate_moved_race"][:10]
    stale_examples = [f for f in findings if f["CATEGORY"] == "stale_visible_candidate"][:10]
    missing_examples = [f for f in findings if f["CATEGORY"] == "true_missing_candidate"][:10]
    page_score_examples = [f for f in findings if f["CATEGORY"] == "page_rating_bucket_mismatch"][:10]

    def add_examples(title: str, rows: list[dict]) -> None:
        lines.extend(["", f"## {title}", ""])
        if not rows:
            lines.append("- None")
            return
        for row in rows:
            detail = row["DETAILS"] or row["SUBTYPE"]
            lines.append(
                f"- `{row['SLUG']}` {row['NAME']}: {detail} "
                f"(local `{row['LOCAL_RACE']}` | page `{row['PAGE_RACE']}` | api `{row['API_RACE']}`)"
            )

    add_examples("Representative Race Shifts", move_examples)
    add_examples("Representative Stale Visible Candidates", stale_examples)
    add_examples("Representative True Missing Candidates", missing_examples)
    add_examples("Representative Page Rating Bucket Mismatches", page_score_examples)

    SUMMARY_OUTPUT_FILE.write_text("\n".join(lines) + "\n")


# ── Load local data ────────────────────────────────────────────────────────────
print("Loading corrected swc_embedded_data.json…")
with open(DATA_FILE) as f:
    raw: list[list] = json.load(f)

overrides = load_overrides()
excluded_slugs = set(overrides["excluded_slugs"])
race_override_slugs = set(overrides["race_overrides"])
extra_candidate_slugs = {candidate["slug"] for candidate in overrides["extra_candidates"]}
score_override_slugs = set(overrides["score_overrides"])

local_all = [local_candidate_from_row(row) for row in raw]
visible = [candidate for candidate in local_all if candidate["visibility"] == 1]
hidden = [candidate for candidate in local_all if candidate["visibility"] == 0]

print(f"  Total: {len(local_all)}  Visible: {len(visible)}  Hidden: {len(hidden)}")

local_all_by_slug = {candidate["slug"]: candidate for candidate in local_all if candidate["slug"]}
hidden_by_slug = {candidate["slug"]: candidate for candidate in hidden if candidate["slug"]}

races: dict[tuple, list[dict]] = defaultdict(list)
for candidate in visible:
    key = (candidate["state"], candidate["chamber"], candidate["district"])
    races[key].append(candidate)


def sort_key(key: tuple[str, str, str]):
    state, chamber, district = key
    chamber_order = 0 if chamber == "S" else 1
    if district in ("AL", "At-Large", ""):
        dist_order = 0
    else:
        try:
            dist_order = int(district)
        except (TypeError, ValueError):
            dist_order = 9999
    return (state, chamber_order, dist_order, district)


sorted_races = sorted(races.keys(), key=sort_key)
print(f"  {len(sorted_races)} unique races across {len(set(key[0] for key in sorted_races))} states\n")

# ── Fetch API baseline ─────────────────────────────────────────────────────────
api_by_slug = fetch_api_candidates()
print("")

# ── Compare local JSON vs API (exact by slug) ─────────────────────────────────
findings: list[dict] = []
api_findings: list[dict] = []

all_slugs = sorted(set(local_all_by_slug) | set(api_by_slug))
for slug in all_slugs:
    local = local_all_by_slug.get(slug)
    api = api_by_slug.get(slug)

    if local and not api:
        if slug in excluded_slugs or slug in extra_candidate_slugs:
            continue
        row = make_finding(
            "local_only_candidate",
            slug=slug,
            name=local["name"],
            details="Candidate exists in local JSON but not in live API.",
            local=local,
        )
        findings.append(row)
        api_findings.append(row)
        continue

    if api and not local:
        row = make_finding(
            "api_only_candidate",
            slug=slug,
            name=api["name"],
            details="Candidate exists in live API but not in local JSON.",
            api=api,
        )
        findings.append(row)
        api_findings.append(row)
        continue

    if not local or not api:
        continue

    if local["party"] != api["party"]:
        row = make_finding(
            "api_party_mismatch",
            slug=slug,
            name=local["name"],
            details=f"Local party {local['party']} differs from API party {api['party']}.",
            local=local,
            api=api,
        )
        findings.append(row)
        api_findings.append(row)

    if local["score"] != api["score"]:
        if slug in score_override_slugs:
            continue
        row = make_finding(
            "api_score_mismatch",
            slug=slug,
            name=local["name"],
            details=f"Local score {local['score']} differs from API score {api['score']}.",
            local=local,
            api=api,
        )
        findings.append(row)
        api_findings.append(row)

    if (
        local["state"],
        local["chamber"],
        local["district"],
    ) != (
        api["state"],
        api["chamber"],
        api["district"],
    ):
        if slug in race_override_slugs:
            continue
        row = make_finding(
            "api_race_mismatch",
            slug=slug,
            name=local["name"],
            details="Local race placement differs from live API race placement.",
            local=local,
            api=api,
        )
        findings.append(row)
        api_findings.append(row)

# ── Audit obvious invalid local districts ─────────────────────────────────────
invalid_district_findings: list[dict] = []
for local in visible:
    if local["chamber"] == "H" and not is_valid_house_district(local["state"], local["district"]):
        row = make_finding(
            "invalid_house_district",
            subtype="local_visible",
            slug=local["slug"],
            name=local["name"],
            details=f"Visible local House district '{local['district']}' is invalid for {local['state']}.",
            local=local,
            api=api_by_slug.get(local["slug"]),
        )
        findings.append(row)
        invalid_district_findings.append(row)

# ── Compare live race pages vs visible JSON ───────────────────────────────────
legacy_rows: list[dict] = []
page_missing_local: dict[str, dict] = {}
page_extra_local: dict[str, dict] = {}
page_rows_with_non_slug_key: list[dict] = []

for i, key in enumerate(sorted_races):
    state, chamber, district = key
    local_candidates = races[key]
    url = build_url(state, chamber, district)
    dist_label = "Senate" if chamber == "S" else (district if district else "AL")

    sys.stdout.write(
        f"\r[{i+1:4d}/{len(sorted_races)}] {state} {'Senate' if chamber == 'S' else f'Dist {district}':<12}  "
    )
    sys.stdout.flush()

    swc_candidates, err = fetch_swc_candidates(url)
    page_race = {"state": state, "chamber": chamber, "district": district}

    if err:
        rated = [candidate for candidate in local_candidates if candidate["score"] != -1]
        if rated:
            names = ", ".join(candidate["name"] for candidate in rated)
            legacy_rows.append({
                "STATE": state,
                "DISTRICT": dist_label,
                "ISSUE": f"SWC page unavailable ({err}) — JSON has rated: {names}",
            })
        time.sleep(REQUEST_DELAY)
        continue

    json_by_slug = {candidate["slug"]: candidate for candidate in local_candidates if candidate["slug"]}
    swc_by_slug = {candidate["slug"]: candidate for candidate in swc_candidates if candidate["slug"]}
    json_by_norm = {candidate["norm"]: candidate for candidate in local_candidates}
    swc_by_norm = {candidate["norm"]: candidate for candidate in swc_candidates}

    for local in local_candidates:
        if local["slug"] and local["slug"] in swc_by_slug:
            continue
        if local["norm"] in swc_by_norm:
            swc_candidate = swc_by_norm[local["norm"]]
            if swc_candidate["slug"] != local["slug"]:
                legacy_rows.append({
                    "STATE": state,
                    "DISTRICT": dist_label,
                    "ISSUE": (
                        f"Slug mismatch for '{local['name']}': "
                        f"JSON slug='{local['slug']}' SWC slug='{swc_candidate['slug']}'"
                    ),
                })
                findings.append(make_finding(
                    "page_slug_mismatch",
                    slug=local["slug"],
                    name=local["name"],
                    details=(
                        f"Candidate name matches on page, but slug differs "
                        f"(local '{local['slug']}' vs page '{swc_candidate['slug']}')."
                    ),
                    local=local,
                    page={**page_race, **swc_candidate},
                    api=api_by_slug.get(local["slug"]) or api_by_slug.get(swc_candidate["slug"]),
                ))
            continue

        legacy_rows.append({
            "STATE": state,
            "DISTRICT": dist_label,
            "ISSUE": (
                f"Visible JSON candidate NOT on SWC: {local['name']} ({local['party']}) "
                f"[slug: {local['slug']}]"
            ),
        })
        entry = {
            "slug": local["slug"],
            "name": local["name"],
            "local": local,
            "page": page_race,
        }
        if local["slug"]:
            page_missing_local[local["slug"]] = entry
        else:
            page_rows_with_non_slug_key.append(entry)

    for page_candidate in swc_candidates:
        if page_candidate["slug"] in json_by_slug:
            local = json_by_slug[page_candidate["slug"]]

            if local["party"] != page_candidate["party"] and page_candidate["party"] != "?":
                legacy_rows.append({
                    "STATE": state,
                    "DISTRICT": dist_label,
                    "ISSUE": (
                        f"Party mismatch for '{local['name']}': "
                        f"JSON={local['party']} SWC={page_candidate['party']}"
                    ),
                })
                findings.append(make_finding(
                    "page_party_mismatch",
                    slug=local["slug"],
                    name=local["name"],
                    details=(
                        f"Local party {local['party']} differs from page party {page_candidate['party']}."
                    ),
                    local=local,
                    page={**page_race, **page_candidate},
                    api=api_by_slug.get(local["slug"]),
                ))

            if local["norm"] != page_candidate["norm"]:
                legacy_rows.append({
                    "STATE": state,
                    "DISTRICT": dist_label,
                    "ISSUE": (
                        f"Name mismatch (same slug '{local['slug']}'): "
                        f"JSON='{local['name']}' SWC='{page_candidate['name']}'"
                    ),
                })
                findings.append(make_finding(
                    "page_name_mismatch",
                    slug=local["slug"],
                    name=local["name"],
                    details=(
                        f"Local name '{local['name']}' differs from page name '{page_candidate['name']}'."
                    ),
                    local=local,
                    page={**page_race, **page_candidate},
                    api=api_by_slug.get(local["slug"]),
                ))

            if score_bucket(local["score"]) != score_bucket(page_candidate["score"]):
                findings.append(make_finding(
                    "page_rating_bucket_mismatch",
                    slug=local["slug"],
                    name=local["name"],
                    details=(
                        f"Local rating bucket {score_bucket(local['score'])} differs from "
                        f"page bucket {score_bucket(page_candidate['score'])}."
                    ),
                    local=local,
                    page={**page_race, **page_candidate},
                    api=api_by_slug.get(local["slug"]),
                ))

            continue

        if page_candidate["norm"] in json_by_norm:
            continue

        if page_candidate["slug"] in hidden_by_slug:
            if page_candidate["slug"] in excluded_slugs:
                continue
            findings.append(make_finding(
                "hidden_candidate_on_page",
                slug=page_candidate["slug"],
                name=page_candidate["name"],
                details="Candidate is hidden locally but appears on the live SWC page.",
                local=hidden_by_slug[page_candidate["slug"]],
                page={**page_race, **page_candidate},
                api=api_by_slug.get(page_candidate["slug"]),
            ))
            continue

        legacy_rows.append({
            "STATE": state,
            "DISTRICT": dist_label,
            "ISSUE": (
                f"SWC candidate missing from visible JSON: {page_candidate['name']} "
                f"({page_candidate['party']}) [slug: {page_candidate['slug']}]"
            ),
        })
        page_extra_local[page_candidate["slug"]] = {
            "slug": page_candidate["slug"],
            "name": page_candidate["name"],
            "page": {**page_race, **page_candidate},
            "local": local_all_by_slug.get(page_candidate["slug"]),
        }

    time.sleep(REQUEST_DELAY)

print(f"\n\nDone. {len(legacy_rows)} legacy page discrepancies found.")

# ── Pair page findings into race moves vs standalone issues ───────────────────
paired_slugs = sorted(set(page_missing_local) & set(page_extra_local))
for slug in paired_slugs:
    missing_entry = page_missing_local[slug]
    extra_entry = page_extra_local[slug]
    local = missing_entry["local"]
    page = extra_entry["page"]
    api = api_by_slug.get(slug)
    findings.append(make_finding(
        "candidate_moved_race",
        subtype=classify_move(local, page),
        slug=slug,
        name=local["name"],
        details="Candidate appears on a different live SWC race page than the visible local race.",
        local=local,
        page=page,
        api=api,
    ))

for slug, entry in sorted(page_missing_local.items()):
    if slug in paired_slugs:
        continue
    findings.append(make_finding(
        "stale_visible_candidate",
        slug=slug,
        name=entry["name"],
        details="Candidate is still visible locally but does not appear on the live SWC race page.",
        local=entry["local"],
        page=entry["page"],
        api=api_by_slug.get(slug),
    ))

for entry in page_rows_with_non_slug_key:
    findings.append(make_finding(
        "stale_visible_candidate",
        subtype="missing_slug",
        slug="",
        name=entry["name"],
        details="Candidate has no local slug and does not appear on the live SWC race page.",
        local=entry["local"],
        page=entry["page"],
    ))

for slug, entry in sorted(page_extra_local.items()):
    if slug in paired_slugs:
        continue
    if entry["local"]:
        findings.append(make_finding(
            "candidate_missing_from_visible_set",
            subtype="exists_locally_but_not_visible",
            slug=slug,
            name=entry["name"],
            details="Candidate appears on the page and exists locally, but not in the visible race set.",
            local=entry["local"],
            page=entry["page"],
            api=api_by_slug.get(slug),
        ))
    else:
        findings.append(make_finding(
            "true_missing_candidate",
            slug=slug,
            name=entry["name"],
            details="Candidate appears on the live SWC page but is absent from local JSON.",
            page=entry["page"],
            api=api_by_slug.get(slug),
        ))

# ── Write outputs ──────────────────────────────────────────────────────────────
with open(LEGACY_OUTPUT_FILE, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["STATE", "DISTRICT", "ISSUE"])
    writer.writeheader()
    writer.writerows(legacy_rows)

report_fieldnames = [
    "CATEGORY",
    "SUBTYPE",
    "SLUG",
    "NAME",
    "DETAILS",
    "LOCAL_VISIBLE",
    "LOCAL_RACE",
    "LOCAL_PARTY",
    "LOCAL_SCORE",
    "PAGE_RACE",
    "PAGE_PARTY",
    "PAGE_SCORE",
    "PAGE_SCORE_BUCKET",
    "API_RACE",
    "API_PARTY",
    "API_SCORE",
    "API_SCORE_BUCKET",
]
with open(REPORT_OUTPUT_FILE, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=report_fieldnames)
    writer.writeheader()
    writer.writerows(findings)

write_summary(legacy_rows, findings, api_findings, invalid_district_findings)

# ── Print summary ──────────────────────────────────────────────────────────────
by_category = Counter(finding["CATEGORY"] for finding in findings)
by_subtype = Counter(finding["SUBTYPE"] for finding in findings if finding["SUBTYPE"])

if legacy_rows or findings:
    print(f"\nLegacy mismatch file written to: {LEGACY_OUTPUT_FILE}")
    print(f"Comparison report written to:   {REPORT_OUTPUT_FILE}")
    print(f"Summary written to:             {SUMMARY_OUTPUT_FILE}")
    print("\nBreakdown by category:")
    for category, count in by_category.most_common():
        print(f"  {category}: {count}")
    if by_subtype:
        print("\nBreakdown by subtype:")
        for subtype, count in by_subtype.most_common():
            print(f"  {subtype}: {count}")
    sys.exit(1)

print("✓ Verification PASSED — local JSON matches the live SWC API and race pages.")
sys.exit(0)
