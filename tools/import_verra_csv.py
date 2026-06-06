"""
TerraLedger — Verra / registry CSV importer

The anomaly model trains on real registry data when store/verra_projects.csv exists.
Verra's live registry can't be scraped reliably (JS app + bot protection), so you
download the CSV once via the browser, then this script normalises it to the exact
columns the trainer expects.

WHERE TO GET A REAL CSV (pick one, ~2 min):
  1. Verra registry:
       https://registry.verra.org/app/search/VCS  →  filter  →  "Download" (CSV)
  2. Berkeley Carbon Trading Project "Voluntary Registry Offsets Database" (free Excel,
     the standard research dataset; open in Excel → Save As CSV):
       https://gspp.berkeley.edu/faculty-and-impact/centers/cepp/projects/berkeley-carbon-trading-project

USAGE:
    python scripts/import_verra_csv.py <downloaded.csv>
    rm -f store/isolation_forest.pkl     # clear cached model
    # restart backend → it trains on the real data

The importer fuzzy-matches column names, so it works with either source's export.
"""

import sys
import re
from pathlib import Path

import pandas as pd

OUT_PATH = Path("store/verra_projects.csv")

# Column the trainer reads → list of possible source header names (lowercased, fuzzy)
COLUMN_MAP = {
    "Total VCUs Issued": [
        "total vcus issued", "vcus issued", "credits issued", "total credits issued",
        "estimated annual emission reductions", "annual emission reductions",
        "total credits", "issued", "quantity issued",
    ],
    "Project Area (ha)": [
        "project area (ha)", "project area", "area (ha)", "area ha", "hectares",
        "total project area", "size (ha)",
    ],
    "Project Type": [
        "project type", "type", "category", "scope", "afolu activities",
        "methodology category", "sectoral scope",
    ],
    "Vintage Start": [
        "vintage start", "vintage", "crediting period start", "crediting period start date",
        "project start date", "start date", "first vintage",
    ],
}


def _find_col(df_cols_lower, candidates):
    """Return the original column whose lowercased name best matches a candidate."""
    for cand in candidates:
        for orig_lower, orig in df_cols_lower.items():
            if cand == orig_lower:
                return orig
    # loose contains-match fallback
    for cand in candidates:
        for orig_lower, orig in df_cols_lower.items():
            if cand in orig_lower or orig_lower in cand:
                return orig
    return None


def _num(x):
    """Parse a number out of a messy cell ('1,234,567', '12 ha', '—')."""
    if pd.isna(x):
        return None
    s = re.sub(r"[^\d.]", "", str(x))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_verra_csv.py <downloaded.csv>")
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"File not found: {src}")
        sys.exit(1)

    # Read (try CSV, then Excel)
    try:
        df = pd.read_csv(src, on_bad_lines="skip", low_memory=False)
    except Exception:
        df = pd.read_excel(src)

    cols_lower = {c.lower().strip(): c for c in df.columns}
    resolved = {target: _find_col(cols_lower, cands) for target, cands in COLUMN_MAP.items()}

    print("Column mapping detected:")
    for target, found in resolved.items():
        print(f"  {target:<22} <- {found or 'NOT FOUND'}")

    if not resolved["Total VCUs Issued"] or not resolved["Project Area (ha)"]:
        print("\n✗ Could not find credits/area columns. Open the CSV and check headers,")
        print("  or add the header name to COLUMN_MAP in this script.")
        sys.exit(1)

    out_rows = []
    for _, row in df.iterrows():
        vcus = _num(row.get(resolved["Total VCUs Issued"]))
        area = _num(row.get(resolved["Project Area (ha)"])) if resolved["Project Area (ha)"] else None
        if not vcus or not area or area < 1:
            continue
        ptype   = str(row.get(resolved["Project Type"], "other")) if resolved["Project Type"] else "other"
        vintage = row.get(resolved["Vintage Start"], 2015) if resolved["Vintage Start"] else 2015
        out_rows.append({
            "Total VCUs Issued": int(vcus),
            "Project Area (ha)": int(area),
            "Project Type":      ptype,
            "Vintage Start":     vintage,
        })

    if len(out_rows) < 50:
        print(f"\n⚠ Only {len(out_rows)} usable rows — trainer needs ≥50 to use real data.")
        print("  It will fall back to the literature-grounded synthetic prior.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(out_rows).to_csv(OUT_PATH, index=False)
    print(f"\n✓ Wrote {len(out_rows)} real projects → {OUT_PATH}")
    print("Next: rm -f store/isolation_forest.pkl  &&  restart backend (trains on real data)")


if __name__ == "__main__":
    main()
