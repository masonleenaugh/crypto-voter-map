#!/usr/bin/env python3
"""Round-2 patch: normalize 'AL' district → 'At-Large' and hide two stale candidates."""
import json
from pathlib import Path

DATA_FILE = Path(__file__).parent / "swc_embedded_data.json"

with open(DATA_FILE) as f:
    data = json.load(f)

hide_slugs = {"charles---summers", "jacob---anders"}
normalize_count = 0
hidden_count = 0

for r in data:
    # Normalize at-large district label
    if r[5] == "AL" and r[4] == "H":
        r[5] = "At-Large"
        normalize_count += 1
    # Hide stale candidates still marked visible
    if r[8] in hide_slugs and r[10] == 1:
        r[10] = 0
        hidden_count += 1

print(f"Normalized 'AL' → 'At-Large': {normalize_count} records")
print(f"Newly hidden: {hidden_count} records")

with open(DATA_FILE, "w") as f:
    json.dump(data, f, separators=(",", ":"))

print("Saved.")
