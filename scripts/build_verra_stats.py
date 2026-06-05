"""
TerraLedger — build real per-type credit-volume stats from a Verra pipeline CSV.

The Verra pipeline export has real project types, AFOLU activities and
"Estimated Annual Emission Reductions" (tCO2/yr) but NO project area, so it can't
feed the tCO2/hectare model directly. What it CAN give is a real-world
distribution of annual credit volume per project type — used by the anomaly
module as a complementary "volume realism" signal grounded in 1400+ real projects.

Output: data/verra_type_volume_stats.json
    { "<our_type>": {"count": N, "log_mean": x, "log_std": y, "median": m}, ... }

Run:  python scripts/build_verra_stats.py ~/Downloads/pipeline.csv
"""

import sys, json, math
from pathlib import Path
import numpy as np
import pandas as pd

OUT = Path("data/verra_type_volume_stats.json")


def map_type(project_type: str, afolu: str) -> str:
    """Map verbose Verra type + AFOLU activity to our 5 categories."""
    pt = str(project_type or "").lower()
    af = str(afolu or "").upper()
    if af == "NAN": af = ""
    if pt == "nan": pt = ""
    if "agriculture forestry" in pt or af:
        if "ALM" in af:                       # Agricultural Land Management
            return "agriculture"
        if any(a in af for a in ("ARR", "REDD", "IFM", "WRC")):
            return "forest"                   # afforestation / avoided defor. / forest mgmt
        return "forest"
    if "renewable" in pt or "energy industries" in pt:
        return "renewable"
    if "livestock" in pt or "manure" in pt or "waste handling" in pt:
        return "methane"
    return "other"


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/build_verra_stats.py <pipeline.csv>")
        sys.exit(1)
    df = pd.read_csv(sys.argv[1], low_memory=False)
    df["ER"] = pd.to_numeric(
        df["Estimated Annual Emission Reductions"].astype(str).str.replace(",", ""),
        errors="coerce",
    )
    df = df[df["ER"].notna() & (df["ER"] > 0)]
    df["our_type"] = df.apply(lambda r: map_type(r.get("Project Type"), r.get("AFOLU Activities")), axis=1)

    stats = {}
    for t, grp in df.groupby("our_type"):
        er = grp["ER"].values
        log_er = np.log(er)
        stats[t] = {
            "count":    int(len(er)),
            "median":   float(np.median(er)),
            "log_mean": float(np.mean(log_er)),
            "log_std":  float(max(np.std(log_er), 0.5)),
            "p95":      float(np.percentile(er, 95)),
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(stats, indent=2))
    print(f"Wrote {OUT} from {len(df)} real Verra projects:")
    for t, s in stats.items():
        print(f"  {t:<12} n={s['count']:<4} median={s['median']:,.0f} tCO2/yr  p95={s['p95']:,.0f}")


if __name__ == "__main__":
    main()
