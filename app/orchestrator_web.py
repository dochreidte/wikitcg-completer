"""Web monitoring page for the multi-account orchestrator.

Serves a self-contained HTML page (no external resources) that polls /api/multi/status every
2 s and shows, per account: status, series, level, ink, available packs, opened packs and
recycled copies — with the active account highlighted.

A single control: the MANUAL account switch. Each row offers an "Activate" button
(POST /api/multi/switch) to leave the active account and switch to it immediately; the active
account itself offers "Next" to simply move on. The rest of the control (series, cookies,
settings) is still done via accounts.toml / config.toml.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>wikitcg — multi-account monitoring</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#0f1115; color:#e6e8ee; font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif; }
  header { padding:14px 18px; background:#161922; border-bottom:1px solid #232838; }
  h1 { margin:0 0 4px; font-size:16px; font-weight:600; }
  .sub { color:#8b93a7; font-size:13px; }
  .sub b { color:#cfd5e6; font-weight:600; }
  table { width:100%; border-collapse:collapse; }
  th,td { padding:9px 14px; text-align:left; border-bottom:1px solid #1d2230; white-space:nowrap; }
  th { color:#8b93a7; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr.active td { background:#13241c; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:7px; vertical-align:middle; }
  .badge { font-size:12px; color:#aeb6c8; }
  .s-run .dot{background:#3ddc84} .s-run{color:#3ddc84}
  .s-sync .dot{background:#4aa8ff} .s-sync{color:#4aa8ff}
  .s-done .dot{background:#7c8395} .s-done{color:#8b93a7}
  .s-err .dot{background:#ff5c5c} .s-err{color:#ff7a7a}
  .s-idle .dot{background:#555b6e} .s-idle{color:#8b93a7}
  .muted{color:#5b6273}
  footer{padding:10px 18px;color:#5b6273;font-size:12px}
  button.sw { font:12px/1 system-ui,Segoe UI,Roboto,sans-serif; color:#cfd5e6;
    background:#1d2230; border:1px solid #2c3346; border-radius:6px; padding:5px 10px;
    cursor:pointer; white-space:nowrap; }
  button.sw:hover { background:#26304a; border-color:#3a4566; }
  button.sw:disabled { opacity:.45; cursor:default; }
  button.sw.next { color:#aeb6c8; }
</style></head>
<body>
  <header>
    <h1>Multi-account monitoring — farm</h1>
    <div class="sub" id="head">Connecting…</div>
  </header>
  <table>
    <thead><tr>
      <th>Account</th><th>Series</th><th>Status</th>
      <th class="num">Lvl</th><th class="num">Ink</th><th class="num">Packs avail.</th>
      <th class="num">Opened</th><th class="num">Recycled</th><th>Action</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <footer id="foot"></footer>
<script>
const LBL = {
  pending:["s-idle","pending"], starting:["s-sync","starting…"],
  idle:["s-idle","idle"], syncing:["s-sync","syncing…"], running:["s-run","farming"],
  waiting:["s-done","exhausted"], exhausted:["s-done","exhausted"],
  stopped:["s-done","stopped"], error:["s-err","error"],
};
const n = v => (v===null||v===undefined) ? '<span class="muted">—</span>' : v;
const dur = s => { s=s|0; const h=s/3600|0, m=(s%3600)/60|0; return h?`${h} h ${m} min`:`${m} min`; };
async function tick(){
  let d; try { d = await (await fetch('/api/multi/status')).json(); }
  catch(e){ document.getElementById('head').textContent='Orchestrator stopped or unreachable.'; return; }
  const act = d.active ? `active: <b>${d.active}</b>` : '<span class="muted">no active account</span>';
  document.getElementById('head').innerHTML =
    `${act} &nbsp;·&nbsp; pass <b>#${d.cycle}</b> &nbsp;·&nbsp; ${d.accounts.length} account(s) &nbsp;·&nbsp; running for ${dur(d.uptime_s)}`;
  document.getElementById('rows').innerHTML = d.accounts.map(a => {
    const [cls,txt] = LBL[a.status] || ["s-idle", a.status||"—"];
    const pend = d.pending_switch !== undefined && d.pending_switch === a.name;
    const btn = a.active
      ? `<button class="sw next" data-acc="">⏭ Next</button>`
      : `<button class="sw" data-acc="${encodeURIComponent(a.name)}"${pend?' disabled':''}>${pend?'⏳ pending…':'▶ Activate'}</button>`;
    return `<tr class="${a.active?'active':''}">
      <td>${a.active?'▶ ':''}${a.name}</td><td class="badge">${a.series}</td>
      <td class="${cls}"><span class="dot"></span>${txt}</td>
      <td class="num">${n(a.level)}</td><td class="num">${n(a.ink)}</td><td class="num">${n(a.packs)}</td>
      <td class="num">${n(a.opened)}</td><td class="num">${n(a.recycled)}</td><td>${btn}</td></tr>`;
  }).join('');
  const tot = d.accounts.reduce((s,a)=>s+(a.opened||0),0);
  const rec = d.accounts.reduce((s,a)=>s+(a.recycled||0),0);
  document.getElementById('foot').textContent =
    `Total: ${tot} pack(s) opened, ${rec} recycled · inter-pass pause ${d.idle_cycle_min} min · refresh 2 s`;
}
// Manual switch: a single delegated listener on #rows (which persists across re-renders).
document.getElementById('rows').addEventListener('click', async (e) => {
  const b = e.target.closest('button.sw');
  if (!b) return;
  const acc = b.dataset.acc ? decodeURIComponent(b.dataset.acc) : null;
  b.disabled = true;
  try {
    await fetch('/api/multi/switch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(acc ? {account: acc} : {}) });
  } catch(_) {}
  tick();
});
tick(); setInterval(tick, 2000);
</script>
</body></html>
"""


def make_app(runner) -> FastAPI:
    app = FastAPI(title="wikitcg multi-account (monitoring)")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _PAGE

    @app.get("/api/multi/status")
    async def status() -> JSONResponse:
        return JSONResponse(runner.status_snapshot())

    @app.post("/api/multi/switch")
    async def switch(request: Request) -> JSONResponse:
        """Manual switch: {"account": "<name>"} to activate that account, or an empty body
        to simply move to the next account."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        account = body.get("account") if isinstance(body, dict) else None
        return JSONResponse(runner.request_switch(account))

    return app
