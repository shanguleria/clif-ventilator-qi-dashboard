"""Stage 03 — SBT delivery detection (the numerators) per Jain et al. + liberal views.

For each ventilated-ICU day we look on the NATIVE-resolution waterfall rows for a
CONTROLLED -> SUPPORT mode transition, where the support episode is `pressure
support/cpap` with PEEP <= ps_peep_max (pressure-support arm) or PEEP <= cpap_peep_max
(CPAP arm). A transition whose start falls inside the day's ventilated-ICU window marks
the day. Three nested numerators are emitted (strict ⊆ any-duration ⊆ on-spontaneous):
  sbt_delivered      — strict (Jain headline): transition sustained >= support_min_minutes
  sbt_delivered_any  — liberal: a controlled->support transition of ANY duration
  on_spontaneous     — liberal: on ANY support mode at all that day (no transition, no
                       PEEP gate) — a patient parked on support all day counts here only.
Leadership asked for the more liberal views to see "what is actually happening"; the
strict transition-only numerator remains the by-the-book reference.

Each transition episode is attributed to its US/Central calendar day and LEFT-joined
onto the cohort/eligibility skeleton (cohort-restriction discipline — never group raw
events without restricting to the cohort (block, day) set). The metric numerator
(stage 04) is `eligible & sbt_delivered`.

Also emits a coverage diagnostic: pct_native = share of support-mode readings that
come from native vs hourly-scaffold rows (sites charting only hourly cannot resolve
sub-hourly trials -> delivery is a lower bound).

Outputs:
    output/intermediate/sbt_observation.parquet  (one row per cohort day + SBT flags)
    output/intermediate/sbt_diag.json            (coverage diagnostics for the tile note)

Aggregates only to stdout.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = PROJECT_ROOT / "code"
sys.path.insert(0, str(CODE_DIR))
import sbt_detect as sd            # noqa: E402

log = logging.getLogger("sbt.observation")


def _load_cohort_module():
    spec = importlib.util.spec_from_file_location("sbt_cohort", CODE_DIR / "01_build_cohort.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def attribute_transitions(days: pd.DataFrame, trans: pd.DataFrame, min_min: float) -> pd.DataFrame:
    """Per (encounter_block, icu_day): SBT-delivery flags from transition episodes
    whose ep_start falls inside the day window.

    From the one (arm-qualified, all-duration) transition set we derive BOTH numerators:
      sbt_delivered      — strict: >=1 transition sustained >= min_min  (Jain headline)
      sbt_delivered_any  — liberal: >=1 transition of ANY duration
    """
    base = days[["encounter_block", "icu_day", "day_in", "day_out"]].copy()
    base["encounter_block"] = base["encounter_block"].astype(str)
    cols = ["encounter_block", "icu_day", "sbt_delivered", "sbt_delivered_any",
            "n_transitions", "n_transitions_strict", "longest_support_min", "sbt_arm"]
    if trans is None or trans.empty:
        base["sbt_delivered"] = False
        base["sbt_delivered_any"] = False
        base["n_transitions"] = 0
        base["n_transitions_strict"] = 0
        base["longest_support_min"] = 0.0
        base["sbt_arm"] = ""
        return base[cols]
    t = trans.copy()
    t["encounter_block"] = t["encounter_block"].astype(str)
    con = duckdb.connect()
    con.register("d", base)
    con.register("t", t)
    out = con.execute(
        f"""
        SELECT d.encounter_block AS encounter_block, d.icu_day AS icu_day,
               SUM(CASE WHEN t.dur_min >= {float(min_min)} THEN 1 ELSE 0 END) > 0 AS sbt_delivered,
               COUNT(t.ep_start) > 0           AS sbt_delivered_any,
               COUNT(t.ep_start)               AS n_transitions,
               SUM(CASE WHEN t.dur_min >= {float(min_min)} THEN 1 ELSE 0 END) AS n_transitions_strict,
               COALESCE(MAX(t.dur_min), 0.0)   AS longest_support_min,
               COALESCE(string_agg(DISTINCT t.arm, ','), '') AS sbt_arm
        FROM d LEFT JOIN t
          ON d.encounter_block = t.encounter_block
         AND t.ep_start >= d.day_in AND t.ep_start < d.day_out
        GROUP BY d.encounter_block, d.icu_day
        """
    ).fetchdf()
    con.close()
    out["sbt_delivered"] = out["sbt_delivered"].fillna(False).astype(bool)
    out["sbt_delivered_any"] = out["sbt_delivered_any"].fillna(False).astype(bool)
    out["n_transitions"] = out["n_transitions"].fillna(0).astype(int)
    out["n_transitions_strict"] = out["n_transitions_strict"].fillna(0).astype(int)
    out["longest_support_min"] = out["longest_support_min"].fillna(0.0)
    # normalize arm label: ps / cpap / both
    out["sbt_arm"] = out["sbt_arm"].fillna("").apply(
        lambda s: "both" if ("ps" in s and "cpap" in s) else s)
    return out[cols]


def attribute_episode_durations(days: pd.DataFrame, trans: pd.DataFrame) -> pd.DataFrame:
    """Per transition episode: attach its day's (unit, icu_day) labels for the duration
    panel. PHI-free — returns [unit, icu_day, dur_min, arm] (no block/patient ids)."""
    cols = ["unit", "icu_day", "dur_min", "arm"]
    if trans is None or trans.empty:
        return pd.DataFrame(columns=cols)
    d = days[["encounter_block", "icu_day", "unit", "day_in", "day_out"]].copy()
    d["encounter_block"] = d["encounter_block"].astype(str)
    t = trans.copy()
    t["encounter_block"] = t["encounter_block"].astype(str)
    con = duckdb.connect()
    con.register("d", d)
    con.register("t", t)
    out = con.execute(
        """
        SELECT d.unit AS unit, d.icu_day AS icu_day, t.dur_min AS dur_min, t.arm AS arm
        FROM d JOIN t
          ON d.encounter_block = t.encounter_block
         AND t.ep_start >= d.day_in AND t.ep_start < d.day_out
        """
    ).fetchdf()
    con.close()
    return out[cols]


def attribute_attempt_subsets(days: pd.DataFrame, eps: pd.DataFrame, min_min: float) -> pd.DataFrame:
    """Per (encounter_block, icu_day): the 7 native numerator-subset bits (plan 04).

    For each subset S of the three trial-quality criteria {transition(T), >=2min(D),
    low-PEEP(P)}, the day-bit is "exists an attempt episode (ep_start in the day) meeting
    ALL criteria in S together" — an episode-level conjunction, not a day-level AND of
    separate events. The empty subset (on_spontaneous) is handled separately (native+scaffold
    presence). Columns: nb_t, nb_d, nb_p, nb_td, nb_tp, nb_dp, nb_tdp.
    Reconciliation: nb_tp == legacy sbt_delivered_any; nb_tdp == legacy sbt_delivered.
    """
    base = days[["encounter_block", "icu_day", "day_in", "day_out"]].copy()
    base["encounter_block"] = base["encounter_block"].astype(str)
    nb = ["nb_t", "nb_d", "nb_p", "nb_td", "nb_tp", "nb_dp", "nb_tdp"]
    cols = ["encounter_block", "icu_day"] + nb
    if eps is None or eps.empty:
        for c in nb:
            base[c] = False
        return base[cols]
    e = eps.copy()
    e["encounter_block"] = e["encounter_block"].astype(str)
    con = duckdb.connect()
    con.register("d", base)
    con.register("e", e)
    out = con.execute(
        f"""
        SELECT d.encounter_block AS encounter_block, d.icu_day AS icu_day,
               MAX(CASE WHEN e.is_transition THEN 1 ELSE 0 END) > 0                          AS nb_t,
               MAX(CASE WHEN e.dur_min >= {float(min_min)} THEN 1 ELSE 0 END) > 0            AS nb_d,
               MAX(CASE WHEN e.peep_ok THEN 1 ELSE 0 END) > 0                                AS nb_p,
               MAX(CASE WHEN e.is_transition AND e.dur_min >= {float(min_min)} THEN 1 ELSE 0 END) > 0 AS nb_td,
               MAX(CASE WHEN e.is_transition AND e.peep_ok THEN 1 ELSE 0 END) > 0            AS nb_tp,
               MAX(CASE WHEN e.dur_min >= {float(min_min)} AND e.peep_ok THEN 1 ELSE 0 END) > 0 AS nb_dp,
               MAX(CASE WHEN e.is_transition AND e.dur_min >= {float(min_min)} AND e.peep_ok
                        THEN 1 ELSE 0 END) > 0                                               AS nb_tdp
        FROM d LEFT JOIN e
          ON d.encounter_block = e.encounter_block
         AND e.ep_start >= d.day_in AND e.ep_start < d.day_out
        GROUP BY d.encounter_block, d.icu_day
        """
    ).fetchdf()
    con.close()
    for c in nb:
        out[c] = out[c].fillna(False).astype(bool)
    return out[cols]


def main() -> None:
    cohort_mod = _load_cohort_module()
    cohort_mod._ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(cohort_mod.LOGS_DIR / "03_sbt_observation.log", mode="w")],
    )
    cfg = cohort_mod.load_config()
    tz = cfg["timezone"]
    obs_cfg = cfg["sbt_observation"]
    log.info("support_min_minutes=%.1f ps_peep_max=%.0f cpap_peep_max=%.0f | support_modes=%s",
             float(obs_cfg.get("support_min_minutes", 2)), float(obs_cfg["ps_peep_max"]),
             float(obs_cfg["cpap_peep_max"]), sorted(sd.support_modes(cfg)))

    inter = cohort_mod.INTERMEDIATE_DIR
    elig = pd.read_parquet(inter / "sbt_eligibility.parquet")
    elig["encounter_block"] = elig["encounter_block"].astype(str)
    elig["day_in"] = cohort_mod._coerce_dttm(elig["day_in"], tz)
    elig["day_out"] = cohort_mod._coerce_dttm(elig["day_out"], tz)
    cohort_blocks = set(elig["encounter_block"].unique())

    wf = pd.read_parquet(cohort_mod.cpath("resp_waterfall"))
    wf = cohort_mod._normalize_waterfall(wf, tz)
    wf["encounter_block"] = wf["encounter_block"].astype(str)
    wf = wf[wf["encounter_block"].isin(cohort_blocks)]

    min_min = float(obs_cfg.get("support_min_minutes", 2))
    trans = sd.support_transitions(wf, cfg)
    n_trans_strict = int((trans["dur_min"] >= min_min).sum()) if not trans.empty else 0
    log.info("controlled->support transition episodes (arm-qualified): %d  (>=%.0f min: %d)",
             len(trans), min_min, n_trans_strict)

    obs_flags = attribute_transitions(elig, trans, min_min)
    # liberal "on a spontaneous mode at all" flag (no transition, no PEEP gate)
    spont = sd.support_presence_days(wf, elig, cfg)
    # total minutes on a support mode per day (duration measure for the spontaneous view)
    spont_min = sd.support_minutes_days(wf, elig, cfg)
    # exclusion-toggle model: per-day numerator-subset bits from ALL native support episodes
    eps = sd.support_episodes(wf, cfg)
    log.info("native support episodes: %d  (transitions: %d, low-PEEP: %d)",
             len(eps), int(eps["is_transition"].sum()) if not eps.empty else 0,
             int(eps["peep_ok"].sum()) if not eps.empty else 0)
    nb_flags = attribute_attempt_subsets(elig, eps, min_min)

    out = (elig
           .merge(obs_flags, on=["encounter_block", "icu_day"], how="left")
           .merge(spont, on=["encounter_block", "icu_day"], how="left")
           .merge(spont_min, on=["encounter_block", "icu_day"], how="left")
           .merge(nb_flags, on=["encounter_block", "icu_day"], how="left"))
    for c in ("nb_t", "nb_d", "nb_p", "nb_td", "nb_tp", "nb_dp", "nb_tdp"):
        out[c] = out[c].fillna(False).astype(bool)
    out["sbt_delivered"] = out["sbt_delivered"].fillna(False).astype(bool)
    out["sbt_delivered_any"] = out["sbt_delivered_any"].fillna(False).astype(bool)
    out["on_spontaneous"] = out["on_spontaneous"].fillna(False).astype(bool)
    out["n_transitions"] = out["n_transitions"].fillna(0).astype(int)
    out["n_transitions_strict"] = out["n_transitions_strict"].fillna(0).astype(int)
    out["longest_support_min"] = out["longest_support_min"].fillna(0.0)
    out["spont_minutes"] = out["spont_minutes"].fillna(0.0)
    out["sbt_arm"] = out["sbt_arm"].fillna("")

    # Numerator nesting invariant: strict transition => any-duration transition => on support.
    bad_nest = (out["sbt_delivered"] & ~out["sbt_delivered_any"]).sum() + \
               (out["sbt_delivered_any"] & ~out["on_spontaneous"]).sum()
    if bad_nest:
        raise RuntimeError(f"numerator nesting violated on {int(bad_nest)} days "
                           "(strict ⊆ any-duration ⊆ on-spontaneous)")

    # --- exclusion-toggle reconciliation (plan 04) -----------------------------------
    # The new episode engine must reproduce the legacy transition engine exactly:
    #   nb_tp  (transition & low-PEEP)            == sbt_delivered_any   (per day)
    #   nb_tdp (transition & low-PEEP & >=2min)   == sbt_delivered       (per day)
    mism_any = int((out["nb_tp"] != out["sbt_delivered_any"]).sum())
    mism_str = int((out["nb_tdp"] != out["sbt_delivered"]).sum())
    if mism_any or mism_str:
        raise RuntimeError(f"episode-engine reconciliation failed: nb_tp!=any on {mism_any} days, "
                           f"nb_tdp!=strict on {mism_str} days")
    # Down-closure: every non-empty subset bit implies on_spontaneous (the empty subset).
    nb_cols = ["nb_t", "nb_d", "nb_p", "nb_td", "nb_tp", "nb_dp", "nb_tdp"]
    bad_dc = int(sum((out[c] & ~out["on_spontaneous"]).sum() for c in nb_cols))
    if bad_dc:
        raise RuntimeError(f"down-closure violated: {bad_dc} subset-day bits set without on_spontaneous")
    n = len(out)
    log.info("exclusion-toggle endpoints (all vent-ICU days, n=%d):", n)
    log.info("  baseline  on_spontaneous {}            : %6d (%.1f%%)",
             int(out["on_spontaneous"].sum()), 100*out["on_spontaneous"].sum()/max(n, 1))
    log.info("  +transition           nb_t             : %6d", int(out["nb_t"].sum()))
    log.info("  +transition+low-PEEP  nb_tp  (==any)   : %6d", int(out["nb_tp"].sum()))
    log.info("  strict     nb_tdp (==sbt_delivered)    : %6d", int(out["nb_tdp"].sum()))
    log.info("  reconciliation OK: nb_tp==any, nb_tdp==strict, down-closure holds")

    out.to_parquet(inter / "sbt_observation.parquet", index=False)

    # Per-transition-episode durations (PHI-free) for the dashboard duration panel.
    durs = attribute_episode_durations(elig, trans)
    durs.to_parquet(inter / "sbt_durations.parquet", index=False)

    # ---- coverage diagnostic: native vs scaffold share of support-mode readings ----
    supp = wf[wf["mode_category"].astype("string").str.lower().isin(sd.support_modes(cfg))]
    n_supp = int(len(supp))
    n_supp_native = int((~supp["is_scaffold"].fillna(False)).sum())
    pct_native = (100.0 * n_supp_native / n_supp) if n_supp else None
    diag = {
        "pct_native_support_rows": pct_native,
        "n_support_rows": n_supp,
        "n_support_rows_native": n_supp_native,
        "n_transition_episodes": int(len(trans)),
    }
    (inter / "sbt_diag.json").write_text(json.dumps(diag, indent=2))

    # ---- log ----
    n_days = int(len(out))
    n_elig = int(out["eligible"].sum())
    n_sbt = int((out["eligible"] & out["sbt_delivered"]).sum())
    n_sbt_anyday = int(out["sbt_delivered"].sum())
    log.info("eligible SBT-opportunity days: %6d", n_elig)
    log.info("  SBT delivered / eligible:    %6d (%.1f%%)  [headline numerator]",
             n_sbt, 100 * n_sbt / max(n_elig, 1))
    log.info("  (strict transitions any day: %6d)", n_sbt_anyday)
    log.info("liberal numerators (any vent-ICU day, n=%d):", n_days)
    log.info("  strict SBT (>=%.0f min):       %6d (%.1f%%)", min_min,
             n_sbt_anyday, 100 * n_sbt_anyday / max(n_days, 1))
    log.info("  SBT any duration:            %6d (%.1f%%)",
             int(out["sbt_delivered_any"].sum()), 100 * int(out["sbt_delivered_any"].sum()) / max(n_days, 1))
    log.info("  on a spontaneous mode:       %6d (%.1f%%)",
             int(out["on_spontaneous"].sum()), 100 * int(out["on_spontaneous"].sum()) / max(n_days, 1))
    if pct_native is not None:
        log.info("coverage: %.1f%% of support-mode readings are native-resolution (%d/%d)",
                 pct_native, n_supp_native, n_supp)
    if not durs.empty:
        log.info("transition episodes persisted: %d (median dur %.0f min)",
                 len(durs), float(durs["dur_min"].median()))
    spm = out.loc[out["on_spontaneous"], "spont_minutes"]
    if len(spm):
        log.info("spontaneous-mode minutes/day (on-spont days): median %.0f min", float(spm.median()))
    log.info("wrote: sbt_observation.parquet, sbt_diag.json, sbt_durations.parquet")


if __name__ == "__main__":
    main()
