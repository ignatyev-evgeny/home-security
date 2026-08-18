from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .metrics import Metrics

log = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Телеметрия сервера</title>
<style>
  :root {
    --bg:#f4f2f3; --surface:#fbfafb; --text:#191418; --muted:#6c636a;
    --line:#dcd4d9; --accent:#b72248; --ok:#2c7355; --warn:#96660f; --cool:#2f6f8f;
  }
  @media (prefers-color-scheme: dark) { :root {
    --bg:#141116; --surface:#1b181d; --text:#ece6ea; --muted:#9c929b;
    --line:#2f2933; --accent:#f05b7c; --ok:#62bf97; --warn:#d9a548; --cool:#6fb6d9;
  } }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,sans-serif }
  .wrap { max-width:1100px; margin:0 auto; padding:1.5rem 1rem 4rem }
  h1 { font-size:1.4rem; margin:0 0 .3rem }
  .sub { color:var(--muted); font-size:.9rem; margin:0 0 1.5rem }
  .now { display:grid; grid-template-columns:repeat(auto-fit,minmax(8rem,1fr)); gap:.75rem; margin-bottom:1.5rem }
  .card { background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:.8rem .9rem }
  .card .k { font-size:.72rem; letter-spacing:.06em; text-transform:uppercase; color:var(--muted) }
  .card .v { font-size:1.5rem; font-variant-numeric:tabular-nums; margin-top:.2rem }
  .range { display:flex; gap:.5rem; flex-wrap:wrap; margin-bottom:1rem }
  .range button { font:inherit; padding:.35rem .8rem; border-radius:100px; cursor:pointer;
    background:var(--surface); color:var(--text); border:1px solid var(--line) }
  .range button[aria-pressed="true"] { background:var(--accent); border-color:var(--accent); color:#fff }
  figure { margin:0 0 1.5rem; background:var(--surface); border:1px solid var(--line);
    border-radius:10px; padding:1rem .8rem .6rem }
  figcaption { display:flex; gap:1rem; flex-wrap:wrap; font-size:.82rem; color:var(--muted);
    margin-bottom:.6rem; padding:0 .2rem }
  figcaption b { font-weight:600 }
  .key { display:inline-flex; align-items:center; gap:.35em }
  .key i { width:.8em; height:.8em; border-radius:2px; display:inline-block }
  svg { display:block; width:100%; height:auto; overflow:visible }
  .grid { stroke:var(--line) }
  .axis { fill:var(--muted); font-size:10px }
  .empty { color:var(--muted); padding:2rem; text-align:center }
</style></head>
<body><div class="wrap">
  <h1>Телеметрия сервера</h1>
  <p class="sub" id="sub">загрузка…</p>
  <div class="now" id="now"></div>
  <div class="range" id="range"></div>
  <div id="charts"></div>
</div>
<script>
const RANGES = [[1,"сутки"],[7,"неделя"],[30,"30 дней"]];
const CHARTS = [
  {title:"Температуры, °C", unit:"°C",
   band:{lo:"cpu_min", hi:"cpu_max", color:"var(--accent)"},
   series:[
    {key:"cpu_temp", name:"процессор (среднее)", color:"var(--accent)"},
    {key:"disk_temp", name:"диск", color:"var(--cool)"}]},
  {title:"Нагрузка и память", unit:"", series:[
    {key:"load1", name:"load average", color:"var(--warn)"},
    {key:"mem_pct", name:"память, %", color:"var(--ok)"}]},
  {title:"Детектор и камеры", unit:"", series:[
    {key:"inference", name:"инференс, мс", color:"var(--accent)"},
    {key:"cameras_ok", name:"камер на связи", color:"var(--ok)"}]},
  {title:"Свободно под записи, ГБ", unit:"ГБ", series:[
    {key:"free_gb", name:"свободно", color:"var(--cool)"}]},
];
let days = 1;

const fmt = ts => new Date(ts*1000).toLocaleString("ru-RU",
  days > 2 ? {day:"2-digit",month:"2-digit"} : {hour:"2-digit",minute:"2-digit"});

// Полоса между минимумом и максимумом за минуту. Рисуется отдельными
// кусками: разрыв данных не должен соединяться прямой через всю страницу.
function band(rows, loKey, hiKey, x, y) {
  let out = "", seg = [];
  const flush = () => {
    if (seg.length < 2) { seg = []; return; }
    let d = "M" + seg.map(r => x(r.ts).toFixed(1)+" "+y(r[hiKey]).toFixed(1)).join("L");
    d += "L" + seg.slice().reverse().map(r => x(r.ts).toFixed(1)+" "+y(r[loKey]).toFixed(1)).join("L") + "Z";
    out += d; seg = [];
  };
  for (const r of rows) {
    if (r[loKey] === null || r[loKey] === undefined || r[hiKey] === null || r[hiKey] === undefined) flush();
    else seg.push(r);
  }
  flush();
  return out;
}

function path(rows, key, x, y) {
  let d = "", pen = false;
  for (const r of rows) {
    const v = r[key];
    if (v === null || v === undefined) { pen = false; continue; }   // разрыв, а не ноль
    d += (pen ? "L" : "M") + x(r.ts).toFixed(1) + " " + y(v).toFixed(1) + " ";
    pen = true;
  }
  return d.trim();
}

function chart(rows, spec) {
  const W = 900, H = 220, P = {t:10, r:12, b:22, l:38};
  const keysForScale = spec.series.map(s => s.key)
    .concat(spec.band ? [spec.band.lo, spec.band.hi] : []);
  const vals = rows.flatMap(r => keysForScale.map(k => r[k]).filter(v => v !== null && v !== undefined));
  if (!vals.length) return `<figure><figcaption><b>${spec.title}</b></figcaption>
    <div class="empty">нет данных за период</div></figure>`;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-6) { hi = lo + 1; }
  const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
  const t0 = rows[0].ts, t1 = rows[rows.length-1].ts || t0 + 1;
  const x = t => P.l + (t - t0) / Math.max(1, t1 - t0) * (W - P.l - P.r);
  const y = v => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);

  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, yy = y(v);
    g += `<line class="grid" x1="${P.l}" y1="${yy.toFixed(1)}" x2="${W-P.r}" y2="${yy.toFixed(1)}"/>`
       + `<text class="axis" x="${P.l-6}" y="${(yy+3).toFixed(1)}" text-anchor="end">${v.toFixed(v<10?1:0)}</text>`;
  }
  for (let i = 0; i <= 4; i++) {
    const t = t0 + (t1 - t0) * i / 4, xx = x(t);
    g += `<text class="axis" x="${xx.toFixed(1)}" y="${H-6}" text-anchor="middle">${fmt(t)}</text>`;
  }
  const area = spec.band
    ? `<path d="${band(rows, spec.band.lo, spec.band.hi, x, y)}" fill="${spec.band.color}"
         fill-opacity=".18" stroke="none"/>`
    : "";
  const lines = spec.series.map(s =>
    `<path d="${path(rows, s.key, x, y)}" fill="none" stroke="${s.color}" stroke-width="1.6"
       stroke-linejoin="round" stroke-linecap="round"/>`).join("");
  const keys = spec.series.map(s =>
    `<span class="key"><i style="background:${s.color}"></i>${s.name}</span>`).join("");
  let spread = "";
  if (spec.band) {
    const los = rows.map(r => r[spec.band.lo]).filter(v => v !== null && v !== undefined);
    const his = rows.map(r => r[spec.band.hi]).filter(v => v !== null && v !== undefined);
    if (los.length && his.length)
      spread = ` · разброс ${Math.min(...los).toFixed(0)}–${Math.max(...his).toFixed(0)} °C`;
  }
  const last = spec.series.map(s => {
    for (let i = rows.length-1; i >= 0; i--) {
      const v = rows[i][s.key];
      if (v !== null && v !== undefined) return `${s.name}: <b>${v}</b>`;
    }
    return "";
  }).filter(Boolean).join(" · ") + spread;
  return `<figure><figcaption><b>${spec.title}</b>${keys}<span>${last}</span></figcaption>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${spec.title}">${g}${area}${lines}</svg></figure>`;
}

function cards(rows) {
  if (!rows.length) return "";
  const r = rows[rows.length-1];
  const items = [
    ["процессор", r.cpu_temp !== null && r.cpu_min !== null && r.cpu_max !== null
        ? `${r.cpu_temp} <span style="font-size:.6em;color:var(--muted)">${r.cpu_min}–${r.cpu_max}</span>`
        : r.cpu_temp, "°C"],
    ["диск", r.disk_temp, "°C"],
    ["нагрузка", r.load1, ""], ["память", r.mem_pct, "%"],
    ["детектор", r.inference, "мс"], ["свободно", r.free_gb, "ГБ"],
  ];
  return items.filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v, u]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}${u ? " "+u : ""}</div></div>`)
    .join("");
}

async function load() {
  document.getElementById("range").innerHTML = RANGES.map(([d, n]) =>
    `<button data-d="${d}" aria-pressed="${d===days}">${n}</button>`).join("");
  const res = await fetch(`api/metrics?days=${days}`);
  const rows = await res.json();
  document.getElementById("now").innerHTML = cards(rows);
  document.getElementById("charts").innerHTML = CHARTS.map(c => chart(rows, c)).join("");
  document.getElementById("sub").textContent = rows.length
    ? `${rows.length} замеров · последний ${new Date(rows[rows.length-1].ts*1000).toLocaleString("ru-RU")}`
    : "замеров пока нет — история копится с первого запуска";
}

document.getElementById("range").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (b) { days = +b.dataset.d; load(); }
});
load();
setInterval(load, 60000);
</script></body></html>
"""


def build_app(metrics: Metrics) -> web.Application:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")

    async def api(request: web.Request) -> web.Response:
        try:
            days = min(90.0, max(0.1, float(request.query.get("days", 1))))
        except ValueError:
            days = 1.0
        return web.json_response(await metrics.history(days))

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/metrics", api)
    return app


async def run(metrics: Metrics, port: int) -> None:
    """Поднимает страницу телеметрии.

    Слушает только внутри домашней сети — наружу порт не публикуется, как и
    у Frigate: телеметрия сервера не то, что стоит показывать интернету.
    """
    runner = web.AppRunner(build_app(metrics), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("страница телеметрии слушает порт %d", port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
