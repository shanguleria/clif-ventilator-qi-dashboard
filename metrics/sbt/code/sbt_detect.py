"""Temporal-detection core for the SBT QI vertical (Jain et al.).

Pure functions over the clifpy respiratory-support waterfall (hourly `:59:59`
scaffold rows interleaved with native-resolution rows) + the cohort patient-day
skeleton. No I/O to stdout.

Four pieces:
  A. controlled_hours_before  — cumulative controlled-vent hours accrued before a
                                day's opportunity (the >=12h gate).
  B. trach_day_flag           — tracheostomy in place on a patient-day (excluded
                                from numerator AND denominator).
  C. hourly_stability_window  — a >=2h contiguous window of FiO2<=0.50, PEEP<=8,
                                SpO2>=88, NE-equiv<=0.2 (resampled onto scaffold hrs).
  D. support_transitions      — controlled->support mode-change episodes (the SBT,
                                transition-only), arm-qualified, on native rows. Returns
                                ALL durations with dur_min; the >=2-min "strict" floor
                                is applied downstream (stage 03) so the same episode set
                                feeds both the strict and the any-duration numerators.
  E. support_presence_days    — per patient-day flag: was the patient on ANY support
                                mode at all that day (no transition required, no PEEP
                                gate) -> the liberal "on a spontaneous mode" numerator.

Mode vocabulary (lowercased by the waterfall) is config-driven (`sbt_modes`).
CONTROLLED gates the 12h clock; SUPPORT (pressure support/cpap) is the SBT target.
CPAP pressure is read from `peep_set` (CLIF resp_support has no dedicated CPAP
column) — documented as a known limitation.
"""

from __future__ import annotations

from datetime import timedelta

import duckdb
import numpy as np
import pandas as pd

# Each hourly scaffold row is treated as 1h of coverage; consecutive stable hours
# must be within this delta to count as a contiguous window (hourly cadence + DST).
MAX_HOUR_GAP = pd.Timedelta(minutes=90)
TRAILING_NATIVE_CAP = pd.Timedelta(hours=1)   # cap the open-ended final native row


# ---------------------------------------------------------------------------
# Mode classification
# ---------------------------------------------------------------------------
def controlled_modes(cfg: dict) -> set[str]:
    return {m.lower() for m in cfg["sbt_modes"]["controlled_modes"]}


def support_modes(cfg: dict) -> set[str]:
    return {m.lower() for m in cfg["sbt_modes"]["support_modes"]}


def is_controlled_row(wf: pd.DataFrame, cfg: dict, imv_category: str = "imv") -> pd.Series:
    return (wf["device_category"].astype("string").str.lower() == imv_category) & \
           (wf["mode_category"].astype("string").str.lower().isin(controlled_modes(cfg)))


# ---------------------------------------------------------------------------
# A. Controlled-vent hours accrued before each day's opportunity
# ---------------------------------------------------------------------------
def controlled_hours_before(wf: pd.DataFrame, days: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per (encounter_block, icu_day): count of CONTROLLED scaffold hours strictly
    before that day's `day_in` (cumulative-since-intubation). Each scaffold row
    (`is_scaffold`) ~ 1 clock-hour."""
    base = days[["encounter_block", "icu_day", "day_in"]].copy()
    base["encounter_block"] = base["encounter_block"].astype(str)

    sc = wf[wf["is_scaffold"].fillna(False) & is_controlled_row(wf, cfg)][
        ["encounter_block", "recorded_dttm"]].copy()
    sc["encounter_block"] = sc["encounter_block"].astype(str)
    if sc.empty:
        base["prior_controlled_h"] = 0
        return base[["encounter_block", "icu_day", "prior_controlled_h"]]

    con = duckdb.connect()
    con.register("d", base)
    con.register("s", sc)
    out = con.execute(
        """
        SELECT d.encounter_block AS encounter_block, d.icu_day AS icu_day,
               COUNT(s.recorded_dttm) AS prior_controlled_h
        FROM d LEFT JOIN s
          ON d.encounter_block = s.encounter_block
         AND s.recorded_dttm < d.day_in
        GROUP BY d.encounter_block, d.icu_day
        """
    ).fetchdf()
    con.close()
    out["prior_controlled_h"] = out["prior_controlled_h"].fillna(0).astype(int)
    return out


# ---------------------------------------------------------------------------
# B. Tracheostomy-in-place on a patient-day
# ---------------------------------------------------------------------------
def trach_day_flag(wf: pd.DataFrame, days: pd.DataFrame) -> pd.DataFrame:
    """Per (encounter_block, icu_day): True if any waterfall row in [day_in, day_out)
    has tracheostomy==1 (the flag is forward-filled per encounter by the waterfall)."""
    base = days[["encounter_block", "icu_day", "day_in", "day_out"]].copy()
    base["encounter_block"] = base["encounter_block"].astype(str)
    if "tracheostomy" not in wf.columns:
        base["trach_day"] = False
        return base[["encounter_block", "icu_day", "trach_day"]]

    tr = wf[pd.to_numeric(wf["tracheostomy"], errors="coerce").fillna(0) >= 1][
        ["encounter_block", "recorded_dttm"]].copy()
    tr["encounter_block"] = tr["encounter_block"].astype(str)
    con = duckdb.connect()
    con.register("d", base)
    con.register("t", tr)
    out = con.execute(
        """
        SELECT d.encounter_block AS encounter_block, d.icu_day AS icu_day,
               COUNT(t.recorded_dttm) > 0 AS trach_day
        FROM d LEFT JOIN t
          ON d.encounter_block = t.encounter_block
         AND t.recorded_dttm >= d.day_in AND t.recorded_dttm < d.day_out
        GROUP BY d.encounter_block, d.icu_day
        """
    ).fetchdf()
    con.close()
    out["trach_day"] = out["trach_day"].fillna(False).astype(bool)
    return out


# ---------------------------------------------------------------------------
# C. >=2h contiguous stable-physiology window per day
# ---------------------------------------------------------------------------
def hourly_stability_window(wf: pd.DataFrame, days: pd.DataFrame,
                            spo2: pd.DataFrame, ne_tl: pd.DataFrame,
                            cfg: dict) -> pd.DataFrame:
    """Per (encounter_block, icu_day): does a contiguous run of >= stability_min_hours
    stable scaffold hours exist?

    A scaffold hour is STABLE iff FiO2<=fio2_max & PEEP<=peep_max & SpO2>=spo2_min &
    NE-equiv<=ne_max (NaN -> not stable). SpO2 (irregular, from vitals) and the
    NE-equiv step function are resampled onto each scaffold hour via merge_asof
    backward. Assessability is tracked separately so all-missing days become
    `not_assessable` rather than silently not-eligible.

    Returns [encounter_block, icu_day, stable_window, n_stable_hours,
             n_scaffold_hours, n_assessable_hours].
    """
    elig = cfg["sbt_eligibility"]
    fio2_max = float(elig["fio2_max"]); peep_max = float(elig["peep_max"])
    spo2_min = float(elig["spo2_min"]); ne_max = float(elig["ne_equiv_max_mcg_kg_min"])
    min_hours = int(elig.get("stability_min_hours", 2))
    tz = cfg.get("timezone", "US/Central")

    def _tz(s):
        # DuckDB fetchdf() relabels US/Central -> America/Chicago; align all merge
        # keys to one tz label so pandas merge_asof accepts them.
        s = pd.to_datetime(s)
        return s.dt.tz_convert(tz) if getattr(s.dt, "tz", None) is not None \
            else s.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")

    cols = ["encounter_block", "icu_day", "stable_window",
            "n_stable_hours", "n_scaffold_hours", "n_assessable_hours"]

    # Scaffold hours carrying FiO2/PEEP (already hourly + ffilled in the waterfall).
    sc = wf[wf["is_scaffold"].fillna(False)][
        ["encounter_block", "recorded_dttm", "fio2_set", "peep_set"]].copy()
    sc["encounter_block"] = sc["encounter_block"].astype(str)
    sc = sc.dropna(subset=["recorded_dttm"])
    if sc.empty:
        return pd.DataFrame(columns=cols)

    # Attribute each scaffold hour to its cohort (block, day) via the day window.
    d = days[["encounter_block", "icu_day", "day_in", "day_out"]].copy()
    d["encounter_block"] = d["encounter_block"].astype(str)
    con = duckdb.connect()
    con.register("s", sc)
    con.register("d", d)
    sh = con.execute(
        """
        SELECT s.encounter_block AS encounter_block, d.icu_day AS icu_day,
               s.recorded_dttm AS t, s.fio2_set AS fio2_set, s.peep_set AS peep_set
        FROM s JOIN d
          ON s.encounter_block = d.encounter_block
         AND s.recorded_dttm >= d.day_in AND s.recorded_dttm < d.day_out
        """
    ).fetchdf()
    con.close()
    if sh.empty:
        return pd.DataFrame(columns=cols)
    sh["t"] = _tz(sh["t"])
    sh = sh.sort_values("t").reset_index(drop=True)

    # SpO2 onto each scaffold hour (backward, 1h tolerance).
    sp = spo2[["encounter_block", "t", "spo2"]].copy()
    sp["encounter_block"] = sp["encounter_block"].astype(str)
    sp["t"] = _tz(sp["t"])
    sp = sp.dropna(subset=["t"]).sort_values("t")
    if not sp.empty:
        sh = pd.merge_asof(sh, sp, by="encounter_block", on="t",
                           direction="backward", tolerance=pd.Timedelta(hours=1))
    else:
        sh["spo2"] = np.nan

    # NE-equiv step value onto each scaffold hour (backward, no tolerance: a pressor
    # rate holds until the next charted change; absent any row -> 0, assessable).
    ne = ne_tl[["encounter_block", "t", "ne_equiv", "assessable"]].copy() if not ne_tl.empty \
        else pd.DataFrame(columns=["encounter_block", "t", "ne_equiv", "assessable"])
    ne["encounter_block"] = ne["encounter_block"].astype(str)
    if not ne.empty:
        ne["t"] = _tz(ne["t"])
    ne = ne.dropna(subset=["t"]).sort_values("t")
    if not ne.empty:
        sh = pd.merge_asof(sh, ne, by="encounter_block", on="t", direction="backward")
    else:
        sh["ne_equiv"] = np.nan
        sh["assessable"] = np.nan
    sh["ne_equiv"] = sh["ne_equiv"].fillna(0.0)             # no pressor row -> 0
    sh["ne_assessable"] = sh["assessable"].fillna(True).astype(bool)

    # Stable + assessable per scaffold hour.
    sh["stable_h"] = (
        (sh["fio2_set"] <= fio2_max) & (sh["peep_set"] <= peep_max) &
        (sh["spo2"] >= spo2_min) & (sh["ne_equiv"] <= ne_max)
    ).fillna(False)
    sh["assessable_h"] = (
        sh["fio2_set"].notna() & sh["peep_set"].notna() &
        sh["spo2"].notna() & sh["ne_assessable"]
    )

    # Run-length over consecutive stable scaffold hours per (block, day).
    sh = sh.sort_values(["encounter_block", "icu_day", "t"]).reset_index(drop=True)
    key_change = (sh["encounter_block"] != sh["encounter_block"].shift()) | \
                 (sh["icu_day"] != sh["icu_day"].shift())
    gap = sh.groupby(["encounter_block", "icu_day"], sort=False)["t"].diff() > MAX_HOUR_GAP
    brk = key_change | sh["stable_h"].ne(sh["stable_h"].shift()) | gap.fillna(True)
    sh["run_id"] = brk.cumsum()
    run_size = sh.groupby("run_id")["t"].transform("size")
    sh["in_qual_run"] = sh["stable_h"] & (run_size >= min_hours)

    agg = (sh.groupby(["encounter_block", "icu_day"], observed=True)
             .agg(stable_window=("in_qual_run", "any"),
                  n_stable_hours=("stable_h", "sum"),
                  n_scaffold_hours=("t", "size"),
                  n_assessable_hours=("assessable_h", "sum"))
             .reset_index())
    agg["stable_window"] = agg["stable_window"].astype(bool)
    for c in ("n_stable_hours", "n_scaffold_hours", "n_assessable_hours"):
        agg[c] = agg[c].astype(int)
    return agg[cols]


# ---------------------------------------------------------------------------
# D. Controlled -> support transition episodes (the SBT, transition-only)
# ---------------------------------------------------------------------------
def _row_mode_class(wf: pd.DataFrame, cfg: dict, imv_category: str = "imv") -> pd.Series:
    """Per native row: 'controlled' | 'sbt_ps' | 'sbt_cpap' | 'support_other' | 'other'.

    SUPPORT qualifies as an SBT target arm iff PEEP meets the arm threshold:
      pressure-support arm  -> PEEP <= ps_peep_max
      CPAP arm              -> PEEP <= cpap_peep_max  (PEEP read as CPAP pressure)
    CPAP arm = device_category=='cpap' OR pressure_support_set null/0 (no PS set).
    """
    obs = cfg["sbt_observation"]
    ps_max = float(obs["ps_peep_max"]); cpap_max = float(obs["cpap_peep_max"])
    dev = wf["device_category"].astype("string").str.lower()
    mode = wf["mode_category"].astype("string").str.lower()
    peep = pd.to_numeric(wf["peep_set"], errors="coerce")
    ps = pd.to_numeric(wf.get("pressure_support_set"), errors="coerce")

    is_ctrl = (dev == imv_category) & mode.isin(controlled_modes(cfg))
    is_supp = mode.isin(support_modes(cfg))
    is_cpap_arm = (dev == "cpap") | ps.isna() | (ps == 0)
    qual_ps = is_supp & (~is_cpap_arm) & (peep <= ps_max)
    qual_cpap = is_supp & (is_cpap_arm) & (peep <= cpap_max)

    cls = pd.Series("other", index=wf.index, dtype="object")
    cls[is_supp] = "support_other"
    cls[qual_ps] = "sbt_ps"
    cls[qual_cpap] = "sbt_cpap"
    cls[is_ctrl] = "controlled"
    return cls


def support_transitions(wf: pd.DataFrame, cfg: dict, imv_category: str = "imv") -> pd.DataFrame:
    """Controlled->qualifying-support transition episodes on NATIVE rows.

    Returns one row per transition episode (ALL durations):
      [encounter_block, ep_start, ep_end, dur_min, arm]
    An episode is a maximal run of consecutive native rows of the SAME mode class;
    a transition is a qualifying-support episode ('sbt_ps'/'sbt_cpap') whose
    immediately preceding episode in the block is 'controlled'. The >= support_min_minutes
    "strict SBT" floor is NOT applied here — stage 03 derives both the strict numerator
    (dur_min >= floor) and the any-duration numerator (dur_min > 0) from this one set, so
    the arm qualification (PEEP<=ps_peep_max / CPAP<=cpap_peep_max) is the only gate kept.
    """
    cols = ["encounter_block", "ep_start", "ep_end", "dur_min", "arm"]

    nat = wf[~wf["is_scaffold"].fillna(False)].copy()
    nat["encounter_block"] = nat["encounter_block"].astype(str)
    nat = nat.dropna(subset=["encounter_block", "recorded_dttm"])
    nat = nat[nat["encounter_block"] != "nan"]
    if nat.empty:
        return pd.DataFrame(columns=cols)
    nat = nat.sort_values(["encounter_block", "recorded_dttm"]).reset_index(drop=True)
    nat["cls"] = _row_mode_class(nat, cfg, imv_category)

    # Native segment ends = next native row of the block; trailing capped.
    nat["seg_end"] = nat.groupby("encounter_block")["recorded_dttm"].shift(-1)
    nat["seg_end"] = nat["seg_end"].fillna(nat["recorded_dttm"] + TRAILING_NATIVE_CAP)

    # Collapse consecutive same-class rows into episodes.
    blk_change = nat["encounter_block"] != nat["encounter_block"].shift()
    cls_change = nat["cls"] != nat["cls"].shift()
    nat["ep_id"] = (blk_change | cls_change).cumsum()
    ep = (nat.groupby("ep_id", observed=True)
            .agg(encounter_block=("encounter_block", "first"),
                 cls=("cls", "first"),
                 ep_start=("recorded_dttm", "min"),
                 ep_end=("seg_end", "max"))
            .reset_index(drop=True))
    ep = ep.sort_values(["encounter_block", "ep_start"]).reset_index(drop=True)

    # A transition = qualifying-support episode whose previous episode is controlled.
    ep["prev_cls"] = ep.groupby("encounter_block")["cls"].shift()
    is_sbt = ep["cls"].isin(["sbt_ps", "sbt_cpap"])
    trans = ep[is_sbt & (ep["prev_cls"] == "controlled")].copy()
    if trans.empty:
        return pd.DataFrame(columns=cols)
    trans["dur_min"] = (trans["ep_end"] - trans["ep_start"]).dt.total_seconds() / 60.0
    trans["arm"] = trans["cls"].str.replace("sbt_", "", regex=False)
    return trans[cols].reset_index(drop=True)


# ---------------------------------------------------------------------------
# E. On ANY support mode that day (the liberal "on a spontaneous mode" numerator)
# ---------------------------------------------------------------------------
def support_presence_days(wf: pd.DataFrame, days: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per (encounter_block, icu_day): True if ANY waterfall row (native OR scaffold)
    whose recorded_dttm falls in the day window is on a support mode.

    No transition required and NO PEEP/arm gate — this is the broadest "what is actually
    happening" view (a patient parked on pressure-support/CPAP all day counts, unlike the
    transition-only SBT numerator). Mode membership is `sbt_modes.support_modes`.
    Returns [encounter_block, icu_day, on_spontaneous]."""
    base = days[["encounter_block", "icu_day", "day_in", "day_out"]].copy()
    base["encounter_block"] = base["encounter_block"].astype(str)

    supp = wf[wf["mode_category"].astype("string").str.lower().isin(support_modes(cfg))][
        ["encounter_block", "recorded_dttm"]].copy()
    supp["encounter_block"] = supp["encounter_block"].astype(str)
    supp = supp.dropna(subset=["recorded_dttm"])
    if supp.empty:
        base["on_spontaneous"] = False
        return base[["encounter_block", "icu_day", "on_spontaneous"]]

    con = duckdb.connect()
    con.register("d", base)
    con.register("s", supp)
    out = con.execute(
        """
        SELECT d.encounter_block AS encounter_block, d.icu_day AS icu_day,
               COUNT(s.recorded_dttm) > 0 AS on_spontaneous
        FROM d LEFT JOIN s
          ON d.encounter_block = s.encounter_block
         AND s.recorded_dttm >= d.day_in AND s.recorded_dttm < d.day_out
        GROUP BY d.encounter_block, d.icu_day
        """
    ).fetchdf()
    con.close()
    out["on_spontaneous"] = out["on_spontaneous"].fillna(False).astype(bool)
    return out[["encounter_block", "icu_day", "on_spontaneous"]]


# ---------------------------------------------------------------------------
# F. Continuous paralytic (NMBA) in effect on a patient-day (eligibility exclusion)
# ---------------------------------------------------------------------------
PARALYTIC_TRAILING_CAP = timedelta(hours=24)   # cap an open-ended final paralytic record
PARALYTIC_STOP_ACTION = "stop"                 # mar_action_category that ends an infusion


def _coerce_dttm(series: pd.Series, tz: str) -> pd.Series:
    s = pd.to_datetime(series, errors="coerce")
    if getattr(s.dt, "tz", None) is None:
        return s.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
    return s.dt.tz_convert(tz)


def paralytic_categories(cfg: dict) -> set[str]:
    return {c.lower() for c in cfg.get("sbt_paralytics", {}).get("paralytic_categories", [])}


def paralytic_day_flag(mac: pd.DataFrame, days: pd.DataFrame, cfg: dict, tz: str) -> pd.DataFrame:
    """Per (encounter_block, icu_day): True if a CONTINUOUS paralytic infusion is in
    effect at any time in the day window.

    Each charted paralytic record with med_dose>0 (and not a 'stop' mar_action) is "on"
    from its admin_dttm until the same block's next paralytic record (trailing capped at
    24h, like the NEE engine). A day overlapping any on-interval is flagged. A paralyzed
    patient has no respiratory drive, so the day is not an SBT candidate -> excluded from
    the eligible denominator (status 'excluded_paralytic'). Bolus paralytics live in
    medication_admin_intermittent and are not captured here.
    Returns [encounter_block, icu_day, on_paralytic]."""
    base = days[["encounter_block", "icu_day", "day_in", "day_out"]].copy()
    base["encounter_block"] = base["encounter_block"].astype(str)
    cols = ["encounter_block", "icu_day", "on_paralytic"]
    cats = paralytic_categories(cfg)
    if mac is None or mac.empty or not cats:
        base["on_paralytic"] = False
        return base[cols]

    df = mac[mac["med_category"].astype("string").str.lower().isin(cats)].copy()
    if df.empty:
        base["on_paralytic"] = False
        return base[cols]
    df["encounter_block"] = df["encounter_block"].astype(str)
    df["admin_dttm"] = _coerce_dttm(df["admin_dttm"], tz)
    df = df.dropna(subset=["encounter_block", "admin_dttm"])
    df["med_dose"] = pd.to_numeric(df.get("med_dose"), errors="coerce")
    if "mar_action_category" in df.columns:
        df["__stop"] = df["mar_action_category"].astype("string").str.strip().str.lower().eq(
            PARALYTIC_STOP_ACTION)
    else:
        df["__stop"] = False

    df = df.sort_values(["encounter_block", "admin_dttm"])
    df["seg_end"] = df.groupby("encounter_block")["admin_dttm"].shift(-1)
    df["seg_end"] = df["seg_end"].fillna(df["admin_dttm"] + PARALYTIC_TRAILING_CAP)
    on = df[(df["med_dose"].fillna(0) > 0) & (~df["__stop"]) & (df["seg_end"] > df["admin_dttm"])][
        ["encounter_block", "admin_dttm", "seg_end"]].rename(
        columns={"admin_dttm": "on_start", "seg_end": "on_end"})
    if on.empty:
        base["on_paralytic"] = False
        return base[cols]

    con = duckdb.connect()
    con.register("d", base)
    con.register("p", on)
    out = con.execute(
        """
        SELECT d.encounter_block AS encounter_block, d.icu_day AS icu_day,
               COUNT(p.on_start) > 0 AS on_paralytic
        FROM d LEFT JOIN p
          ON d.encounter_block = p.encounter_block
         AND p.on_start < d.day_out AND p.on_end > d.day_in
        GROUP BY d.encounter_block, d.icu_day
        """
    ).fetchdf()
    con.close()
    out["on_paralytic"] = out["on_paralytic"].fillna(False).astype(bool)
    return out[cols]
