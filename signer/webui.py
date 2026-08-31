"""auto-checkin 状态看板服务。

与 ok-ef-logviewer 相同的模式：stdlib ThreadingHTTPServer + 内嵌看板页。
路由：
    GET  /              看板页面
    GET  /api/data      最近一次快照 JSON（status.json）
    POST /api/refresh   实时拉取各账号状态（不签到），更新快照后返回
    GET  /health        健康检查
"""

from __future__ import annotations

import asyncio
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>auto-checkin 状态看板</title>
<style>
  :root { --bg:#f3f4f6; --card:#fff; --line:#e5e7eb; --txt:#111; --sub:#6b7280; --brand:#4f6ef7; }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--txt);
         font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:860px; margin:24px auto; padding:0 14px; }
  header { background:var(--brand); color:#fff; border-radius:12px; padding:18px 22px;
           display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px }
  header h1 { font-size:20px; margin:0 }
  header .meta { font-size:13px; opacity:.9 }
  button { background:#fff; color:var(--brand); border:none; border-radius:8px;
           padding:8px 16px; font-size:14px; font-weight:600; cursor:pointer }
  button:disabled { opacity:.6; cursor:wait }
  .bar { display:flex; gap:16px; font-size:13px; color:var(--sub); margin:14px 2px; flex-wrap:wrap }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:14px 18px; margin:12px 0 }
  .card h3 { margin:0 0 8px; font-size:15px }
  .card h3 small { color:var(--sub); font-weight:400; margin-left:8px }
  ul { list-style:none; padding:0; margin:0 }
  li { padding:5px 0; border-top:1px dashed var(--line); font-size:14px }
  li:first-child { border-top:none }
  table { border-collapse:collapse; margin:6px 0 0 26px; font-size:13px; color:#4b5563 }
  td { padding:3px 14px 3px 0; font-variant-numeric:tabular-nums }
  tr:not(:last-child) td { border-bottom:1px solid #f0f1f3 }
  .ok { color:#16a34a } .already { color:#2563eb } .fail { color:#dc2626 } .info { color:#6b7280 }
  footer { text-align:center; color:var(--sub); font-size:12px; margin:18px 0 }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><h1>🎮 auto-checkin 状态看板</h1></div>
    <div style="text-align:right">
      <div class="meta" id="updated">加载中…</div>
      <button id="refresh" onclick="refresh()">立即刷新状态</button>
    </div>
  </header>
  <div class="bar" id="summary"></div>
  <main id="cards"></main>
  <footer>auto-checkin · 每日北京时间 05:00 自动签到 · 页面每 60 秒自动刷新</footer>
</div>
<script>
const ICON = {ok:'✅', already:'ℹ️', fail:'❌', info:'📊'};
const CLS = {ok:'ok', already:'already', fail:'fail', info:'info'};

function render(d) {
  document.getElementById('updated').textContent = '更新于 ' + (d.updated_at || '-');
  const items = [].concat(...(d.accounts||[]).map(a => a.items||[]));
  const sign = items.filter(i => i.kind !== 'info');
  const ok = sign.filter(i => i.kind==='ok'||i.kind==='already').length;
  const fail = sign.length - ok;
  document.getElementById('summary').innerHTML =
     `<span>账号 <b>${(d.accounts||[]).length}</b></span>` +
     `<span>条目 <b>${sign.length}</b></span>` +
     `<span class="ok">成功/已签 <b>${ok}</b></span>` +
     `<span class="${fail?'fail':'info'}">失败 <b>${fail}</b></span>`;
  document.getElementById('cards').innerHTML = (d.accounts||[]).map(a => `
    <div class="card">
      <h3>${a.platform_name.split('(')[0]} · ${a.nickname}<small>${a.uid}</small></h3>
      <ul>${(a.items||[]).map(i => {
        const stats = (i.stats && i.stats.length)
          ? '<table>' + i.stats.map(s =>
              `<tr><td>${s.name}</td><td>${s.cur ?? ''}${s.total ? ' / '+s.total : ''}</td></tr>`
            ).join('') + '</table>'
          : '';
        return `<li class="${CLS[i.kind]||''}">${i.line}${stats}</li>`;
      }).join('')}</ul>
    </div>`).join('') || '<div class="card">暂无数据，点击右上角刷新</div>';
}

let lastUpdated = null;
// 反代子路径兼容：页面在 /auto-checkin/ 下时，API 请求跟随该前缀
const BASE = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
let pollTimer = null;

function render(d) {
  document.getElementById('updated').textContent = '更新于 ' + (d.updated_at || '-');
  if (d.updated_at && d.updated_at !== lastUpdated) {
    lastUpdated = d.updated_at;
    stopPolling();
  }
  const items = [].concat(...(d.accounts||[]).map(a => a.items||[]));
  const sign = items.filter(i => i.kind !== 'info');
  const ok = sign.filter(i => i.kind==='ok'||i.kind==='already').length;
  const fail = sign.length - ok;
  document.getElementById('summary').innerHTML =
     `<span>账号 <b>${(d.accounts||[]).length}</b></span>` +
     `<span>条目 <b>${sign.length}</b></span>` +
     `<span class="ok">成功/已签 <b>${ok}</b></span>` +
     `<span class="${fail?'fail':'info'}">失败 <b>${fail}</b></span>`;
  if (d.refresh_error)
     document.getElementById('summary').innerHTML += `<span class="fail">刷新出错: ${d.refresh_error}</span>`;
  document.getElementById('cards').innerHTML = (d.accounts||[]).map(a => `
    <div class="card">
      <h3>${a.platform_name.split('(')[0]} · ${a.nickname}<small>${a.uid}</small></h3>
      <ul>${(a.items||[]).map(i => {
        const stats = (i.stats && i.stats.length)
          ? '<table>' + i.stats.map(s =>
              `<tr><td>${s.name}</td><td>${s.cur ?? ''}${s.total ? ' / '+s.total : ''}</td></tr>`
            ).join('') + '</table>'
          : '';
        return `<li class="${CLS[i.kind]||''}">${i.line}${stats}</li>`;
      }).join('')}</ul>
    </div>`).join('') || '<div class="card">暂无数据，点击右上角刷新</div>';
}

async function fetchJSON(url, opts) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 60000);
  try {
    const r = await fetch(url, {...opts, signal: ctrl.signal});
    return {ok: r.ok, status: r.status, data: r.ok ? await r.json() : null};
  } finally { clearTimeout(t); }
}

async function load() {
  try {
    const r = await fetchJSON(BASE + 'api/data');
    if (r.ok) render(r.data);
    if (r.data && r.data.refreshing) startPolling();
  } catch (e) { /* 网络异常时静默，下个周期重试 */ }
}

function startPolling() {
  const b = document.getElementById('refresh');
  b.disabled = true; b.textContent = '拉取中…';
  if (!pollTimer) pollTimer = setInterval(load, 3000);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  const b = document.getElementById('refresh');
  b.disabled = false; b.textContent = '立即刷新状态';
}

async function refresh() {
  try {
    await fetchJSON(BASE + 'api/refresh', {method:'POST'});
  } catch (e) { /* 超时也继续轮询快照 */ }
  startPolling();
}
load();
setInterval(load, 60000);
</script>
</body>
</html>"""


_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "error": ""}


def _run_refresh_async(service):
    """后台线程执行实时状态拉取，结果写入 status.json。"""
    with _refresh_lock:
        _refresh_state["running"] = True
        _refresh_state["error"] = ""
    try:
        asyncio.run(service.refresh_status_only())
    except Exception as e:
        with _refresh_lock:
            _refresh_state["error"] = str(e)
        import logging

        logging.getLogger("webui").exception("看板刷新失败")
    finally:
        with _refresh_lock:
            _refresh_state["running"] = False


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/":
                self._send(HTTPStatus.OK, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/data":
                data = service.load_status_snapshot() or {"updated_at": None, "accounts": []}
                with _refresh_lock:
                    data = dict(data)
                    data["refreshing"] = _refresh_state["running"]
                    data["refresh_error"] = _refresh_state["error"]
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
            elif path == "/health":
                self._send(HTTPStatus.OK, b'{"status":"ok"}', "application/json")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            if self.path.split("?")[0] != "/api/refresh":
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            with _refresh_lock:
                running = _refresh_state["running"]
            if running:
                self._send(HTTPStatus.ACCEPTED,
                           b'{"refreshing": true}', "application/json; charset=utf-8")
                return
            threading.Thread(
                target=_run_refresh_async, args=(service,), daemon=True, name="status-refresh"
            ).start()
            # 立即返回，前端轮询 /api/data 直到 updated_at 变化
            self._send(HTTPStatus.ACCEPTED,
                       b'{"refreshing": true}', "application/json; charset=utf-8")

        def log_message(self, fmt, *args):  # 安静模式
            pass

    return Handler


def start_webui(host: str, port: int, service) -> threading.Thread:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="webui")
    thread.start()
    return thread
