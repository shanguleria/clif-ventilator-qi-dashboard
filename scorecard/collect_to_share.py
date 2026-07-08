"""collect_to_share.py — assemble the PHI-free deliverables for the active site.

Gathers what a CLIF coordinating center receives into `output/<site>/output_to_share/`:

    output/<site>/output_to_share/
      manifest.json                       # site, code/definition versions, per-metric headline num/den, file inventory
      feeds/<site>_tile_feed_<metric>.json   # the poolable data (num/den at every grain) — STRICTLY PHI-checked
      dashboards/<site>_scorecard.html + <site>_<metric>_dashboard.html   # the published visual deliverables
      methods/<metric>_METHODS.md + scorecard_methods.md                  # the definitions that travel with the numbers

The rest of `output/<site>/` (per-metric intermediates + final parquet) is the PHI / working space — the
consortium's `output_phi` analogue — and is never copied here.

PHI policy: the machine-readable **feeds** must not contain row-level identifiers; each is hard-checked
for `hospitalization_id` / `patient_id` and the run aborts if either appears. Dashboards and methods docs
are the pipeline's already-published, aggregate-only artifacts (methods docs legitimately *name* those
schema fields in prose), so they are copied verbatim. All output/ is gitignored — a site uploads this
folder out-of-band.

Run:  CLIF_SITE=<site> python scorecard/collect_to_share.py   (or via run_bundle.sh / refresh_scorecard.sh)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import bundle_config as _bc  # noqa: E402

FORBIDDEN = ("hospitalization_id", "patient_id")


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def main() -> None:
    site = _bc.active_site()
    metrics = _bc.enabled_metrics(site)
    site_root = _bc.output_root(site)
    share = site_root / "output_to_share"
    dash_dir = _bc.dashboard_dir(site)

    if not site_root.exists():
        sys.exit(f"[to-share] no output for site '{site}' yet — run the pipeline first ({site_root} missing).")

    # Clean rebuild so the folder always reflects the current build exactly.
    if share.exists():
        shutil.rmtree(share)
    (share / "feeds").mkdir(parents=True)
    (share / "dashboards").mkdir(parents=True)
    (share / "methods").mkdir(parents=True)

    manifest: dict = {
        "site_id": None,
        "code_version": _git_sha(),
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "enabled_metrics": list(metrics),
        "metrics": {},
        "files": [],
    }
    included: list[str] = []

    # 1. Feeds — the poolable data. Strict PHI check; abort on violation.
    for m in metrics:
        feed = _bc.metric_output_dir(m, site) / "final" / f"tile_feed_{m}.json"
        if not feed.exists():
            print(f"  [skip] {m}: no tile feed yet ({feed.relative_to(ROOT)}) — metric not built")
            continue
        text = feed.read_text()
        hit = next((tok for tok in FORBIDDEN if tok in text), None)
        if hit:
            sys.exit(f"[to-share] ABORT: {feed} contains '{hit}' — refusing to stage a feed with row-level ids.")
        d = json.loads(text)
        dest = share / "feeds" / f"{site}_tile_feed_{m}.json"
        dest.write_text(text)
        included.append(m)
        manifest["site_id"] = manifest["site_id"] or d.get("provenance", {}).get("site_id")
        c = d.get("headline", {}).get("cells", {}).get("__ALL__", {}).get("all", {})
        manifest["metrics"][m] = {
            "headline_label": d.get("headline", {}).get("label"),
            "num": c.get("num"), "den": c.get("den"),
            "rate": round(c["num"] / c["den"], 4) if c.get("den") else None,
            "provenance": d.get("provenance", {}),
        }
        manifest["files"].append(f"feeds/{dest.name}")

    # 2. Dashboards — published, aggregate-only visual deliverables (copied verbatim).
    for html, tag in ([(dash_dir / "scorecard.html", "scorecard")]
                      + [(dash_dir / f"{m}_dashboard.html", f"{m}_dashboard") for m in metrics]):
        if not html.exists():
            print(f"  [skip] dashboard {html.name} not present")
            continue
        dest = share / "dashboards" / f"{site}_{tag}.html"
        shutil.copyfile(html, dest)
        manifest["files"].append(f"dashboards/{dest.name}")

    # 3. Methods docs — the definitions that must travel with the numbers (copied verbatim).
    for m in included:
        md = ROOT / "metrics" / m / "METHODS.md"
        if md.exists():
            dest = share / "methods" / f"{m}_METHODS.md"
            shutil.copyfile(md, dest)
            manifest["files"].append(f"methods/{dest.name}")
    sc_md = ROOT / "docs" / "scorecard_methods.md"
    if sc_md.exists():
        shutil.copyfile(sc_md, share / "methods" / "scorecard_methods.md")
        manifest["files"].append("methods/scorecard_methods.md")

    (share / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"[to-share] site '{site}' -> {share.relative_to(ROOT)}")
    print(f"  feeds:      {included or '(none — build metrics first)'}")
    print(f"  dashboards: {sorted(p.name for p in (share / 'dashboards').glob('*.html'))}")
    print(f"  files:      {len(manifest['files'])}  |  manifest.json written")


if __name__ == "__main__":
    main()
