"""
05_tile_feed.py — emit the LPV metric's PHI-free scorecard tile feed.

Writes metrics/lpv/output/final/tile_feed_lpv.json (schema v1 — the same shape every metric vertical
emits), from this metric's own rollups (02_patient_day_status / 02_intervals / 02d_severity). The
bundle scorecard (scorecard/build_scorecard.py) is a pure combiner that collects this feed alongside
the other metrics' feeds and renders them — LPV is just another metric.

Headline = tidal-volume adherence at <= 8 mL/kg PBW; 3 segments = Plateau <= 30, dP <= 15, Vt <= 8 in
severe respiratory failure. Also carries a 'ui' block (weeks / months / units + labels) the combiner
uses for its global Week/Month/Unit selectors and sparkline axes.

Run (after 01-04):  .venv/bin/python metrics/lpv/code/05_tile_feed.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]            # bundle root (shared config.json)
_METRIC_ROOT = Path(__file__).resolve().parents[1]    # metrics/lpv (per-metric outputs)
CFG = json.loads((ROOT / "config.json").read_text())
OUT_DIR = Path(CFG.get("output_path", _METRIC_ROOT / "output"))
FINAL_DIR = OUT_DIR / "final"
FINAL_DIR.mkdir(parents=True, exist_ok=True)

# ---- Named parameters (LPV tile) ----
SCORECARD_VT_CUTOFF = 8.0   # headline Vt/kg cutoff for the scorecard tile
LPV_GOAL = 0.90             # target line on the LPV tile
ADHERENCE_FRACTION = 0.80
MIN_ASSESSABLE_MIN = 60

UNIT_ORDER_REST = ["medical_icu", "mixed_cardiothoracic_icu", "surgical_icu",
                   "mixed_neuro_icu", "general_icu", "burn_icu"]

# ----------------------------------------------------------------------------
# 1. Load + per-(hosp, day) Vt<=8 recompute (status file is default-6)
# ----------------------------------------------------------------------------
print("[lpv-feed] Loading + computing Vt<=8 per patient-day ...")
status = pd.read_parquet(OUT_DIR / "02_patient_day_status.parquet")
status["hospitalization_id"] = status["hospitalization_id"].astype(str)
status["calendar_day"] = pd.to_datetime(status["calendar_day"]).dt.date

iv = pd.read_parquet(OUT_DIR / "02_intervals.parquet")
iv["hospitalization_id"] = iv["hospitalization_id"].astype(str)
iv["calendar_day"] = pd.to_datetime(iv["calendar_day"]).dt.date
key = ["hospitalization_id", "calendar_day"]
gk = [iv["hospitalization_id"], iv["calendar_day"]]
vt_present = iv["vt_per_pbw"].notna()
vt_assess = iv["duration_min"].where(vt_present, 0.0).groupby(gk).sum()
vt8_in = iv["duration_min"].where(vt_present & (iv["vt_per_pbw"] <= SCORECARD_VT_CUTOFF), 0.0).groupby(gk).sum()
vt = pd.DataFrame({"vt_assess_min": vt_assess, "vt8_in_min": vt8_in}).reset_index()
vt.columns = key + ["vt_assess_min", "vt8_in_min"]

sev = pd.read_parquet(OUT_DIR / "02d_severity.parquet")[["hospitalization_id", "calendar_day", "severity"]]
sev["hospitalization_id"] = sev["hospitalization_id"].astype(str)
sev["calendar_day"] = pd.to_datetime(sev["calendar_day"]).dt.date

day = status[["hospitalization_id", "calendar_day", "assigned_unit", "total_imv_minutes",
              "plat_status", "dp_status"]].merge(vt, on=key, how="left").merge(sev, on=key, how="left")
day[["vt_assess_min", "vt8_in_min"]] = day[["vt_assess_min", "vt8_in_min"]].fillna(0.0)
day["severity"] = day["severity"].fillna("unknown")

day["vt8_ass"] = day["vt_assess_min"] >= MIN_ASSESSABLE_MIN
day["vt8_ad"] = day["vt8_ass"] & ((day["vt8_in_min"] / day["vt_assess_min"].where(day["vt_assess_min"] > 0)) >= ADHERENCE_FRACTION)
day["plat_ass"] = day["plat_status"].isin(["adherent", "non_adherent"])
day["plat_ad"] = day["plat_status"] == "adherent"
day["dp_ass"] = day["dp_status"].isin(["adherent", "non_adherent"])
day["dp_ad"] = day["dp_status"] == "adherent"

_dt = pd.to_datetime(day["calendar_day"])
isoc = _dt.dt.isocalendar()
day["week"] = isoc["year"].astype(str) + "-W" + isoc["week"].astype(int).map("{:02d}".format)
day["month"] = _dt.dt.strftime("%Y-%m")

# ----------------------------------------------------------------------------
# 2. Roll up to (unit, period) cells and assemble the v1 feed
# ----------------------------------------------------------------------------
print("[lpv-feed] Building the LPV tile feed (per unit x all/month/week) ...")
weeks = sorted(day["week"].unique().tolist())
months = sorted(day["month"].unique().tolist())
units = ["__ALL__"] + [u for u in UNIT_ORDER_REST if u in set(day["assigned_unit"])]
rep = day.groupby("week")["calendar_day"].min()
week_label = {w: f"Week {w[-2:].lstrip('0')} · {pd.Timestamp(rep[w]).strftime('%b %Y')}" for w in weeks}
month_label = {m: pd.Timestamp(m + "-01").strftime("%b %Y") for m in months}


def cell_counts(df: pd.DataFrame) -> dict:
    """(numerator, denominator) per measure + denominator-line counts, for one (unit, period) slice."""
    sevdf = df[df["severity"] == "severe"]
    return {
        "vt8": (int(df["vt8_ad"].sum()), int(df["vt8_ass"].sum())),
        "plat": (int(df["plat_ad"].sum()), int(df["plat_ass"].sum())),
        "dp": (int(df["dp_ad"].sum()), int(df["dp_ass"].sum())),
        "vt8sev": (int(sevdf["vt8_ad"].sum()), int(sevdf["vt8_ass"].sum())),
        "n": int(len(df)),
        "hrs": round(float(df["total_imv_minutes"].sum()) / 60.0),
    }


raw = {u: {} for u in units}
raw["__ALL__"]["all"] = cell_counts(day)
for u, gu in day.groupby("assigned_unit"):
    if u in raw:
        raw[u]["all"] = cell_counts(gu)
for bucket in ("week", "month"):
    for b, gb in day.groupby(bucket):
        raw["__ALL__"][b] = cell_counts(gb)
        for u, gu in gb.groupby("assigned_unit"):
            if u in raw:
                raw[u][b] = cell_counts(gu)


def headline_cells() -> dict:
    out = {}
    for u, periods in raw.items():
        out[u] = {pk: {"num": c["vt8"][0], "den": c["vt8"][1], "n": c["n"], "hrs": c["hrs"]}
                  for pk, c in periods.items()}
    return out


def measure_cells(mkey: str) -> dict:
    out = {}
    for u, periods in raw.items():
        out[u] = {pk: {"num": c[mkey][0], "den": c[mkey][1]} for pk, c in periods.items()}
    return out


cut = f"{SCORECARD_VT_CUTOFF:g}"
lpv_feed = {
    "schema_version": 1,
    "metric_id": "lpv",
    "title": "LPV Adherence",
    "subtitle": f"Tidal volume ≤ {cut} mL/kg PBW",
    "icon": "lpv",
    "detail_href": "lpv_dashboard.html",
    "goal": LPV_GOAL,
    "note": None,
    "grain": {"units": units, "periods": ["all", "month", "week"]},
    "headline": {"label": "adherent", "den_label": "of assessable", "n_unit": "patient-days",
                 "cells": headline_cells()},
    "segments": [
        {"key": "plat", "label": "Plateau ≤ 30", "cells": measure_cells("plat")},
        {"key": "dp", "label": "∆P ≤ 15", "cells": measure_cells("dp")},
        {"key": "vt8sev", "label": f"Vt ≤ {cut} · severe", "cells": measure_cells("vt8sev")},
    ],
    # UI metadata the combiner uses for its global Week/Month/Unit selectors + sparkline axes.
    "ui": {"weeks": weeks, "week_label": week_label,
           "months": months, "month_label": month_label, "units": units},
}

out_path = FINAL_DIR / "tile_feed_lpv.json"
out_path.write_text(json.dumps(lpv_feed, indent=2, allow_nan=False))
print(f"  wrote {out_path}")
hc = lpv_feed["headline"]["cells"]["__ALL__"]["all"]
print(f"  LPV headline Vt<={cut}: {hc['num']}/{hc['den']} = {hc['num'] / hc['den'] * 100:.1f}% (all units / all time)")
print("Done.")
