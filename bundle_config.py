"""bundle_config.py — one config + output resolver for the whole multi-site QI bundle.

Collapses the three legacy config systems into one:
  - shared, versioned **definitions** (`definitions/<metric>.json`, identical across sites — the
    clinical thresholds / category-concept lists / knob blocks that `definition_version` tracks), plus
  - a small per-**site** profile (`sites/<site>.json`: data access, timezone, clif_version, unit_labels,
    optional vocabulary overrides, enabled_metrics).

Output is namespaced under `output/<site>/` so multiple sites coexist. The active site is the env var
`CLIF_SITE` (default "uchicago").

Each vertical's config-load + output-dir resolution delegates here. `effective(metric)` returns a dict
shaped EXACTLY like that vertical's legacy config (LPV = flat `clif_data_path`/`filetype`/…;
proning/sat/sbt = nested `primary_dataset`), so downstream stage logic is unchanged and a same-site run
reproduces the previous outputs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # bundle root (bundle_config.py lives at the root)
DEFAULT_SITE = "uchicago"
ALL_METRICS = ["lpv", "proning", "sat", "sbt"]


# --------------------------------------------------------------------------- site / files
def active_site() -> str:
    return (os.environ.get("CLIF_SITE") or DEFAULT_SITE).strip() or DEFAULT_SITE


def load_profile(site: str | None = None) -> dict:
    site = site or active_site()
    p = ROOT / "sites" / f"{site}.json"
    if not p.exists():
        raise FileNotFoundError(
            f"site profile not found: {p} — set CLIF_SITE or create sites/{site}.json "
            f"(copy sites/uchicago.json).")
    return json.loads(p.read_text())


def load_definitions(metric: str) -> dict:
    p = ROOT / "definitions" / f"{metric}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def enabled_metrics(site: str | None = None) -> list[str]:
    return load_profile(site).get("enabled_metrics", list(ALL_METRICS))


# --------------------------------------------------------------------------- output namespacing
def output_root(site: str | None = None) -> Path:
    return ROOT / "output" / (site or active_site())


def metric_output_dir(metric: str, site: str | None = None) -> Path:
    return output_root(site) / "metrics" / metric


def dashboard_dir(site: str | None = None) -> Path:
    return output_root(site) / "dashboard"


def feeds_dir(site: str | None = None) -> Path:
    return output_root(site) / "feeds"


# --------------------------------------------------------------------------- effective config
def _access_flat(prof: dict) -> dict:
    """LPV-shaped access keys (flat)."""
    return {
        "clif_data_path": prof["data_path"],
        "filetype": prof.get("file_format", "parquet"),
        "timezone": prof["timezone"],
        "site": prof["site_id"],
        "clif_version": prof.get("clif_version"),
    }


def _access_nested(prof: dict) -> dict:
    """proning/sat/sbt-shaped access keys (nested under primary_dataset)."""
    return {
        "site": prof["site_id"],
        "timezone": prof["timezone"],
        "primary_dataset": {
            "name": prof.get("dataset_name", f'{prof["site_id"]}_CLIF'),
            "clif_version": prof.get("clif_version"),
            "data_path": prof["data_path"],
            "file_format": prof.get("file_format", "parquet"),
        },
    }


def effective(metric: str, site: str | None = None) -> dict:
    """The per-metric config a vertical consumes: shared definitions ⊕ per-site access/overrides.

    Shape matches that vertical's legacy config file, so stage logic is untouched.
    """
    prof = load_profile(site)
    site = site or active_site()
    cfg = dict(load_definitions(metric))            # knob blocks + _comment_* (shared, versioned)
    cfg.update(_access_flat(prof) if metric == "lpv" else _access_nested(prof))
    cfg["unit_labels"] = prof.get("unit_labels", {}) or {}
    # per-site vocabulary overrides replace a whole knob block (e.g. site-specific mode strings)
    for k, v in ((prof.get("vocabulary_overrides") or {}).get(metric) or {}).items():
        cfg[k] = v
    cfg["output_path"] = str(metric_output_dir(metric, site))
    return cfg
