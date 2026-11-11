#!/usr/bin/env python3
"""Quick web viewer for a filings SQLite database."""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from flask import Flask, Response, jsonify, request

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WATCH_DB", ROOT_DIR / "data" / "hsmap.db")).expanduser().resolve()
PORT = int(os.environ.get("WATCH_PORT", "5000"))
PAGE_SIZE = 100

app = Flask(__name__)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Serve a local viewer for a filings SQLite database."
    )
    parser.add_argument(
        "db",
        nargs="?",
        default=os.environ.get("WATCH_DB", str(ROOT_DIR / "data" / "hsmap.db")),
        help="path to the SQLite database (default: %(default)s)",
    )
    return parser.parse_args()


def _db():
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def _parse_hs_codes(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        out = []
        for item in parsed:
            if isinstance(item, dict):
                out.append(
                    {
                        "code": str(item.get("code") or item.get("hs_code") or ""),
                        "probability": item.get("probability", item.get("prob")),
                        "description": str(item.get("description") or ""),
                    }
                )
            elif isinstance(item, (list, tuple)):
                out.append(
                    {
                        "code": str(item[0]) if len(item) > 0 else "",
                        "probability": item[1] if len(item) > 1 else None,
                        "description": str(item[2]) if len(item) > 2 and item[2] else "",
                    }
                )
            else:
                out.append(
                    {"code": str(item), "probability": None, "description": ""}
                )
        return out
    return [
        {"code": code.strip(), "probability": None, "description": ""}
        for code in str(value).split(",")
        if code.strip()
    ]


def _parse_code_list(value):
    if isinstance(value, dict):
        value = [[code, probability, ""] for code, probability in value.items()]
    if not isinstance(value, list):
        return []
    rows = []
    for item in value:
        if isinstance(item, dict):
            code = item.get("code") or item.get("hs_code") or ""
            probability = item.get("probability", item.get("prob"))
            description = item.get("description") or ""
        elif isinstance(item, (list, tuple)):
            code = item[0] if len(item) > 0 else ""
            probability = item[1] if len(item) > 1 else None
            description = item[2] if len(item) > 2 and item[2] else ""
        else:
            code, probability, description = item, None, ""
        rows.append(
            {
                "code": "" if code is None else str(code),
                "probability": probability,
                "description": str(description) if description else "",
            }
        )
    return rows


def _parse_output(value):
    empty = {
        "context": "",
        "products": [],
        "removed": [],
        "all_codes": [],
        "removal_probabilities": [],
    }
    if not value or value == "NO_DATA":
        return empty
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return empty
    if not isinstance(parsed, dict):
        return empty
    product_data = parsed.get("product data")
    products = list(product_data) if isinstance(product_data, dict) else []
    removed = parsed.get("removed")
    removed = [str(code) for code in removed] if isinstance(removed, list) else []
    context = parsed.get("context")
    if not isinstance(context, str):
        context = ""
    all_codes = _parse_code_list(parsed.get("all_codes"))
    removal_probabilities = _parse_code_list(parsed.get("removal_probabilities"))
    descriptions = {
        row["code"]: row["description"]
        for row in all_codes
        if row["description"]
    }
    for row in removal_probabilities:
        if not row["description"]:
            row["description"] = descriptions.get(row["code"], "")
    return {
        "context": context,
        "products": products,
        "removed": removed,
        "all_codes": all_codes,
        "removal_probabilities": removal_probabilities,
    }


@app.route("/")
def index():
    return Response(HTML.replace("__DB_PATH__", str(DB_PATH)), mimetype="text/html")


@app.route("/api/rows")
def api_rows():
    offset = max(0, request.args.get("offset", 0, type=int))
    limit = min(500, max(1, request.args.get("limit", PAGE_SIZE, type=int)))
    conn = _db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        rows = conn.execute(
            "SELECT key, cik, conm, fyear, tenk_tag, hs_tag "
            "FROM filings ORDER BY key LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    finally:
        conn.close()
    return jsonify(total=total, rows=[dict(row) for row in rows])


@app.route("/api/stats")
def api_stats():
    conn = _db()
    try:
        total, max_key, codes_len = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(key), 0), "
            "COALESCE(SUM(LENGTH(hs_codes)), 0) FROM filings"
        ).fetchone()
        hs = {}
        for tag, count in conn.execute(
            "SELECT hs_tag, COUNT(*) FROM filings GROUP BY hs_tag"
        ):
            name = "ERROR" if str(tag).startswith("ERROR") else (tag or "NO_DATA")
            hs[name] = hs.get(name, 0) + count
        tenk = {
            tag or "NO_DATA": count
            for tag, count in conn.execute(
                "SELECT tenk_tag, COUNT(*) FROM filings GROUP BY tenk_tag"
            )
        }
    finally:
        conn.close()
    version = "{}|{}|{}|{}|{}".format(
        total,
        max_key,
        codes_len,
        ";".join(f"{key}:{value}" for key, value in sorted(hs.items())),
        ";".join(f"{key}:{value}" for key, value in sorted(tenk.items())),
    )
    return jsonify(total=total, hs=hs, tenk=tenk, version=version)


@app.route("/api/row/<int:key>")
def api_row(key):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT key, cik, conm, fyear, tenk_tag, hs_tag, hs_codes, hs_output "
            "FROM filings WHERE key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify(error="not found"), 404
    data = dict(row)
    data["hs_codes_parsed"] = _parse_hs_codes(data.pop("hs_codes", None))
    data.update(_parse_output(data.pop("hs_output", None)))
    return jsonify(data)


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>hsmap.db viewer</title>
<style>
  :root { --border:#e5e7eb; --muted:#6b7280; }
  html, body { height:100%; }
  * { box-sizing:border-box; }
  body { margin:0; display:flex; flex-direction:column; height:100vh; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f3f4f6; color:#111827; }
  header { flex:0 0 auto; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:10px 16px; background:#111827; color:#f9fafb; }
  header h1 { font-size:15px; margin:0; font-weight:600; }
  header .meta { font-size:12px; color:#9ca3af; }
  .summary { flex:0 0 auto; background:#fff; border-bottom:1px solid var(--border); padding:10px 16px; }
  .summary .line { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .summary .label { font-size:12px; color:var(--muted); min-width:64px; }
  .chips { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
  .chip { display:inline-flex; align-items:center; gap:6px; padding:3px 9px; border-radius:999px; background:#f3f4f6; font-size:12px; white-space:nowrap; }
  .chip b { font-weight:600; }
  .chip .pct { color:var(--muted); }
  .dot { width:8px; height:8px; border-radius:999px; display:inline-block; }
  .stack { display:flex; height:8px; border-radius:999px; overflow:hidden; background:#e5e7eb; margin:8px 0 6px 72px; }
  .stack > span { display:block; height:100%; }
  .summary .small { margin-left:72px; font-size:11.5px; color:var(--muted); }
  .layout { flex:1 1 auto; min-height:0; display:grid; grid-template-columns:minmax(0,1.7fr) minmax(340px,1fr); gap:12px; padding:12px; }
  .panel { background:#fff; border:1px solid var(--border); border-radius:8px; overflow:hidden; min-height:0; }
  .table-wrap { height:100%; overflow:auto; }
  table { border-collapse:collapse; width:100%; font-size:12.5px; }
  thead th { position:sticky; top:0; z-index:1; background:#f9fafb; text-align:left; font-weight:600; padding:8px 10px; border-bottom:1px solid var(--border); white-space:nowrap; }
  tbody td { padding:6px 10px; border-bottom:1px solid #f3f4f6; white-space:nowrap; }
  tbody tr { cursor:pointer; }
  tbody tr:hover { background:#f0f9ff; }
  tbody tr.selected { background:#dbeafe; }
  .num { text-align:right; color:var(--muted); }
  .detail { height:100%; overflow:auto; padding:16px; }
  .detail h2 { margin:0 0 4px; font-size:18px; }
  .detail .sub { color:var(--muted); font-size:12px; margin-bottom:14px; }
  .detail h3 { font-size:11.5px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:16px 0 6px; }
  .plain { font-size:13px; line-height:1.5; }
  .params { display:grid; grid-template-columns:120px 1fr; gap:6px 12px; font-size:13px; margin-bottom:16px; }
  .params dt { color:var(--muted); }
  .params dd { margin:0; }
  .context { background:#f9fafb; border:1px solid var(--border); border-radius:6px; padding:10px; font-size:12.5px; line-height:1.45; margin-bottom:16px; white-space:pre-wrap; }
  .codes { width:100%; font-size:12.5px; border:1px solid var(--border); border-radius:6px; border-collapse:separate; border-spacing:0; overflow:hidden; table-layout:fixed; }
  .codes th { background:#f9fafb; text-align:left; padding:7px 9px; border-bottom:1px solid var(--border); }
  .codes td { padding:7px 9px; border-bottom:1px solid #f3f4f6; vertical-align:top; white-space:normal; overflow-wrap:anywhere; }
  .codes tr:last-child td { border-bottom:none; }
  .codes .p { text-align:right; white-space:nowrap; }
  .codes th:nth-child(1), .codes td:nth-child(1) { width:56px; }
  .codes th:nth-child(2), .codes td:nth-child(2) { width:78px; }
  .codes th:nth-child(4), .codes td:nth-child(4) { width:92px; }
  .note { font-size:11.5px; color:var(--muted); margin:-2px 0 6px; }
  .tag { display:inline-block; padding:1px 6px; border-radius:999px; font-size:11px; font-weight:600; white-space:nowrap; }
  .tag.OK { background:#dcfce7; color:#166534; }
  .tag.EMPTY { background:#e5e7eb; color:#374151; }
  .tag.ERROR { background:#fee2e2; color:#991b1b; }
  .tag.NO_DATA { background:#f3f4f6; color:#6b7280; }
  .placeholder { color:var(--muted); font-size:13px; }
  .empty { color:var(--muted); font-style:italic; }
  @media (max-width:800px) { .layout { grid-template-columns:1fr; flex:none; height:auto; } .panel { height:60vh; } .summary .stack, .summary .small { margin-left:0; } }
</style>
</head>
<body>
<header>
  <h1>hsmap.db viewer</h1>
  <div class="meta"><span>__DB_PATH__</span> · <span id="total">…</span> filings</div>
</header>
<div class="summary">
  <div class="line">
    <span class="label">HS tag</span>
    <div class="chips" id="hsChips"></div>
  </div>
  <div class="stack" id="hsBar"></div>
  <div class="small" id="tenkLine"></div>
</div>
<div class="layout">
  <div class="panel">
    <div class="table-wrap" id="tableWrap">
      <table>
        <thead><tr>
          <th>Key</th><th>CIK</th><th>Company</th><th>FY</th><th>10-K</th><th>HS</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
  <aside class="panel">
    <div class="detail" id="detail">
      <p class="placeholder">Select a filing on the left.</p>
    </div>
  </aside>
</div>
<script>
  const PAGE = 100;
  const MAX_FETCH = 500;
  const HS_ORDER = ['OK', 'EMPTY', 'ERROR', 'NO_DATA'];
  const HS_COLORS = {OK:'#16a34a', EMPTY:'#9ca3af', ERROR:'#dc2626', NO_DATA:'#d1d5db'};
  const rowsEl = document.getElementById('rows');
  const wrap = document.getElementById('tableWrap');
  const detailEl = document.getElementById('detail');
  const totalEl = document.getElementById('total');
  const hsChips = document.getElementById('hsChips');
  const hsBar = document.getElementById('hsBar');
  const tenkLine = document.getElementById('tenkLine');
  let offset = 0, total = 0, loading = false, done = false;
  let selectedKey = null, lastVersion = null, refreshing = false;

  function esc(v) {
    return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function tag(t) {
    t = t || 'NO_DATA';
    const cls = t === 'OK' ? 'OK' : t === 'EMPTY' ? 'EMPTY' : t.startsWith('ERROR') ? 'ERROR' : 'NO_DATA';
    return `<span class="tag ${cls}">${esc(t)}</span>`;
  }
  function prob(v) {
    if (v === null || v === undefined || v === '') return '';
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(4) : esc(v);
  }
  function pct(n, total) {
    return total ? (100 * n / total).toFixed(1) + '%' : '0%';
  }

  function renderSummary(stats) {
    const t = stats.total || 0;
    totalEl.textContent = t;
    const hs = stats.hs || {};
    const keys = HS_ORDER.filter(k => k in hs);
    hsChips.innerHTML =
      `<span class="chip"><span class="dot" style="background:#111827"></span>Total <b>${t}</b></span>` +
      keys.map(k => `<span class="chip"><span class="dot" style="background:${HS_COLORS[k]}"></span>${esc(k)} <b>${hs[k]}</b> <span class="pct">${pct(hs[k], t)}</span></span>`).join('');
    hsBar.innerHTML = keys.map(k => `<span style="width:${t ? 100 * hs[k] / t : 0}%;background:${HS_COLORS[k]}"></span>`).join('');
    tenkLine.textContent = '10-K tag: ' + Object.entries(stats.tenk || {}).map(([k, v]) => `${k} ${v}`).join(' · ');
  }

  function buildRow(row) {
    const tr = document.createElement('tr');
    tr.dataset.key = row.key;
    tr.innerHTML = `<td class="num">${row.key}</td><td>${esc(row.cik)}</td><td>${esc(row.conm)}</td><td>${esc(row.fyear)}</td><td>${tag(row.tenk_tag)}</td><td>${tag(row.hs_tag)}</td>`;
    tr.addEventListener('click', () => selectRow(row.key));
    return tr;
  }

  function applySelection() {
    document.querySelectorAll('#rows tr').forEach(tr => tr.classList.toggle('selected', Number(tr.dataset.key) === selectedKey));
  }

  async function loadMore() {
    if (loading || done || refreshing) return;
    loading = true;
    try {
      const res = await fetch(`/api/rows?offset=${offset}&limit=${PAGE}`);
      const data = await res.json();
      total = data.total;
      totalEl.textContent = total;
      for (const row of data.rows) rowsEl.appendChild(buildRow(row));
      offset += data.rows.length;
      done = offset >= total || data.rows.length === 0;
      if (!done && wrap.scrollHeight <= wrap.clientHeight) setTimeout(loadMore, 0);
    } catch (err) {
      console.error(err);
    } finally {
      loading = false;
    }
  }

  async function fetchLoaded(n) {
    const out = [];
    let off = 0;
    let fetchedTotal = total;
    while (off < n) {
      const data = await (await fetch(`/api/rows?offset=${off}&limit=${Math.min(MAX_FETCH, n - off)}`)).json();
      fetchedTotal = data.total;
      out.push(...data.rows);
      if (!data.rows.length) break;
      off += data.rows.length;
    }
    return {rows: out, total: fetchedTotal};
  }

  async function refreshLive() {
    if (document.hidden || refreshing) return;
    refreshing = true;
    try {
      const stats = await (await fetch('/api/stats')).json();
      renderSummary(stats);
      if (stats.version === lastVersion) return;
      lastVersion = stats.version;
      if (offset === 0) return;
      const data = await fetchLoaded(offset);
      total = data.total;
      totalEl.textContent = total;
      const scrollTop = wrap.scrollTop;
      const frag = document.createDocumentFragment();
      for (const row of data.rows) frag.appendChild(buildRow(row));
      rowsEl.replaceChildren(frag);
      wrap.scrollTop = scrollTop;
      offset = data.rows.length;
      done = offset >= total;
      applySelection();
      if (selectedKey !== null) await selectRow(selectedKey);
      if (!done && (wrap.scrollHeight <= wrap.clientHeight ||
          wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 200)) {
        setTimeout(loadMore, 0);
      }
    } catch (err) {
      console.error(err);
    } finally {
      refreshing = false;
    }
  }

  async function selectRow(key) {
    selectedKey = key;
    applySelection();
    let d;
    try {
      const res = await fetch(`/api/row/${key}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      d = await res.json();
    } catch (err) {
      console.error(err);
      detailEl.innerHTML = '<p class="placeholder">Could not load row.</p>';
      return;
    }
    const codes = d.hs_codes_parsed || [];
    const products = d.products || [];
    const removed = d.removed || [];
    const pickedSet = new Set(codes.map(c => c.code));
    const removedSet = new Set(removed);

    const byProbDesc = (a, b) =>
      (Number(b.probability) || 0) - (Number(a.probability) || 0) ||
      String(a.code).localeCompare(String(b.code));

    const considered = (d.all_codes && d.all_codes.length) ? d.all_codes.slice() : codes.slice();
    const consideredCodes = new Set(considered.map(c => c.code));
    for (const c of codes) {
      if (!consideredCodes.has(c.code)) {
        considered.push(c);
        consideredCodes.add(c.code);
      }
    }
    considered.sort(byProbDesc);
    const consideredRows = considered.length ? considered.map(c => {
      const status = removedSet.has(c.code)
        ? '<span class="tag ERROR">Removed</span>'
        : pickedSet.has(c.code)
          ? '<span class="tag OK">Picked</span>'
          : '<span class="tag NO_DATA">Candidate</span>';
      return `<tr><td>${esc(c.code)}</td><td class="p">${prob(c.probability)}</td><td>${esc(c.description)}</td><td>${status}</td></tr>`;
    }).join('') : `<tr><td colspan="4" class="empty">No considered HS codes stored.</td></tr>`;

    const removals = (d.removal_probabilities || []).slice();
    const scoredCodes = new Set(removals.map(r => r.code));
    for (const code of removed) {
      if (!scoredCodes.has(code)) {
        removals.push({code, probability: null, description: ""});
        scoredCodes.add(code);
      }
    }
    removals.sort(byProbDesc);
    const removalRows = removals.length ? removals.map(r => {
      const decision = removedSet.has(r.code)
        ? '<span class="tag ERROR">Removed</span>'
        : '<span class="tag OK">Kept</span>';
      return `<tr><td>${esc(r.code)}</td><td class="p">${prob(r.probability)}</td><td>${esc(r.description)}</td><td>${decision}</td></tr>`;
    }).join('') : `<tr><td colspan="4" class="empty">No removal scores stored.</td></tr>`;

    const missingAllCodes = !d.all_codes || d.all_codes.length === 0;

    detailEl.innerHTML = `
      <h2>${esc(d.conm)}</h2>
      <div class="sub">Filing ${d.key} · CIK ${esc(d.cik)} · FY ${esc(d.fyear)}</div>
      <dl class="params">
        <dt>10-K tag</dt><dd>${tag(d.tenk_tag)}</dd>
        <dt>HS tag</dt><dd>${tag(d.hs_tag)}</dd>
        <dt>CIK</dt><dd>${esc(d.cik)}</dd>
        <dt>Fiscal year</dt><dd>${esc(d.fyear)}</dd>
      </dl>
      <h3>Products</h3>
      <div class="plain">${products.length ? products.map(p => esc(p)).join(', ') : '<span class="empty">None</span>'}</div>
      ${d.context ? `<h3>Context</h3><div class="context">${esc(d.context)}</div>` : ''}
      <h3>Considered HS codes — ${considered.length} entries, ${pickedSet.size} picked</h3>
      ${missingAllCodes ? '<div class="note">No full candidate list is stored for this row; showing final picks only.</div>' : ''}
      <table class="codes">
        <thead><tr><th>Code</th><th class="p">Probability</th><th>Description</th><th>Status</th></tr></thead>
        <tbody>${consideredRows}</tbody>
      </table>
      <h3>Removal scores — P(outlier), ${removals.length} entries, ${removedSet.size} removed</h3>
      <table class="codes">
        <thead><tr><th>Code</th><th class="p">Probability</th><th>Description</th><th>Decision</th></tr></thead>
        <tbody>${removalRows}</tbody>
      </table>
    `;
  }

  wrap.addEventListener('scroll', () => {
    if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 200) loadMore();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshLive(); });
  (async () => { await refreshLive(); await loadMore(); })();
  setInterval(refreshLive, 2000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    args = _parse_args()
    DB_PATH = Path(args.db).expanduser().resolve()
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")
    print(f"Serving {DB_PATH} at http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
