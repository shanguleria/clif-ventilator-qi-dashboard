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

    if columns == "all":
        cols = None                                    # load every column (the full waterfall needs them)
    else:
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
        print(f"[resp_support.load_clean] rows={len(rs):,} cols={len(rs.columns)} "
              f"outliers={outlier_src} fio2={fio2_note}")
    return rs


def _numpy_backed(df):
    """Coerce pyarrow-backed columns to classic numpy dtypes (object for text/bool, float64 for numeric),
    leaving datetime columns alone. clifpy's waterfall run-length-encodes via `s.ne(s.shift()).cumsum()`,
    and on pandas-3/pyarrow the resulting bool[pyarrow] has no cumsum kernel — numpy-backed dtypes avoid
    it. Applied only in the Level-1 waterfall path (LPV consumes load_clean directly and is unaffected)."""
    out = df.copy()
    for c in out.columns:
        s = out[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            continue
        if pd.api.types.is_bool_dtype(s):
            out[c] = s.astype("object")
        elif pd.api.types.is_numeric_dtype(s):
            out[c] = pd.to_numeric(s.astype("object"), errors="coerce").astype("float64")
        else:
            out[c] = s.astype("object")
    return out


def build_waterfall(data_dir, filetype, timezone, scope_hosp_ids, encounter_mapping, *,
                    cache_dir, waterfall_version, data_version=None, cache_name=None, verbose=True):
    """LEVEL 1 (proning/sat/sbt): the filled + hourly-scaffold ventilator timeline, built ON TOP of the
    Level-0 clean. Runs clifpy.process_resp_support_waterfall(load_clean(scope), bfill=False), attaches
    `encounter_block` from `encounter_mapping`, and caches under `cache_dir`. Returns the waterfall
    DataFrame. Cleaning is PRE-waterfall inside load_clean — there is no post-waterfall normalize step.

    Cache filename:
      * default (cache_name=None): scope-keyed `resp_waterfall__<hash(scope,version,data)>.parquet` — a
        narrow-scope build can never be silently reused for a wider-scope need (the old fixed-path bug).
      * cache_name="resp_waterfall": a FIXED name, for a vertical whose own later stages read that exact
        path. A sibling `<name>.version` sidecar records waterfall_version; a version mismatch is treated
        as a miss so the cache auto-rebuilds after a WATERFALL_VERSION bump (no manual --refresh needed).
    """
    import hashlib
    from pathlib import Path
    import clifpy

    cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
    scope = sorted({str(h) for h in scope_hosp_ids})
    ver_tag = f"{waterfall_version}|{data_version or ''}"

    if cache_name:
        cache_path = cache_dir / f"{cache_name}.parquet"
        ver_path = cache_dir / f"{cache_name}.version"
        fresh = (cache_path.exists() and ver_path.exists()
                 and ver_path.read_text().strip() == ver_tag)
    else:
        key = hashlib.sha1(("\n".join(scope) + "::" + ver_tag).encode()).hexdigest()[:16]
        cache_path = cache_dir / f"resp_waterfall__{key}.parquet"
        ver_path = None
        fresh = cache_path.exists()

    if fresh:
        if verbose:
            print(f"[resp_support.build_waterfall] cache hit {cache_path.name} (scope={len(scope)} hosps)")
        return pd.read_parquet(cache_path)

    rs = load_clean(data_dir, filetype, timezone, scope, columns="all", verbose=verbose)
    rs = _numpy_backed(rs)   # clifpy waterfall needs numpy-backed dtypes (bool[pyarrow].cumsum has no kernel)
    # clifpy's waterfall assumes UTC input: its hourly scaffold is generated in UTC (waterfall.py, the
    # DuckDB `utc=True` scaffold). load_clean hands it site-local tz-aware rows, so on pandas-3 the
    # concat of local real rows + UTC scaffold rows demotes recorded_dttm to a mixed-tz *object* column,
    # which downstream `pd.to_datetime(errors="coerce")` then NaT's on every scaffold row. Feed UTC in,
    # convert the (clean, single-tz) result back to the site tz — same absolute instants either way.
    rs["recorded_dttm"] = pd.to_datetime(rs["recorded_dttm"], utc=True)
    wf = clifpy.process_resp_support_waterfall(
        rs, id_col="hospitalization_id", bfill=False, verbose=verbose,
    )
    wf["recorded_dttm"] = pd.to_datetime(wf["recorded_dttm"], utc=True).dt.tz_convert(timezone)
    wf["hospitalization_id"] = wf["hospitalization_id"].astype(str)
    em = encounter_mapping[["hospitalization_id", "encounter_block"]].copy()
    em["hospitalization_id"] = em["hospitalization_id"].astype(str)
    wf = wf.merge(em, on="hospitalization_id", how="left")
    wf.to_parquet(cache_path, index=False)
    if ver_path is not None:
        ver_path.write_text(ver_tag)
    if verbose:
        print(f"[resp_support.build_waterfall] built {len(wf):,} rows "
              f"(scope={len(scope)} hosps) -> {cache_path.name}")
    return wf
