"""Unit tests for common.resp_support Level-0 cleaning (no CLIF data — synthetic frames only).

Run:  .venv/bin/python tests/test_resp_support.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import resp_support as rs


def test_fio2_percent_rescaled():
    df = pd.DataFrame({"fio2_set": [21.0, 40.0, 100.0, 50.0]})
    note = rs.fio2_unit_detect(df)
    assert "percent" in note, note
    assert df["fio2_set"].tolist() == [0.21, 0.40, 1.00, 0.50]


def test_fio2_fraction_untouched():
    df = pd.DataFrame({"fio2_set": [0.21, 0.40, 1.0, 0.6]})
    note = rs.fio2_unit_detect(df)
    assert "fraction" in note, note
    assert df["fio2_set"].tolist() == [0.21, 0.40, 1.0, 0.6]


def test_fio2_missing_or_empty_is_noop():
    assert rs.fio2_unit_detect(pd.DataFrame({"x": [1, 2]})) is None
    assert rs.fio2_unit_detect(pd.DataFrame({"fio2_set": [None, None]})) is None


def test_unit_detect_before_clip_preserves_percent_fio2():
    # The load-bearing ordering: rescale THEN clip. A percent 40 must survive as 0.40, not be nulled.
    df = pd.DataFrame({"fio2_set": [40.0, 60.0, 100.0]})
    rs.fio2_unit_detect(df)      # -> 0.40, 0.60, 1.00
    rs.fallback_clip(df)         # spec [0.21, 1.0] keeps all three
    assert df["fio2_set"].notna().all()
    # If we had clipped FIRST, 40/60/100 would all be nulled:
    df2 = pd.DataFrame({"fio2_set": [40.0, 60.0, 100.0]})
    rs.fallback_clip(df2)
    assert df2["fio2_set"].isna().all()


def test_fallback_clip_spec_ranges():
    df = pd.DataFrame({
        "tidal_volume_set": [50.0, 500.0, 5000.0],     # 50 & 5000 out of [100,3000]
        "peep_set": [0.0, 25.0, 40.0],                 # 40 out of spec [0,30]
        "peep_obs": [0.0, 45.0, 60.0],                 # 60 out of [0,50]
        "fio2_set": [0.10, 0.5, 1.0],                  # 0.10 out of [0.21,1.0]
        "plateau_pressure_obs": [-5.0, 30.0, 120.0],   # -5 & 120 out of [0,100]
    })
    rs.fallback_clip(df)
    assert df["tidal_volume_set"].isna().tolist() == [True, False, True]
    assert df["peep_set"].isna().tolist() == [False, False, True]
    assert df["peep_obs"].isna().tolist() == [False, False, True]
    assert df["fio2_set"].isna().tolist() == [True, False, False]
    assert df["plateau_pressure_obs"].isna().tolist() == [True, False, True]


def test_normalize_frame_lowercases_and_tz():
    df = pd.DataFrame({
        "hospitalization_id": [101, 102],
        "device_category": ["IMV", " Nasal Cannula "],
        "mode_category": ["Assist Control-Volume Control", "SIMV"],
        "recorded_dttm": pd.to_datetime(["2023-01-01 00:00", "2023-01-01 01:00"]),
    })
    out = rs.normalize_frame(df, "US/Central")
    assert out["device_category"].tolist() == ["imv", "nasal cannula"]
    assert out["mode_category"].tolist() == ["assist control-volume control", "simv"]
    assert out["hospitalization_id"].tolist() == ["101", "102"]
    assert str(out["recorded_dttm"].dt.tz) == "US/Central"
    # device .str.upper()=="IMV" (LPV's existing check) still fires on the lowercased value:
    assert (out["device_category"].astype("string").str.upper() == "IMV").tolist() == [True, False]
    # Title-case ELIGIBLE_MODES lowercased matches the normalized modes:
    eligible = {m.lower() for m in {"Assist Control-Volume Control", "SIMV", "Pressure Control"}}
    assert out["mode_category"].isin(eligible).tolist() == [True, True]


def test_normalize_frame_tz_convert_when_already_aware():
    df = pd.DataFrame({"recorded_dttm": pd.to_datetime(["2023-06-01 12:00Z"])})
    out = rs.normalize_frame(df, "US/Central")
    assert str(out["recorded_dttm"].dt.tz) == "US/Central"
    assert out["recorded_dttm"].iloc[0].hour == 7  # 12:00 UTC -> 07:00 CDT


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
