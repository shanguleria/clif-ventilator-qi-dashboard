"""Shared respiratory_support substrate (Phase 1, Level 0).

`load_clean(...)` is the single loader/cleaner for the raw `respiratory_support` table, used by every
vertical (lpv/proning/sat/sbt). It returns **native rows** (no forward-fill, no hourly scaffold) that
have been:

  1. FiO2 unit-detected (percent → fraction) — run BEFORE the range clip, else percent-encoded FiO2
     (e.g. 40) is nulled by the [0.21, 1.0] range instead of rescaled to 0.40.
  2. Outlier-clipped to the CLIF-spec ranges via clifpy's `apply_outlier_handling`
     (`clifpy/schemas/outlier_config.yaml`); a fallback range table is used only if that helper is
     unavailable.
  3. device_category / mode_category normalized (strip + lowercase) so category matches are
     case-insensitive across sites.
  4. recorded_dttm coerced tz-aware to the site timezone; hospitalization_id coerced to str.

The filled + hourly-scaffold *waterfall* that proning/sat/sbt consume is a separate Level-1 layer built
ON TOP of this (added in a later commit); LPV never uses the waterfall (its scaffold rows would
double-count LPV's minute-weighted intervals).

Cleaning happens here, PRE-waterfall (a change from the old post-waterfall `_normalize_waterfall`): a
forward-fill then carries the last *valid* value instead of propagating an outlier forward and nulling it
afterwards. Downstream callers do their own dropna / sort / carry-forward.
"""
from __future__ import annotations

import pandas as pd

# clifpy CLIF-spec respiratory_support ranges (mirror of schemas/outlier_config.yaml); used ONLY when
# clifpy's apply_outlier_handling can't be imported. Keep in sync with the spec.
FALLBACK_RANGES = {
    "tidal_volume_obs": (100.0, 3000.0), "tidal_volume_set": (100.0, 3000.0),
    "plateau_pressure_obs": (0.0, 100.0), "peep_obs": (0.0, 50.0),
    "peep_set": (0.0, 30.0), "fio2_set": (0.21, 1.0),
    "pressure_support_set": (-50.0, 50.0),
}

# Superset of columns any vertical needs; callers may pass a narrower list.
DEFAULT_COLUMNS = [
    "hospitalization_id", "recorded_dttm", "device_category", "mode_category", "tracheostomy",
    "tidal_volume_obs", "tidal_volume_set", "plateau_pressure_obs", "peep_obs", "peep_set",
    "fio2_set", "pressure_support_set",
]

_FIO2_PERCENT_P95 = 1.5   # p95 above this ⇒ FiO2 charted as percent ⇒ divide by 100


# ---------------------------------------------------------------------------
# pure, unit-testable steps
# ---------------------------------------------------------------------------
def fio2_unit_detect(df: pd.DataFrame) -> str | None:
    """If fio2_set looks percent-encoded (p95 > 1.5), divide it by 100 IN PLACE. Returns a note (or
    None if there's no fio2_set / it's empty)."""
    if "fio2_set" not in df.columns:
        return None
    f = pd.to_numeric(df["fio2_set"], errors="coerce")
    if not f.notna().any():
        return None
    p95 = f.quantile(0.95)
    if p95 is not None and p95 > _FIO2_PERCENT_P95:
        df["fio2_set"] = f / 100.0
        return f"percent (p95={p95:.2f}, max={f.max():.1f}) → /100"
    return f"fraction (p95={p95:.3f}, max={f.max():.3f})"


def fallback_clip(df: pd.DataFrame) -> None:
    """Clip out-of-range values to NaN using FALLBACK_RANGES, IN PLACE. Only used when clifpy's
    apply_outlier_handling is unavailable; clips only columns that are present."""
    for col, (lo, hi) in FALLBACK_RANGES.items():
        if col in df.columns:
            v = pd.to_numeric(df[col], errors="coerce")
            df[col] = v.where((v >= lo) & (v <= hi))


def normalize_frame(df: pd.DataFrame, timezone: str) -> pd.DataFrame:
    """device/mode → strip+lower; recorded_dttm → tz-aware(timezone); hospitalization_id → str.
    Returns the same frame (mutated + returned for chaining)."""
    for col in ("device_category", "mode_category"):
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip().str.lower()
    if "hospitalization_id" in df.columns:
        df["hospitalization_id"] = df["hospitalization_id"].astype(str)
    if "recorded_dttm" in df.columns:
        s = pd.to_datetime(df["recorded_dttm"])
        df["recorded_dttm"] = (s.dt.tz_localize(timezone) if s.dt.tz is None
                               else s.dt.tz_convert(timezone))
    return df


# ---------------------------------------------------------------------------
# the loader
# ---------------------------------------------------------------------------
def _apply_outliers(rs_tbl) -> str:
    """CLIF-spec clip on the clifpy table object (preferred), else FALLBACK_RANGES on its df."""
    try:
        from clifpy.utils.outlier_handler import apply_outlier_handling
        apply_outlier_handling(rs_tbl)
        return "clifpy apply_outlier_handling"
    except Exception as e:  # pragma: no cover - exercised only when clifpy helper is absent
        fallback_clip(rs_tbl.df)
        return f"fallback ranges ({type(e).__name__})"


def load_clean(data_dir, filetype, timezone, hosp_ids, columns=None, *,
               extra_filters=None, fio2_unit_detect_on=True, verbose=True) -> pd.DataFrame:
    """Load respiratory_support filtered to `hosp_ids` (+ optional `extra_filters`, e.g.
    {"device_category": ["IMV","imv"]}) and return cleaned NATIVE rows. See module docstring for the
    order of operations (unit-detect BEFORE clip is load-bearing)."""
    from clifpy.tables import RespiratorySupport

    cols = list(columns) if columns is not None else list(DEFAULT_COLUMNS)
    filters = {"hospitalization_id": list(hosp_ids)}
    if extra_filters:
        filters.update(extra_filters)

    rs_tbl = RespiratorySupport.from_file(
        data_dir, filetype=filetype, timezone=timezone, filters=filters, columns=cols,
    )
    fio2_note = fio2_unit_detect(rs_tbl.df) if fio2_unit_detect_on else None   # (1) BEFORE clip
    outlier_src = _apply_outliers(rs_tbl)                                      # (2) CLIF-spec clip
    rs = normalize_frame(rs_tbl.df, timezone)                                  # (3)+(4)
    if verbose:
        print(f"[resp_support.load_clean] rows={len(rs):,} cols={len(cols)} "
              f"outliers={outlier_src} fio2={fio2_note}")
    return rs
