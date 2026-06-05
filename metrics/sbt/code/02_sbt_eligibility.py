"""Stage 02 — eligible SBT-opportunity days (the SBT denominator), per Jain et al.

Starting from the ventilated-ICU patient-days (01/cohort.parquet), a day is an
ELIGIBLE SBT opportunity iff:
  - >= controlled_min_hours (12h) of CONTROLLED ventilation has accrued before the
    day's opportunity (cumulative-since-intubation), AND
  - there is a >= stability_min_hours (2h) contiguous window that day of stable
    physiology: FiO2 <= 0.50, PEEP <= 8, SpO2 >= 88, norepinephrine-equiv <= 0.2
    mcg/kg/min, AND
  - the patient is NOT tracheostomized that day (excluded from numerator AND
    denominator).

Eligibility status per day (priority: trach > paralytic > stability/accrual):
  excluded_trach     — tracheostomized that day (dropped from num & den)
  excluded_paralytic — a continuous paralytic (NMBA) infusion in effect that day; no
                       respiratory drive, so not an SBT candidate -> dropped from the
                       eligible denominator, shown as a justified exclusion
  eligible           — accrued 12h controlled AND a >=2h stable window
  not_assessable     — accrued 12h but stability un-assessable (no scaffold hour with
                       all four signals) -> reported as a bound, excluded from the rate
  not_eligible       — accrued 12h but assessed not-stable, OR < 12h controlled

Inputs: cohort.parquet, the warm resp_waterfall cache, vitals (spo2 + weight_kg),
medication_admin_continuous (vasopressors). Aggregates only to stdout.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from clifpy.tables import Vitals, MedicationAdminContinuous

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = PROJECT_ROOT / "code"
sys.path.insert(0, str(CODE_DIR))
import sbt_detect as sd            # noqa: E402
import sbt_vasopressors as sv      # noqa: E402

log = logging.getLogger("sbt.eligibility")

SPO2_RANGE = (50.0, 100.0)        # plausible SpO2 %


def _load_cohort_module():
    spec = importlib.util.spec_from_file_location("sbt_cohort", CODE_DIR / "01_build_cohort.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    cohort_mod = _load_cohort_module()
    cohort_mod._ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(cohort_mod.LOGS_DIR / "02_sbt_eligibility.log", mode="w")],
    )
    cfg = cohort_mod.load_config()
    tz = cfg["timezone"]
    ds = cfg["primary_dataset"]
    elig_cfg = cfg["sbt_eligibility"]
    ctrl_min_h = int(elig_cfg.get("controlled_min_hours", 12))
    exclude_trach = bool(elig_cfg.get("exclude_trach_days", True))
    exclude_paralytic = bool(elig_cfg.get("exclude_paralytic_days", True))

    inter = cohort_mod.INTERMEDIATE_DIR

    # ---- cohort ----
    cohort = pd.read_parquet(inter / "cohort.parquet")
    cohort["encounter_block"] = cohort["encounter_block"].astype(str)
    cohort["day_in"] = cohort_mod._coerce_dttm(cohort["day_in"], tz)
    cohort["day_out"] = cohort_mod._coerce_dttm(cohort["day_out"], tz)
    cohort_blocks = set(cohort["encounter_block"].unique())
    log.info("ventilated-ICU patient-days in: %d (%d blocks)", len(cohort), len(cohort_blocks))

    # ---- waterfall (cache) restricted to cohort blocks ----
    wf = pd.read_parquet(cohort_mod.cpath("resp_waterfall"))
    wf = cohort_mod._normalize_waterfall(wf, tz)
    wf["encounter_block"] = wf["encounter_block"].astype(str)
    wf = wf[wf["encounter_block"].isin(cohort_blocks)]
    log.info("waterfall rows (cohort blocks): %d  (scaffold=%d)",
             len(wf), int(wf["is_scaffold"].fillna(False).sum()))

    # ---- hosp ids + mapping for vitals/meds loads ----
    mapping = pd.read_parquet(cohort_mod.cpath("encounter_mapping"))
    mapping["hospitalization_id"] = mapping["hospitalization_id"].astype(str)
    mapping["encounter_block"] = mapping["encounter_block"].astype(str)
    map_cohort = mapping[mapping["encounter_block"].isin(cohort_blocks)]
    hosp_ids = sorted(map_cohort["hospitalization_id"].unique())
    h2b = map_cohort.set_index("hospitalization_id")["encounter_block"].to_dict()

    # ---- vitals: SpO2 (stability) + weight_kg (vasopressor normalization) ----
    vit = Vitals.from_file(
        ds["data_path"], filetype=ds["file_format"], timezone=tz,
        filters={"hospitalization_id": hosp_ids, "vital_category": ["spo2", "weight_kg"]},
        columns=["hospitalization_id", "vital_category", "recorded_dttm", "vital_value"],
    ).df
    vit["hospitalization_id"] = vit["hospitalization_id"].astype(str)
    vit["recorded_dttm"] = cohort_mod._coerce_dttm(vit["recorded_dttm"], tz)
    vit["vital_value"] = pd.to_numeric(vit["vital_value"], errors="coerce")

    spo2 = vit[vit["vital_category"] == "spo2"].copy()
    spo2 = spo2[spo2["vital_value"].between(*SPO2_RANGE) & spo2["recorded_dttm"].notna()]
    spo2["encounter_block"] = spo2["hospitalization_id"].map(h2b).astype("string")
    spo2 = spo2.dropna(subset=["encounter_block"]).rename(
        columns={"recorded_dttm": "t", "vital_value": "spo2"})[["encounter_block", "t", "spo2"]]
    weight_vitals = vit[vit["vital_category"] == "weight_kg"][
        ["hospitalization_id", "recorded_dttm", "vital_category", "vital_value"]].copy()
    log.info("vitals: spo2=%d obs, weight_kg=%d obs", len(spo2), len(weight_vitals))

    # ---- continuous meds: vasopressors (NEE) + paralytics (exclusion) in one load ----
    vaso_cats = sorted(sv.vasopressor_categories(cfg))
    paralytic_cats = sorted(sd.paralytic_categories(cfg)) if exclude_paralytic else []
    med_cats = sorted(set(vaso_cats) | set(paralytic_cats))
    mac = MedicationAdminContinuous.from_file(
        ds["data_path"], filetype=ds["file_format"], timezone=tz,
        filters={"hospitalization_id": hosp_ids, "med_category": med_cats},
        columns=["hospitalization_id", "admin_dttm", "med_category", "med_dose",
                 "med_dose_unit", "mar_action_category"],
    ).df
    mac["hospitalization_id"] = mac["hospitalization_id"].astype(str)
    mac["encounter_block"] = mac["hospitalization_id"].map(h2b).astype("string")
    mac = mac.dropna(subset=["encounter_block"])
    log.info("continuous-med rows: %d (med_categories present: %s)",
             len(mac), sorted(mac["med_category"].astype("string").str.lower().dropna().unique().tolist()))
    # NEE timeline (ne_equiv_timeline filters to vasopressor categories internally)
    ne_tl = sv.ne_equiv_timeline(mac, weight_vitals, cfg, tz)
    log.info("NE-equiv timeline change-points: %d (blocks=%d)",
             len(ne_tl), ne_tl["encounter_block"].nunique() if not ne_tl.empty else 0)

    # ---- per-day computations ----
    ctrl = sd.controlled_hours_before(wf, cohort, cfg)
    trach = sd.trach_day_flag(wf, cohort)
    stab = sd.hourly_stability_window(wf, cohort, spo2, ne_tl, cfg)
    paral = sd.paralytic_day_flag(mac, cohort, cfg, tz)

    out = (cohort
           .merge(ctrl, on=["encounter_block", "icu_day"], how="left")
           .merge(trach, on=["encounter_block", "icu_day"], how="left")
           .merge(stab, on=["encounter_block", "icu_day"], how="left")
           .merge(paral, on=["encounter_block", "icu_day"], how="left"))
    out["prior_controlled_h"] = out["prior_controlled_h"].fillna(0).astype(int)
    out["trach_day"] = out["trach_day"].fillna(False).astype(bool)
    out["on_paralytic"] = out["on_paralytic"].fillna(False).astype(bool)
    out["stable_window"] = out["stable_window"].fillna(False).astype(bool)
    out["stable_window_no_ne"] = out["stable_window_no_ne"].fillna(False).astype(bool)
    out["stable_window_ne_only"] = out["stable_window_ne_only"].fillna(False).astype(bool)
    for c in ("n_stable_hours", "n_scaffold_hours", "n_assessable_hours"):
        out[c] = out[c].fillna(0).astype(int)

    out["accrued_12h"] = out["prior_controlled_h"] >= ctrl_min_h
    trach_flag = out["trach_day"] & exclude_trach
    paral_flag = out["on_paralytic"] & exclude_paralytic

    # Status (per the docstring). A continuous paralytic precludes spontaneous breathing,
    # so a paralyzed (non-trach) day is excluded from the eligible denominator (justified).
    cond_trach = trach_flag
    cond_paral = (~trach_flag) & paral_flag
    free = (~trach_flag) & (~paral_flag)
    cond_elig = free & out["accrued_12h"] & out["stable_window"]
    cond_notassess = free & out["accrued_12h"] & (~out["stable_window"]) & (out["n_assessable_hours"] == 0)
    out["eligibility_status"] = np.select(
        [cond_trach, cond_paral, cond_elig, cond_notassess],
        ["excluded_trach", "excluded_paralytic", "eligible", "not_assessable"],
        default="not_eligible",
    )
    out["eligible"] = out["eligibility_status"] == "eligible"

    # ---- exclusion-toggle model: raw per-day DENOMINATOR bits (plan 04) --------------
    # Stored raw (NOT config-gated) so the dashboard toggles decide what to apply. The
    # legacy eligibility_status/notelig_reason above stay for the (unchanged) tile feed.
    #   db_trach     — tracheostomized that day              (toggle: exclude trach)
    #   db_paralytic — continuous NMBA that day              (toggle: exclude paralytic)
    #   db_accrued12 — >=12h controlled accrued before day   (toggle: require >=12h controlled)
    #   db_stable_oxy/_vaso/_both — >=2h stable-physiology window for the active criterion set
    out["db_trach"] = out["trach_day"].astype(bool)
    out["db_paralytic"] = out["on_paralytic"].astype(bool)
    out["db_accrued12"] = out["accrued_12h"].astype(bool)
    out["db_stable_oxy"] = out["stable_window_no_ne"].astype(bool)
    out["db_stable_vaso"] = out["stable_window_ne_only"].astype(bool)
    out["db_stable_both"] = out["stable_window"].astype(bool)

    # Subdivide the not_eligible bucket into its driver (for the dashboard "Why not
    # eligible?" sub-bar + cross-site denominator harmonization). Precedence: the
    # <12h-controlled accrual gate is sequential-first (stability is only assessed once
    # 12h has accrued), so it wins; among stability failures, "vasopressor" = a day a site
    # NOT screening on pressors would have called eligible (relaxing NE alone yields a >=2h
    # stable window). Within not_eligible & accrued_12h, stable_window is False by
    # construction and assessable hours exist (else not_assessable), so the split is exact.
    notelig = out["eligibility_status"] == "not_eligible"
    cond_lt12 = notelig & (~out["accrued_12h"])
    cond_vaso = notelig & out["accrued_12h"] & out["stable_window_no_ne"]
    out["notelig_reason"] = np.select(
        [~notelig, cond_lt12, cond_vaso],
        ["", "lt12h_controlled", "failed_vasopressor"],
        default="failed_oxy_peep",
    )

    out.to_parquet(inter / "sbt_eligibility.parquet", index=False)

    # ---- log ----
    n = len(out)
    n_trach = int(cond_trach.sum())
    n_paral = int(cond_paral.sum())
    n_nontrach = n - n_trach
    n_accrued = int((free & out["accrued_12h"]).sum())
    vc = out["eligibility_status"].value_counts()
    n_elig = int(vc.get("eligible", 0))
    n_notassess = int(vc.get("not_assessable", 0))
    n_notelig = int(vc.get("not_eligible", 0))
    rc = out.loc[notelig, "notelig_reason"].value_counts()
    n_rc_lt12 = int(rc.get("lt12h_controlled", 0))
    n_rc_vaso = int(rc.get("failed_vasopressor", 0))
    n_rc_oxy = int(rc.get("failed_oxy_peep", 0))
    assert n_rc_lt12 + n_rc_vaso + n_rc_oxy == n_notelig, \
        f"notelig_reason partition {n_rc_lt12 + n_rc_vaso + n_rc_oxy} != not_eligible {n_notelig}"
    log.info("vent-ICU days:                 %6d", n)
    log.info("  tracheostomized (excluded):  %6d", n_trach)
    log.info("  continuous paralytic (excl): %6d", n_paral)
    log.info("  non-trach vent-ICU days:     %6d", n_nontrach)
    log.info("  >=%dh controlled accrued (non-trach, non-paralytic): %6d (%.1f%% of non-trach)",
             ctrl_min_h, n_accrued, 100 * n_accrued / max(n_nontrach, 1))
    log.info("  not_assessable stability:    %6d", n_notassess)
    log.info("  not_eligible breakdown:      %6d  (<12h controlled %d | vasopressor %d | oxy/PEEP %d)",
             n_notelig, n_rc_lt12, n_rc_vaso, n_rc_oxy)
    log.info("ELIGIBLE SBT-opportunity days: %6d (%.1f%% of non-trach vent-ICU days)",
             n_elig, 100 * n_elig / max(n_nontrach, 1))
    log.info("wrote: sbt_eligibility.parquet")


if __name__ == "__main__":
    main()
