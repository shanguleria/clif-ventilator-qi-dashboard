"""Render the proning QI dashboard (self-contained HTML).

CLIF maroon-cream house style (see ~/.claude/templates/dashboard_design_guide.md
and lpv/code/05_scorecard.py). Single self-contained file: logo + figures are
base64-embedded so the dashboard ships as one HTML for any consortium site.

Components:
    - Brand header (logo lockup) + headline metric cards.
    - CONSORT funnel (matplotlib → standalone PNG/SVG + embedded).
    - Time-to-prone cumulative-incidence figure (descriptive; 7-day horizon).
    - Table 1 — proned vs not-proned within the PROSEVA-eligible cohort,
      via the verbatim gtsummary renderer (every cell html.escape'd).
    - Position-table coverage caveat (amber info box).

Inputs:
    output/intermediate/metrics_patient_level.parquet   (per eligible patient)
    output/final/metrics_site_summary.csv               (counts + rates)
    output/final/cohort_flow.csv                         (CONSORT counts)

Output:
    output/final/proning_dashboard.html
    output/final/graphs/cohort_consort.png / .svg
"""

from __future__ import annotations

import base64
import html
import importlib.util
import logging
import re
import sys
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = PROJECT_ROOT / "code"
log = logging.getLogger("proning.dashboard")

# --- palette (CLIF maroon-cream) -------------------------------------------
MAROON, MAROON_D, CREAM = "#8a1f2b", "#6f1622", "#f6efe9"
CARD, INK, MUTED, LINE, BAR = "#fffdfb", "#3a2c2c", "#9a8c86", "#ece1d9", "#efe4dc"
GOOD, WARN, BAD = "#2f7d5b", "#b5852a", "#a23b3b"

CDF_HORIZONS_H = [24, 48, 72, 168]

# Unit slug → display label (dashboard filter; ordered for the dropdown).
UNIT_LABELS = {
    "__ALL__": "All ICUs",
    "medical_icu": "Medical ICU",
    "mixed_cardiothoracic_icu": "Cardiothoracic ICU",
    "surgical_icu": "Surgical ICU",
    "mixed_neuro_icu": "Neuro ICU",
    "general_icu": "General ICU",
    "burn_icu": "Burn ICU",
    "unknown": "Unknown unit",
}
GRAN_LABELS = {"all": "All-time", "year": "Yearly", "month": "Monthly", "week": "Weekly"}


def _period_label(key: str) -> str:
    """month 'YYYY-MM' -> 'Jul 2023'; ISO week 'YYYY-Www' -> 'Week 42 · Oct 2023';
    year 'YYYY' -> unchanged."""
    import datetime as _dt
    try:
        if "-W" in key:
            y, w = key.split("-W")
            d = _dt.date.fromisocalendar(int(y), int(w), 1)
            return f"Week {int(w)} · {d.strftime('%b %Y')}"
        if "-" in key:                       # month YYYY-MM
            return _dt.datetime.strptime(key + "-01", "%Y-%m-%d").strftime("%b %Y")
        return key                            # year YYYY already friendly
    except Exception:
        return key


def _load_cohort_module():
    path = CODE_DIR / "01_build_cohort.py"
    spec = importlib.util.spec_from_file_location("proning_cohort", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Logo / figure embedding
# ---------------------------------------------------------------------------
def _load_logo(p: Path, px: int = 480):
    if not p.exists():
        return None
    try:
        from PIL import Image
        im = Image.open(p).convert("RGBA")
        im.thumbnail((px, px))
        buf = BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# gtsummary renderer (verbatim from the dashboard design guide)
# ---------------------------------------------------------------------------
def render_gtsummary_table_html(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""

    def _header_cell(c: str) -> str:
        c = html.escape(str(c), quote=False)
        c = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", c)
        return c.replace("\n", "<br>")

    header_row = "".join(f"<th>{_header_cell(c)}</th>" for c in df.columns)
    body_rows = []
    for _, row in df.iterrows():
        cells = []
        for i, col in enumerate(df.columns):
            val = row[col]
            raw = "" if (pd.isna(val) or val is None) else str(val)
            if i == 0:
                m_bold = re.match(r"^__(.+)__$", raw)
                if m_bold:
                    cell = f"<strong>{html.escape(m_bold.group(1), quote=False)}</strong>"
                elif raw:
                    cell = f'<span style="padding-left: 20px;">{html.escape(raw, quote=False)}</span>'
                else:
                    cell = ""
            else:
                cell = html.escape(raw, quote=False)
            cells.append(f"<td>{cell}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<table class="results-table" border="0">'
        f"<thead><tr>{header_row}</tr></thead>"
        "<tbody>" + "\n".join(body_rows) + "</tbody></table>"
    )


# ---------------------------------------------------------------------------
# Table 1 seed — render the all-units/all-time slice from the 04 payload
# (the reactive JS rebuilds it per filter from the same component structure).
# ---------------------------------------------------------------------------
def table1_df_from_payload(t: dict) -> pd.DataFrame:
    """gtsummary-shaped DataFrame from one Table-1 component cell (build_dashboard_payload)."""
    cols = [
        "**Characteristic**",
        f"**Overall**\nN = {t['n_all']:,}",
        f"**Proned**\nN = {t['n_proned']:,}",
        f"**Not proned**\nN = {t['n_not']:,}",
        "**p-value**",
    ]

    def med(tr):
        return "—" if tr is None else f"{tr[0]:.1f} ({tr[1]:.1f}, {tr[2]:.1f})"

    def npr(a):
        return f"{a[0]:,} ({100*a[0]/a[1]:.1f}%)" if a and a[1] else "—"

    def pval(p):
        return "" if p is None else ("<0.001" if p < 0.001 else f"{p:.3f}")

    rows = []
    for r in t["rows"]:
        if r["kind"] == "cont":
            rows.append([f"__{r['label']}__", med(r["all"]), med(r["proned"]), med(r["not"]), pval(r["p"])])
        elif r["kind"] == "bin":
            rows.append([f"__{r['label']}__", npr(r["all"]), npr(r["proned"]), npr(r["not"]), pval(r["p"])])
        else:
            rows.append([f"__{r['label']}__", "", "", "", pval(r["p"])])
            for lv in r["levels"]:
                rows.append([lv["label"], npr(lv["all"]), npr(lv["proned"]), npr(lv["not"]), ""])
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Sliced metrics → embedded JS (unit × granularity × period)
# ---------------------------------------------------------------------------
def build_slices_js(slices: pd.DataFrame) -> dict:
    """Nest the slice table into SLICES[unit][granularity][period] = {counts}.
    Counts only — no ids/dates — so the embed stays PHI-free."""
    out: dict = {}
    def num(v):
        return None if pd.isna(v) else round(float(v), 1)
    for r in slices.itertuples(index=False):
        cell = {
            "den": int(r.n_eligible), "proned": int(r.n_ever_proned),
            "documented": int(r.n_documented),
            "ttp_median": num(r.ttp_median_h), "ttp_q1": num(r.ttp_q1_h), "ttp_q3": num(r.ttp_q3_h),
            "fsd_median": num(r.fsd_median_h), "fsd_q1": num(r.fsd_q1_h), "fsd_q3": num(r.fsd_q3_h),
        }
        out.setdefault(r.unit, {}).setdefault(r.granularity, {})[r.period] = cell
    return out


# Reactive filter + trend logic. Plain string (no f-string) so braces are literal;
# it reads three Python-injected globals: SLICES, CFG, and PAYLOAD (distributions + Table 1).
FILTER_JS = r"""
(function(){
  const $ = id => document.getElementById(id);
  const min = CFG.smallCellMin;
  const MAROON = "#8a1f2b", MAROON_D = "#6f1622", INK = "#3a2c2c", MUTED = "#7a6c66", LINE = "#ece1d9";
  const state = {unit: "__ALL__", gran: "all", period: "__all__", unitDim: "type"};

  const unitSel = $("f-unit"), periodSel = $("f-period"), periodWrap = $("f-period-wrap");
  const plabel = p => (CFG.periodLabels && CFG.periodLabels[p]) || p;
  const esc = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  // "Group ICUs by": location_type (default) vs specific unit (location_name). Swaps the Unit
  // dropdown's options; SLICES holds both grains.
  const groupSel = $("f-group");
  function unitsForDim(){ return state.unitDim === "name" ? (CFG.nameOrder || ["__ALL__"]) : CFG.unitOrder; }
  function rebuildUnitOptions(){
    const list = unitsForDim();
    unitSel.innerHTML = list.map(u => '<option value="' + u + '">' + (CFG.unitLabels[u] || u) + '</option>').join('');
    state.unit = "__ALL__"; unitSel.value = "__ALL__";
  }

  function periodsFor(unit, gran){
    if (gran === "all") return [];
    return Object.keys((SLICES[unit] || {})[gran] || {}).sort();
  }
  function fillPeriods(){
    const ps = periodsFor(state.unit, state.gran);
    if (!ps.length){ periodWrap.style.display = "none"; return; }
    periodWrap.style.display = "";
    let opts = '<option value="__all__">All periods</option>';
    for (const p of ps) opts += '<option value="' + p + '">' + plabel(p) + '</option>';
    periodSel.innerHTML = opts;
    if (!ps.includes(state.period)) state.period = "__all__";
    periodSel.value = state.period;
  }
  function cell(){
    const u = SLICES[state.unit] || {};
    if (state.gran === "all" || state.period === "__all__") return (u.all || {}).all || null;
    return (u[state.gran] || {})[state.period] || null;
  }
  // Heavy panels (duration / time-to-prone / Table 1) carry only all/year/month. Resolve the
  // requested slice, falling back to the unit's all-time cell when the grain is too granular
  // (weekly) or the specific period was not embedded.
  function heavy(kind){
    const root = (PAYLOAD[kind] || {})[state.unit] || {};
    if (state.gran !== "all" && state.gran !== "week" && state.period !== "__all__"
        && root[state.gran] && root[state.gran][state.period]) {
      return {cell: root[state.gran][state.period], exact: true};
    }
    return {cell: (root.all || {}).all || null, exact: false};
  }
  function heavyCtx(exact){
    if (exact) return CFG.unitLabels[state.unit] + " · " + plabel(state.period);
    const granular = state.gran !== "all" && state.period !== "__all__";
    return CFG.unitLabels[state.unit] + " · all time" +
      (granular ? " (period too granular for this panel)" : "");
  }
  function pct(x, dp){ return x == null ? "—" : (100*x).toFixed(dp == null ? 0 : dp) + "%"; }
  function hrs(x){ return x == null ? "—" : (x >= 10 ? Math.round(x) : x.toFixed(1)) + " h"; }
  function setBig(el, txt, small){ el.innerHTML = txt; el.classList.toggle("dim", !!small); }

  function render(){
    const c = cell();
    const ctx = CFG.unitLabels[state.unit] + " · " +
      (state.gran === "all" || state.period === "__all__" ? "all time" : plabel(state.period));
    if (!c){
      ["c1","c2","c3","c4"].forEach(k => { setBig($(k+"big"), "—", false); $(k+"sub").textContent = ""; });
      $("c1sub").textContent = ctx; $("c2sub").textContent = "no eligible patients";
      $("smallnote").style.display = "none"; drawTrend(); drawDuration(); drawTTP(); drawTable1(); return;
    }
    const small = c.den < min;
    setBig($("c1big"), c.den.toLocaleString(), false); $("c1sub").textContent = ctx;
    setBig($("c2big"), pct(c.den ? c.proned/c.den : null), small);
    $("c2sub").textContent = c.proned.toLocaleString() + " of " + c.den.toLocaleString() + " eligible";
    setBig($("c3big"), c.fsd_median == null ? "—" : hrs(c.fsd_median), small);
    $("c3sub").textContent = c.fsd_median == null ? "no IMV-era proned" :
      "IQR " + hrs(c.fsd_q1) + "–" + hrs(c.fsd_q3) + ", IMV-era proned";
    setBig($("c4big"), c.ttp_median == null ? "—" : Math.round(c.ttp_median) + " h", small);
    $("c4sub").textContent = c.ttp_median == null ? "no IMV-era proned" :
      "IQR " + Math.round(c.ttp_q1) + "–" + Math.round(c.ttp_q3) + " h, IMV-era proned";
    $("smallnote").style.display = small ? "block" : "none";
    drawTrend(); drawDuration(); drawTTP(); drawTable1();
  }

  function drawTrend(){
    const tg = state.gran === "all" ? "year" : state.gran;
    const series = (SLICES[state.unit] || {})[tg] || {};
    const keys = Object.keys(series).sort();
    $("trendTitle").textContent = "Ever-proned rate by " +
      ({year:"year", month:"month", week:"week"}[tg]) + " · " + CFG.unitLabels[state.unit];
    const host = $("trend");
    if (!keys.length){ host.innerHTML = '<div class="muted">No periods in this slice.</div>'; return; }
    const monthly = tg === "month";
    const slot = monthly ? 46 : (keys.length > 40 ? 15 : (keys.length > 15 ? 34 : 56));
    const pad = {l:40, r:12, t:14, b:56}, ih = 150;
    const W = pad.l + pad.r + keys.length*slot, H = pad.t + ih + pad.b;
    let maxr = 0.05; for (const k of keys){ const d = series[k]; if (d.den) maxr = Math.max(maxr, d.proned/d.den); }
    const top = Math.max(0.1, Math.ceil(maxr*100/10)*10/100);
    const lblStep = monthly ? 1 : Math.ceil(keys.length/24);
    // y grid (0 / mid / top): faint dashed lines behind the bars + % labels at left
    let svg = '';
    [0, 0.5, 1].forEach(f => {
      const gy = pad.t + ih*(1 - f);
      svg += '<line x1="'+pad.l+'" y1="'+gy+'" x2="'+(W-pad.r)+'" y2="'+gy+'" stroke="'+(f===0?"#d8c7bf":"#ece1d9")+'"'+(f===0?"":' stroke-dasharray="2 3"')+'/>';
      svg += '<text x="'+(pad.l-7)+'" y="'+(gy+3.5)+'" font-size="10" text-anchor="end" fill="#7a6c66">'+(100*top*f).toFixed(0)+'%</text>';
    });
    svg += '<line x1="'+pad.l+'" y1="'+pad.t+'" x2="'+pad.l+'" y2="'+(pad.t+ih)+'" stroke="#d8c7bf"/>';
    keys.forEach((k, i) => {
      const d = series[k], r = d.den ? d.proned/d.den : 0;
      const x = pad.l + i*slot + slot*0.16, w = slot*0.68;
      const yT = pad.t + ih*(1 - r/top), hT = ih*(r/top);
      const dim = d.den < min, sel = (k === state.period);
      const cD = dim ? "#b39a93" : "#8a1f2b";
      svg += '<g><title>' + k + "\n" + d.proned + "/" + d.den + " proned (" + (100*r).toFixed(0) + "%)" +
             (dim ? "  — n small" : "") + '</title>';
      svg += '<rect x="'+x+'" y="'+yT+'" width="'+w+'" height="'+hT+'" fill="'+cD+'"' + (sel ? ' stroke="#3a2c2c" stroke-width="1.5"' : '') + '/></g>';
      if (i % lblStep === 0){
        const lab = monthly ? (k.slice(5,7) + "/" + k.slice(2,4))
                            : (tg === "week" ? k.replace(/^\d{4}-/, "") : k);
        const cx = x + w/2, axisY = pad.t + ih, ly = axisY + 11;
        // faint per-tick separator so months read distinctly
        svg += '<line x1="'+cx+'" y1="'+pad.t+'" x2="'+cx+'" y2="'+(axisY+4)+'" stroke="#f1e7e0"/>';
        svg += '<line x1="'+cx+'" y1="'+axisY+'" x2="'+cx+'" y2="'+(axisY+4)+'" stroke="#cbb8b0"/>';
        svg += '<text x="'+cx+'" y="'+ly+'" font-size="10" text-anchor="end" fill="#6b5d57" transform="rotate(-35 '+cx+' '+ly+')">'+lab+'</text>';
      }
    });
    host.innerHTML = '<svg viewBox="0 0 '+W+' '+H+'" height="'+H+'" width="'+W+'" style="max-width:none">'+svg+'</svg>';
  }

  // Item 2 — first-prone-session duration histogram + median/IQR (reactive).
  function drawDuration(){
    const {cell: d, exact} = heavy("dist");
    const host = $("duration"), labels = PAYLOAD.fsd_bin_labels;
    $("durCtx").textContent = heavyCtx(exact);
    if (!d || !d.n_fsd){
      host.innerHTML = '<div class="muted">No proned patients with a charted first-session duration in this slice.</div>';
      $("durStat").textContent = ""; return;
    }
    const hist = d.fsd_hist, nmax = Math.max(1, ...hist);
    const small = d.n_proned < min;
    const slot = 74, pad = {l:40, r:14, t:14, b:42}, ih = 150;
    const W = pad.l + pad.r + labels.length*slot, H = pad.t + ih + pad.b;
    let svg = '';
    [0, 0.5, 1].forEach(f => {
      const gy = pad.t + ih*(1 - f);
      svg += '<line x1="'+pad.l+'" y1="'+gy+'" x2="'+(W-pad.r)+'" y2="'+gy+'" stroke="'+(f===0?"#d8c7bf":"#ece1d9")+'"'+(f===0?"":' stroke-dasharray="2 3"')+'/>';
      svg += '<text x="'+(pad.l-7)+'" y="'+(gy+3.5)+'" font-size="10" text-anchor="end" fill="#7a6c66">'+Math.round(nmax*f)+'</text>';
    });
    labels.forEach((lab, i) => {
      const n = hist[i], x = pad.l + i*slot + slot*0.14, w = slot*0.72;
      const h = ih*(n/nmax), y = pad.t + ih - h;
      svg += '<g><title>' + lab + ": " + n + " patients</title>";
      svg += '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="'+(small?"#cdb3b3":MAROON)+'"/>';
      if (n) svg += '<text x="'+(x+w/2)+'" y="'+(y-4)+'" font-size="10" text-anchor="middle" fill="'+INK+'">'+n+'</text>';
      svg += '<text x="'+(x+w/2)+'" y="'+(pad.t+ih+15)+'" font-size="10" text-anchor="middle" fill="#6b5d57">'+lab+'</text></g>';
    });
    host.innerHTML = '<svg viewBox="0 0 '+W+' '+H+'" height="'+H+'" width="'+W+'" style="max-width:100%">'+svg+'</svg>';
    $("durStat").innerHTML = "n = " + d.n_fsd + " proned · median <b>" + hrs(d.fsd_med) +
      "</b> (IQR " + hrs(d.fsd_q1) + "–" + hrs(d.fsd_q3) + "; p10–p90 " + hrs(d.fsd_p10) + "–" + hrs(d.fsd_p90) + ")" +
      (small ? " — n small, interpret with caution" : "");
  }

  // Item 4 — two side-by-side cumulative curves, both clocked from T₀ (ARDS onset), reactive:
  //  left  = % of ALL eligible proned by hour (the QI incidence; caps at the ever-proned rate);
  //  right = % of the PRONED proned by hour (rises toward 100% — "of those proned, how long?").
  //  Same numerator (ttp0_cum), different denominator, so the two are directly comparable.
  //  A dotted vertical line marks the median T_eligible (the decision-point) on the T₀ axis,
  //  labelled with the % proned by T_eligible (per patient's own T_eligible).
  // linear interpolation of a cumulative curve (matches the drawn polyline) at an arbitrary x
  function interpCum(cum, grid, x){
    if (x <= grid[0]) return cum[0];
    const last = grid.length - 1;
    if (x >= grid[last]) return cum[last];
    for (let i = 1; i <= last; i++){
      if (x <= grid[i]){ const t = (x-grid[i-1])/(grid[i]-grid[i-1]); return cum[i-1] + t*(cum[i]-cum[i-1]); }
    }
    return cum[last];
  }
  function ttpCurveSVG(cum, xlabel, ytopFixed, small, te){
    const grid = PAYLOAD.ttp_grid_h, xmax = grid[grid.length-1];
    const pad = {l:40, r:34, t:24, b:38}, iw = 300, ih = 168;
    const W = pad.l + iw + pad.r, H = pad.t + ih + pad.b;
    const ytop = ytopFixed || Math.max(20, Math.ceil(Math.max(...cum)*1.25/10)*10);
    const X = h => pad.l + iw*(Math.min(h, xmax)/xmax), Y = v => pad.t + ih*(1 - v/ytop);
    function mark(x, y, txt, anchor, dx, dy){
      return '<circle cx="'+x+'" cy="'+y+'" r="3.3" fill="'+MAROON_D+'"/>'
           + '<text x="'+(x+dx)+'" y="'+(y+dy)+'" font-size="9" font-weight="700" text-anchor="'+anchor+'" fill="'+MAROON_D+'">'+txt+'</text>';
    }
    let s = '';
    [0,0.5,1].forEach(f => {
      const gy = pad.t + ih*(1-f);
      s += '<line x1="'+pad.l+'" y1="'+gy+'" x2="'+(W-pad.r)+'" y2="'+gy+'" stroke="'+(f===0?"#d8c7bf":"#ece1d9")+'"'+(f===0?"":' stroke-dasharray="2 3"')+'/>';
      s += '<text x="'+(pad.l-7)+'" y="'+(gy+3.5)+'" font-size="10" text-anchor="end" fill="#7a6c66">'+Math.round(ytop*f)+'%</text>';
    });
    [0,24,48,72,96,120,144,168].forEach(h => {
      if (h>xmax) return; const x = X(h);
      s += '<line x1="'+x+'" y1="'+(pad.t+ih)+'" x2="'+x+'" y2="'+(pad.t+ih+4)+'" stroke="#cbb8b0"/>';
      s += '<text x="'+x+'" y="'+(pad.t+ih+16)+'" font-size="9.5" text-anchor="middle" fill="#6b5d57">'+h+'</text>';
    });
    // T_eligible reference line (drawn before the curve so the curve sits on top).
    // Line labelled with HORIZONTAL text at the top (kept out of the curve area so it
    // doesn't collide with the intercept dot/label where the line meets the curve).
    let teX = null, teVal = null;
    if (te && te.x != null){
      teX = X(te.x); teVal = interpCum(cum, grid, te.x);
      const clamp = te.x > xmax;
      const anc = teX < pad.l + 46 ? 'start' : (teX > W - pad.r - 46 ? 'end' : 'middle');
      s += '<line x1="'+teX+'" y1="'+pad.t+'" x2="'+teX+'" y2="'+(pad.t+ih)+'" stroke="#8a7a72" stroke-width="1.1" stroke-dasharray="4 3"/>';
      s += '<text x="'+teX+'" y="'+(pad.t-9)+'" font-size="9" font-weight="600" text-anchor="'+anc+'" fill="#7a6c66">T_eligible · med '+Math.round(te.x)+'h'+(clamp?'+':'')+'</text>';
    }
    let p = ''; grid.forEach((h,i) => { p += (i?'L':'M')+X(h)+' '+Y(cum[i]); });
    s += '<path d="'+p+'" fill="none" stroke="'+(small?"#cdb3b3":MAROON)+'" stroke-width="2.2"/>';
    [48,72,168].forEach(h => {
      const idx = grid.indexOf(h); if (idx<0) return; const x = X(h), y = Y(cum[idx]);
      const end = (h === xmax);   // anchor the rightmost label inward so it isn't clipped
      s += mark(x, y, cum[idx].toFixed(0)+'%', end?'end':'start', end?-5:4, -6);
    });
    // intercept dot at the T_eligible reference line
    if (teX != null) s += mark(teX, Y(teVal), Math.round(teVal)+'%', 'middle', 0, -7);
    s += '<text x="'+(pad.l+iw/2)+'" y="'+(H-3)+'" font-size="10" text-anchor="middle" fill="#6b5d57">'+xlabel+'</text>';
    return '<svg viewBox="0 0 '+W+' '+H+'" width="'+W+'" height="'+H+'" style="max-width:100%">'+s+'</svg>';
  }
  function drawTTP(){
    const {cell: d, exact} = heavy("dist");
    $("ttpCtx").textContent = heavyCtx(exact);
    const elig = $("ttpElig"), pron = $("ttpProned");
    if (!d || !d.den){
      elig.innerHTML = '<div class="muted">No eligible patients in this slice.</div>';
      pron.innerHTML = ""; $("ttpStat").textContent = ""; return;
    }
    const small = d.den < min, X = "Hours since T₀ (ARDS onset)";
    const cumElig = d.ttp0_cum.map(n => 100*n/d.den);                            // % of eligible, since T₀
    const cumPron = d.ttp0_cum.map(n => d.n_proned ? 100*n/d.n_proned : 0);      // % of proned, since T₀
    elig.innerHTML = ttpCurveSVG(cumElig, X, null, small, {x: d.te_off_med});
    pron.innerHTML = d.n_proned ? ttpCurveSVG(cumPron, X, 100, small, {x: d.te_off_med})
                                : '<div class="muted">No proned patients in this slice.</div>';
    const awake = d.n_awake ? '<br><span style="color:#7a6c66">⚑ ' + d.n_awake + ' of ' + d.n_ever_proned +
      ' ever-proned were <b>awake / pre-intubation proned</b> (proned before T₀ — mostly COVID-era HFNC/NIV ' +
      'proning of severely hypoxemic patients, intubated later); excluded from the IMV-era timing above.</span>' : '';
    $("ttpStat").innerHTML =
      "<b>Time to first prone, among the " + d.n_proned + " IMV-era proned (first prone ≥ T₀):</b> from T₀ " +
      hrs(d.ttp0_med) + " (IQR " + hrs(d.ttp0_q1) + "–" + hrs(d.ttp0_q3) + ") · from T_eligible " +
      hrs(d.ttp_med) + " (IQR " + hrs(d.ttp_q1) + "–" + hrs(d.ttp_q3) + ").<br><span style=\"color:#7a6c66\">" +
      "The dotted line is the median T_eligible (~" + Math.round(d.te_off_med) + " h after onset). A " +
      "<b>larger</b> from-T₀ median than the from-T_eligible median = proning later in the ARDS course " +
      "(later / rescue); a <b>smaller</b> one = proactive proning soon after onset.</span>" + awake;
  }

  // Item 5 — reactive Table 1 (proned vs not proned) rebuilt from embedded components.
  function fmtMed(t){ return t == null ? "—" : t[0].toFixed(1) + " (" + t[1].toFixed(1) + ", " + t[2].toFixed(1) + ")"; }
  function fmtNP(a){ return (a && a[1]) ? a[0].toLocaleString() + " (" + (100*a[0]/a[1]).toFixed(1) + "%)" : "—"; }
  function fmtP(p){ return p == null ? "" : (p < 0.001 ? "<0.001" : p.toFixed(3)); }
  function tr(c0, c1, c2, c3, c4, bold){
    const l = bold ? '<strong>'+esc(c0)+'</strong>' : '<span style="padding-left:20px">'+esc(c0)+'</span>';
    return '<tr><td>'+l+'</td><td>'+c1+'</td><td>'+c2+'</td><td>'+c3+'</td><td>'+(c4||'')+'</td></tr>';
  }
  function buildTable1(t){
    const hdr = ['<strong>Characteristic</strong>',
      '<strong>Overall</strong><br>N = '+t.n_all.toLocaleString(),
      '<strong>Proned</strong><br>N = '+t.n_proned.toLocaleString(),
      '<strong>Not proned</strong><br>N = '+t.n_not.toLocaleString(),
      '<strong>p-value</strong>'];
    let body = '';
    for (const r of t.rows){
      if (r.kind === "cont") body += tr(r.label, fmtMed(r.all), fmtMed(r.proned), fmtMed(r.not), fmtP(r.p), true);
      else if (r.kind === "bin") body += tr(r.label, fmtNP(r.all), fmtNP(r.proned), fmtNP(r.not), fmtP(r.p), true);
      else { body += tr(r.label, '', '', '', fmtP(r.p), true);
        for (const lv of r.levels) body += tr(lv.label, fmtNP(lv.all), fmtNP(lv.proned), fmtNP(lv.not), '', false); }
    }
    return '<table class="results-table" border="0"><thead><tr>' +
      hdr.map(h => '<th>'+h+'</th>').join('') + '</tr></thead><tbody>' + body + '</tbody></table>';
  }
  function drawTable1(){
    const {cell: t, exact} = heavy("table1");
    $("t1Ctx").textContent = heavyCtx(exact);
    $("t1n").textContent = t ? t.n_all.toLocaleString() : "0";
    const host = $("table1");
    if (!t || !t.n_all){ host.innerHTML = '<div class="muted">No eligible patients in this slice.</div>'; return; }
    const small = t.n_all < min;
    host.innerHTML = (small ? '<div class="smallnote" style="display:block;margin:0 0 10px">† Fewer than '
      + min + ' eligible patients in this slice — interpret with caution.</div>' : '') + buildTable1(t);
  }

  // wire controls
  unitSel.onchange = () => { state.unit = unitSel.value; fillPeriods(); render(); };
  if (groupSel) groupSel.onchange = () => {
    state.unitDim = groupSel.value; rebuildUnitOptions(); fillPeriods(); render();
  };
  periodSel.onchange = () => { state.period = periodSel.value; render(); };
  document.querySelectorAll("#f-gran button").forEach(b => b.onclick = () => {
    document.querySelectorAll("#f-gran button").forEach(x => x.classList.remove("on"));
    b.classList.add("on"); state.gran = b.dataset.g; state.period = "__all__"; fillPeriods(); render();
  });
  fillPeriods(); render();
})();
"""


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------
def _card(big, label, sub):
    return (f'<div class="mcard"><div class="big">{big}</div>'
            f'<div class="mlab">{html.escape(label)}</div>'
            f'<div class="msub">{html.escape(sub)}</div></div>')


def _card_slot(cid, label, big0, sub0):
    """Card with an id'd big/sub so the filter JS can rewrite it; seeded with the
    all-units/all-time values so it reads correctly even before JS runs."""
    return (f'<div class="mcard"><div class="big" id="{cid}big">{big0}</div>'
            f'<div class="mlab">{html.escape(label)}</div>'
            f'<div class="msub" id="{cid}sub">{html.escape(sub0)}</div></div>')


def build_controls(slices: pd.DataFrame) -> str:
    typ = slices[slices["dim"] == "type"] if "dim" in slices.columns else slices
    units = [u for u in UNIT_LABELS if u in set(typ["unit"])]
    opts = "".join(
        f'<option value="{html.escape(u)}">{html.escape(UNIT_LABELS[u])}</option>' for u in units)
    name_units = sorted(set(slices.loc[(slices["dim"] == "name") & (slices["unit"] != "unknown"), "unit"])) \
        if "dim" in slices.columns else []
    splits = "dim" in slices.columns and slices[slices["dim"] == "name"].groupby("parent")["unit"].nunique().gt(1).any()
    group_ctl = ('<label class="ctl">Group ICUs by<select id="f-group">'
                 '<option value="type">ICU type</option>'
                 f'<option value="name">Specific unit ({len(name_units)})</option>'
                 '</select></label>') if (name_units and splits) else ""
    gran_btns = "".join(
        '<button data-g="{g}"{on}>{lab}</button>'.format(
            g=g, on=' class="on"' if g == "all" else "", lab=html.escape(GRAN_LABELS[g]))
        for g in ("all", "year", "month", "week"))
    return (
        '<div class="controls">'
        + group_ctl +
        f'<label class="ctl">Unit<select id="f-unit">{opts}</select></label>'
        f'<div class="ctl">Time<div class="seg" id="f-gran">{gran_btns}</div></div>'
        '<label class="ctl" id="f-period-wrap" style="display:none">Period<select id="f-period"></select></label>'
        '</div>'
    )


def build_html(logo_uri, controls_html, cards_html, smallnote_html, trend_html, caveat,
               table1_html, table_n, site, generated, script_html) -> str:
    brand = (f'<img src="{logo_uri}" alt="CLIF">' if logo_uri
             else '<span style="font-size:28px;font-weight:800;color:#8a1f2b">CLIF</span>')
    css = f"""
:root{{--maroon:{MAROON};--maroon-d:{MAROON_D};--cream:{CREAM};--card:{CARD};--ink:{INK};
--muted:{MUTED};--line:{LINE};--bar:{BAR};--good:{GOOD};--warn:{WARN};--bad:{BAD};}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:Inter,-apple-system,'Segoe UI',system-ui,sans-serif;
background:var(--cream);color:var(--ink);font-size:14px;line-height:1.55;}}
.wrap{{max-width:1180px;margin:0 auto;padding:30px 40px 56px;background:var(--card);
box-shadow:0 3px 16px rgba(120,30,40,.06);}}
header.top{{display:flex;align-items:center;gap:18px;border-bottom:1px solid var(--line);
padding-bottom:18px;margin-bottom:8px;}}
header.top img{{height:72px;width:auto;display:block;flex:0 0 auto;}}
.backlink{{display:inline-block;font-size:12.5px;color:var(--maroon);text-decoration:none;font-weight:700;margin-bottom:4px;}}
.backlink:hover{{text-decoration:underline;}}
h1{{font-size:27px;font-weight:800;color:var(--maroon-d);margin:0;letter-spacing:-.3px;}}
.sub{{color:var(--muted);font-size:13px;margin-top:3px;}}
h2{{font-size:19px;font-weight:700;color:var(--maroon-d);border-bottom:1px solid var(--line);
padding-bottom:6px;margin:0 0 16px;}}
.section{{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:26px 28px;margin:0 0 34px;box-shadow:0 3px 10px rgba(120,30,40,.05);}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:24px 0 34px;}}
.mcard{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 16px;
text-align:center;box-shadow:0 3px 10px rgba(120,30,40,.05);}}
.mcard .big{{font-size:32px;font-weight:800;color:var(--maroon);font-variant-numeric:tabular-nums;line-height:1.05;}}
.mcard .big.dim{{color:var(--muted);}}
.mcard .mlab{{font-size:13px;font-weight:700;color:var(--ink);margin-top:5px;}}
.mcard .msub{{font-size:11.5px;color:var(--muted);margin-top:3px;min-height:28px;}}
.controls{{display:flex;flex-wrap:wrap;align-items:flex-end;gap:18px;margin:22px 0 4px;}}
.ctl{{display:flex;flex-direction:column;gap:5px;font-size:11px;font-weight:700;
color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}}
.ctl select{{font-size:13px;font-weight:600;color:var(--ink);background:var(--card);
border:1px solid var(--line);border-radius:9px;padding:7px 10px;min-width:150px;
font-family:inherit;text-transform:none;letter-spacing:0;}}
.seg{{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden;}}
.seg button{{font:inherit;font-size:13px;font-weight:600;border:0;background:var(--card);
color:var(--ink);padding:7px 13px;cursor:pointer;border-left:1px solid var(--line);
text-transform:none;letter-spacing:0;}}
.seg button:first-child{{border-left:0;}}
.seg button.on{{background:var(--maroon);color:#fff;}}
.smallnote{{display:none;font-size:11.5px;color:var(--warn);margin:-22px 0 26px;}}
.trend-wrap{{overflow-x:auto;padding-bottom:4px;}}
.muted{{color:var(--muted);font-size:13px;}}
.fig{{text-align:center;margin:6px 0;}}
.fig img{{max-width:100%;height:auto;border-radius:8px;}}
.fig-caption{{font-size:13px;color:var(--muted);margin-top:8px;text-align:left;}}
.amber{{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:10px;
padding:14px 18px;font-size:13px;margin:0 0 22px;}}
.amber b{{color:#7a3a0a;}}
table.results-table{{border-collapse:collapse;width:auto;font-size:13px;margin-top:6px;}}
table.results-table th{{background:var(--cream);color:var(--maroon-d);text-align:left;
padding:9px 12px;border-bottom:2px solid var(--maroon-d);font-weight:700;}}
table.results-table td{{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left;
vertical-align:top;}}
table.results-table tbody tr:nth-child(even){{background:#faf5f1;}}
.statline{{font-size:13px;color:var(--ink);margin-top:10px;}}
.scopebadge{{font-size:12px;font-weight:600;color:var(--muted);}}
.ttp-grid{{display:flex;gap:26px;flex-wrap:wrap;align-items:flex-start;}}
.ttp-col{{flex:1 1 320px;min-width:300px;}}
.ttp-title{{font-size:13.5px;font-weight:700;color:var(--ink);margin-bottom:4px;}}
footer{{margin-top:30px;color:var(--muted);font-size:11.5px;text-align:center;
border-top:1px solid var(--line);padding-top:14px;}}
"""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARDS Proning QI — {html.escape(site)}</title><style>{css}</style></head><body>
<div class="wrap">
  <header class="top">{brand}
    <div><a class="backlink" href="scorecard.html">← CLIF ICU Ventilator QI Bundle</a>
    <h1>ARDS Proning — Quality-of-Care</h1>
    <div class="sub">PROSEVA-strict proning eligibility, timeliness &amp; duration · {html.escape(site)} ·
    generated {html.escape(generated)}</div></div>
  </header>

  {controls_html}
  <div class="cards">{cards_html}</div>
  {smallnote_html}

  {caveat}

  <div class="section"><h2>Proning rate over time</h2>
    <div class="fig-caption" id="trendTitle"></div>
    <div class="trend-wrap">{trend_html}</div>
    <div class="fig-caption">Each bar is one period for the selected unit — the ever-proned rate
    (proned / eligible). Bars are grayed when the period has fewer than the small-cell threshold of
    eligible patients. Use the controls above to switch unit and granularity; pick a Period to drill
    the panels below to a single bucket. Monthly labels are MM/YY.</div>
  </div>

  <div class="section"><h2>How long is the first prone session?
    <span class="scopebadge" id="durCtx"></span></h2>
    <div class="fig-caption">Distribution of the duration of each proned patient's <b>first</b> prone
    session (start → end of session 1), among PROSEVA-eligible patients with a charted prone session
    in the selected slice. Reacts to the Unit and Period controls above.</div>
    <div class="trend-wrap" id="duration"></div>
    <div class="statline" id="durStat"></div>
  </div>

  <div class="section"><h2>Time to first prone
    <span class="scopebadge" id="ttpCtx"></span></h2>
    <div class="fig-caption">First <b>IMV-era</b> prone session (first prone at/after T₀, on invasive
    ventilation). Awake / pre-intubation prones (before T₀) are flagged in the note below and excluded
    here. Descriptive; never-proned are event-free, not censored. Reacts to the Unit and Period controls.</div>
    <div class="ttp-grid">
      <div class="ttp-col">
        <div class="ttp-title">Cumulative incidence — % of <b>all eligible</b> IMV-era proned</div>
        <div class="trend-wrap" id="ttpElig"></div>
        <div class="fig-caption">Clocked from <b>T₀ (ARDS onset)</b>. The QI process measure; caps at
        the IMV-era-proned rate (slightly below the ever-proned headline, the gap = awake-only proned).
        Dotted line = median <b>T_eligible</b> (decision-point), with the % proned by then.</div>
      </div>
      <div class="ttp-col">
        <div class="ttp-title">Among the <b>IMV-era proned</b> — % proned by time</div>
        <div class="trend-wrap" id="ttpProned"></div>
        <div class="fig-caption">Same clock (<b>T₀</b>) and numerator, as a fraction of the IMV-era
        proned, so it rises toward 100% — "of those proned, how long did it take?". A long tail is late
        <i>rescue</i> proning. Dotted line = median T_eligible.</div>
      </div>
    </div>
    <div class="statline" id="ttpStat"></div>
  </div>

  <div class="section"><h2>Table 1 — Baseline characteristics (eligible n = <span id="t1n">{table_n:,}</span>)
    <span class="scopebadge" id="t1Ctx"></span></h2>
    <div class="fig-caption">PROSEVA-eligible patients, stratified by whether a prone session was
    ever documented. Continuous variables: median (Q1, Q3), Kruskal–Wallis. Categorical: n (%),
    χ². Physiology and treatments (P/F, FiO₂, PEEP, set tidal volume, vasopressors, neuromuscular
    blockade) are reported at <b>both</b> anchors so you can see how the cohort changes between ARDS
    onset and the proning decision-point. "Not proned" includes patients with no position record
    (see coverage note above). Reacts to the Unit and Period controls above.</div>
    <div class="amber" style="margin:6px 0 14px">
      <b>T₀</b> — ARDS onset: the first arterial blood gas in the ICU on invasive ventilation meeting
      the Berlin moderate–severe screen (PEEP&nbsp;≥&nbsp;5, FiO₂&nbsp;≥&nbsp;0.4, P/F&nbsp;≤&nbsp;300).
      The cohort entry point.<br>
      <b>T_eligible</b> — proning decision-point: 12&nbsp;h after the first ABG meeting the stricter
      PROSEVA-severe thresholds (PEEP&nbsp;≥&nbsp;5, FiO₂&nbsp;≥&nbsp;0.6, P/F&nbsp;≤&nbsp;150), with
      severity re-confirmed by a later qualifying ABG and no extubation during the 12&nbsp;h
      stabilization window. The point at which prone positioning should be considered; the
      time-to-prone clock starts here.
    </div>
    <div id="table1">{table1_html}</div>
  </div>

  <footer>CLIF consortium · multi-site federated QI · proning vertical · row-level data never
  leaves the site — only counts and rates are shared.</footer>
</div>
{script_html}
</body></html>"""


def main() -> None:
    cohort_mod = _load_cohort_module()
    cohort_mod._ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cohort_mod.LOGS_DIR / "05_dashboard.log", mode="w"),
        ],
    )
    cfg = cohort_mod.load_config(cohort_mod.CONFIG_PATH)
    site = cfg.get("site", "unknown")
    inter, final = cohort_mod.INTERMEDIATE_DIR, cohort_mod.FINAL_DIR

    pl_path = inter / "metrics_patient_level.parquet"
    summary_path = final / "metrics_site_summary.csv"
    if not pl_path.exists() or not summary_path.exists():
        raise FileNotFoundError("Run code/04_metrics.py first (metrics outputs missing).")
    pl = pd.read_parquet(pl_path)
    summary = pd.read_csv(summary_path)

    def s(metric):
        return summary.loc[summary["metric"] == metric].iloc[0]

    n_ards = int(s("ards_cohort")["numerator"])
    n_eligible = int(s("proseva_eligible")["numerator"])
    n_proned = int(s("ever_proned")["numerator"])
    n_adherent = int(s("adherent_all_eligible")["numerator"])
    n_documented = int(s("position_data_present")["numerator"])
    ttp_median = s("time_to_prone_median_h")["rate"]
    ttp_q1 = s("time_to_prone_q1_h")["rate"]
    ttp_q3 = s("time_to_prone_q3_h")["rate"]
    generated = str(s("ards_cohort")["generated"])

    # ---- sliced metrics → filters + trend (unit × granularity × period) ----
    slices_path = inter / "metrics_slices.parquet"
    if not slices_path.exists():
        raise FileNotFoundError("Run code/04_metrics.py first (metrics_slices.parquet missing).")
    slices = pd.read_parquet(slices_path)
    small_cell_min = int(cfg.get("reporting", {}).get("small_cell_min_den", 10))

    # ---- dashboard payload (per-slice distributions + reactive Table 1) -----
    import json as _json
    payload_path = inter / "dashboard_payload.json"
    if not payload_path.exists():
        raise FileNotFoundError("Run code/04_metrics.py first (dashboard_payload.json missing).")
    payload = _json.loads(payload_path.read_text())
    t1_seed = payload["table1"]["__ALL__"]["all"]["all"]   # all-units / all-time slice
    d_seed = payload["dist"]["__ALL__"]["all"]["all"]
    fsd_median = d_seed["fsd_med"]; fsd_q1 = d_seed["fsd_q1"]; fsd_q3 = d_seed["fsd_q3"]

    slices_js = build_slices_js(slices)
    period_labels = {p: _period_label(p) for p in
                     slices.loc[slices["granularity"].isin(["year", "month", "week"]), "period"].unique()}
    typ = slices[slices["dim"] == "type"] if "dim" in slices.columns else slices
    unit_order = ["__ALL__"] + [u for u in UNIT_LABELS if u in set(typ["unit"]) and u not in ("__ALL__", "unknown")]
    # Specific-unit (location_name) dimension for the "Group ICUs by" toggle.
    name_rows = slices[slices["dim"] == "name"] if "dim" in slices.columns else slices.iloc[0:0]
    name_parent = name_rows.drop_duplicates("unit").set_index("unit")["parent"].to_dict()
    _canon = [u for u in UNIT_LABELS if u not in ("__ALL__", "unknown")]
    name_units = sorted([n for n in name_parent if n != "unknown"],
                        key=lambda n: (_canon.index(name_parent[n]) if name_parent[n] in _canon else 99, n))
    name_order = ["__ALL__"] + name_units
    LABELS = cfg.get("unit_labels", {}) or {}
    unit_labels = {u: UNIT_LABELS.get(u, u) for u in slices["unit"].unique()}
    unit_labels.update({n: LABELS.get(n, n) for n in name_units})
    unit_labels.update({u: LABELS[u] for u in unit_order if u in LABELS})
    cfg_js = {
        "smallCellMin": small_cell_min,
        "unitLabels": unit_labels,
        "unitOrder": unit_order, "nameOrder": name_order,
        "granLabels": GRAN_LABELS,
        "periodLabels": period_labels,
    }
    script_html = (
        "<script>\nconst SLICES = " + _json.dumps(slices_js, ensure_ascii=False)
        + ";\nconst CFG = " + _json.dumps(cfg_js, ensure_ascii=False)
        + ";\nconst PAYLOAD = " + _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";\n" + FILTER_JS + "\n</script>"
    )
    controls_html = build_controls(slices)

    def _h(x):
        return "—" if x is None else (f"{round(x)} h" if x >= 10 else f"{x:.1f} h")

    # Cards seeded with the all-units/all-time values; JS rewrites them on filter change.
    cards = "".join([
        _card_slot("c1", "PROSEVA-eligible", f"{n_eligible:,}", "All ICUs · all time"),
        _card_slot("c2", "Ever proned", f"{100*n_proned/n_eligible:.0f}%",
                   f"{n_proned:,} of {n_eligible:,} eligible"),
        _card_slot("c3", "Median first prone session", _h(fsd_median),
                   f"IQR {_h(fsd_q1)}–{_h(fsd_q3)}, IMV-era proned"),
        _card_slot("c4", "Median time to prone · from T_eligible", f"{ttp_median:.0f} h",
                   f"IQR {ttp_q1:.0f}–{ttp_q3:.0f} h, IMV-era proned"),
    ])
    smallnote_html = (
        f'<div class="smallnote" id="smallnote">† Rate grayed: this slice has fewer than '
        f'{small_cell_min} eligible patients — interpret with caution.</div>'
    )
    trend_html = '<div id="trend"></div>'

    caveat = (
        '<div class="amber"><b>Data-coverage caveat.</b> At this site the CLIF '
        f'<code>position</code> table appears to chart only proning episodes, not routine supine: '
        f'only {n_documented:,} of {n_eligible:,} eligible patients ({100*n_documented/n_eligible:.0f}%) '
        'have any position record, and every one of them was proned. <b>Ever-proned is therefore a '
        'process floor</b> (patients with no position data are counted as not proned), and the prone '
        'duration / time-to-prone panels describe only the charted, proned subset.</div>'
    )

    logo_uri = _load_logo(PROJECT_ROOT / "references" / "images" / "clif_logo_v2.png")
    table1_html = render_gtsummary_table_html(table1_df_from_payload(t1_seed))

    out = build_html(logo_uri, controls_html, cards, smallnote_html, trend_html, caveat,
                     table1_html, n_eligible, site, generated, script_html)
    out_path = final / "proning_dashboard.html"
    out_path.write_text(out, encoding="utf-8")

    log.info("logo embedded: %s", "yes" if logo_uri else "no (fallback)")
    log.info("filters: %d units × {all,year,month,week}; %d slice cells embedded; small-cell min=%d",
             slices["unit"].nunique(), len(slices), small_cell_min)
    log.info("payload: dist+table1 over %d units (all/year/month); seed Table 1 rows: %d",
             len(payload["table1"]), len(t1_seed["rows"]))
    log.info("wrote: %s (%.0f KB)", out_path.relative_to(PROJECT_ROOT.parents[1]), out_path.stat().st_size / 1024)


if __name__ == "__main__":
    main()
