"""Local browser UI for racepath (``racepath serve``).

A small single-page app served by the standard library ``http.server``.
The work itself is done by :mod:`racepath.jobs`, which drives the existing CLI
subcommands, so detection logic and output formats are exactly those of the CLI.
The same page (INDEX_HTML) is reused by the offline single-file build, where
``window.racepathLocal`` replaces the HTTP API with Pyodide.

Security model: the server binds to 127.0.0.1 by default, checks the Host
header (DNS rebinding), and requires a per-process random token on every API
call (CSRF from other web pages). Files are only served from the work dir.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from racepath import __version__
from racepath.jobs import (  # noqa: F401  (re-exported for tests and the offline build)
    DEFAULT_CORNERS, OUTPUT_FILES, ApiError, App, run_cli, within,
)

CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".zip": "application/zip",
    ".png": "image/png",
}


def make_handler(app: App, allowed_hosts):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"racepath/{__version__}"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # ------------------------------------------------------------ helpers
        def _send(self, status, body, ctype, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status=200):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, status, message, log=""):
            self._json({"error": message, "log": log}, status)

        def _host_ok(self):
            if allowed_hosts is None:
                return True
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _token_ok(self):
            return secrets.compare_digest(self.headers.get("X-Racepath-Token", ""), app.token)

        def _length(self):
            try:
                return max(0, int(self.headers.get("Content-Length") or 0))
            except ValueError:
                return 0

        def _body_json(self):
            n = self._length()
            if n > 16 << 20:
                raise ApiError(413, "リクエストが大きすぎます")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                raise ApiError(400, "JSON を読めません")
            if not isinstance(obj, dict):
                raise ApiError(400, "JSON オブジェクトを送ってください")
            return obj

        # ------------------------------------------------------------- routes
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._error(403, "Host ヘッダーが不正です")
            url = urlparse(self.path)
            p = url.path
            try:
                if p in ("/", "/index.html"):
                    body = INDEX_HTML.replace("__TOKEN__", app.token) \
                                     .replace("__VERSION__", __version__)
                    return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8",
                                      {"Referrer-Policy": "no-referrer"})
                if p.startswith("/files/"):
                    return self._file(p[len("/files/"):], parse_qs(url.query))
                if p.startswith("/api/"):
                    if not self._token_ok():
                        return self._error(403, "トークンが不正です。ページを開き直してください。")
                    if p == "/api/jobs":
                        return self._json({"jobs": app.list_jobs()})
                    if p == "/api/job":
                        job = parse_qs(url.query).get("job", [""])[0]
                        return self._json(app.summary(job))
                    if p == "/api/default-corners":
                        return self._json(DEFAULT_CORNERS)
                self._error(404, "見つかりません")
            except ApiError as e:
                self._error(e.status, e.message, e.log)

        def do_POST(self):
            if not self._host_ok():
                return self._error(403, "Host ヘッダーが不正です")
            if not self._token_ok():
                return self._error(403, "トークンが不正です。ページを開き直してください。")
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/upload":
                    return self._json(app.upload_netlist(q.get("name", ""), self.rfile, self._length()))
                if url.path == "/api/upload-result":
                    return self._json(app.upload_result(q.get("job"), q.get("name", ""),
                                                        self.rfile, self._length()))
                body = self._body_json()
                if url.path == "/api/analyze":
                    return self._json(app.analyze(body))
                if url.path == "/api/decks":
                    return self._json(app.decks(body))
                if url.path == "/api/collect":
                    return self._json(app.collect(body))
                if url.path == "/api/clear-results":
                    return self._json(app.clear_results(body.get("job")))
                self._error(404, "見つかりません")
            except ApiError as e:
                self._error(e.status, e.message, e.log)
            except Exception:
                self._error(500, "サーバー内部のエラーです", traceback.format_exc())

        def _file(self, rest, query):
            parts = rest.split("/", 1)
            if len(parts) != 2:
                return self._error(404, "見つかりません")
            path = app.file_path(parts[0], parts[1])
            ext = os.path.splitext(path)[1].lower()
            ctype = CTYPES.get(ext, "text/plain; charset=utf-8")
            extra = {}
            if query.get("dl") or ext == ".zip":
                fname = f"{parts[0]}_{os.path.basename(path)}" if ext == ".zip" else os.path.basename(path)
                extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(fname)}"
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype, extra)

    return Handler


def make_server(host="127.0.0.1", port=8765, workdir="racepath_web", token=None):
    app = App(workdir, token or secrets.token_urlsafe(24))
    loopback = host in ("127.0.0.1", "localhost", "::1")
    # Host header allow-list; filled after bind because port may be 0 (auto)
    allowed = set() if loopback else None
    httpd = ThreadingHTTPServer((host, port), make_handler(app, allowed))
    if loopback:
        real_port = httpd.server_address[1]
        allowed.update(f"{h}:{real_port}" for h in ("127.0.0.1", "localhost", "[::1]"))
    httpd.daemon_threads = True
    httpd.app = app
    return httpd


def cmd_serve(args):
    try:
        httpd = make_server(args.host, args.port, args.workdir)
    except OSError as e:
        raise SystemExit(f"ポート {args.port} で起動できません({e})。--port で別の番号を指定してください。")
    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"racepath {__version__} ブラウザ画面: {url}")
    print(f"  作業フォルダ: {httpd.app.workdir}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  注意: ループバック以外で待ち受けています。同じネットワークの他の端末からも接続できます。")
    print("  終了するには Ctrl+C")
    sys.stdout.flush()
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました")
    finally:
        httpd.server_close()


INDEX_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>racepath ローカル</title>
<style>
:root{
  --bg:#f6f5f2; --panel:#ffffff; --ink:#1d1d1f; --muted:#62615c; --line:#dcd9d2;
  --accent:#2f5d8a; --accent-ink:#ffffff; --code:#f1efe9;
  --ok:#2e7d4f; --warn:#a86a00; --ng:#b3261e; --info:#2f5d8a;
  --c-sae:#b4532a; --c-wl:#2f6fb0; --c-hold:#6b4fa0; --c-clk:#2a8a7a; --c-gen:#6d6d6d;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#161616; --panel:#202020; --ink:#ecebe8; --muted:#a7a59f; --line:#3a3936;
    --accent:#7fb0e0; --accent-ink:#0f1a26; --code:#2a2a28;
    --ok:#6cc596; --warn:#e0a640; --ng:#f08a80; --info:#7fb0e0;
    --c-sae:#e08a62; --c-wl:#7fb0e0; --c-hold:#b49ae0; --c-clk:#6cc5b4; --c-gen:#a7a59f;
  }
}
:root[data-theme="dark"]{
  --bg:#161616; --panel:#202020; --ink:#ecebe8; --muted:#a7a59f; --line:#3a3936;
  --accent:#7fb0e0; --accent-ink:#0f1a26; --code:#2a2a28;
  --ok:#6cc596; --warn:#e0a640; --ng:#f08a80; --info:#7fb0e0;
  --c-sae:#e08a62; --c-wl:#7fb0e0; --c-hold:#b49ae0; --c-clk:#6cc5b4; --c-gen:#a7a59f;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Segoe UI",sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{margin:0;font-size:18px}
header .sub{color:var(--muted);font-size:13px}
main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:16px}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
h2{margin:0 0 10px;font-size:16px}
h2 .n{display:inline-block;width:22px;height:22px;border-radius:50%;background:var(--accent);
  color:var(--accent-ink);text-align:center;font-size:13px;line-height:22px;margin-right:6px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input[type=text],input[type=number],select,textarea{width:100%;font:inherit;color:var(--ink);
  background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px}
textarea{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;min-height:90px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:0 12px}
.tabs{display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.tabs button{background:transparent;color:var(--ink);border:1px solid var(--line)}
.tabs button[aria-selected=true]{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
button{font:inherit;cursor:pointer;border-radius:6px;border:1px solid var(--accent);
  background:var(--accent);color:var(--accent-ink);padding:8px 14px}
button.ghost{background:transparent;color:var(--accent)}
button:disabled{opacity:.5;cursor:default}
.drop{border:2px dashed var(--line);border-radius:8px;padding:18px;text-align:center;color:var(--muted)}
.drop.over{border-color:var(--accent);color:var(--ink)}
.drop input{display:block;margin:8px auto 0;max-width:100%}
.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
.status{font-size:14px;color:var(--muted)}
.status.err{color:var(--ng)}
.status.ok{color:var(--ok)}
details{margin-top:10px}
summary{cursor:pointer;color:var(--accent)}
pre{background:var(--code);border-radius:6px;padding:10px;overflow:auto;font-size:12.5px;
  max-height:320px;margin:8px 0 0;white-space:pre}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chip{border-radius:999px;padding:2px 10px;font-size:13px;border:1px solid var(--line)}
.chip b{font-weight:700}
.k-sae_bl{color:var(--c-sae);border-color:var(--c-sae)}
.k-wl_pre{color:var(--c-wl);border-color:var(--c-wl)}
.k-hold{color:var(--c-hold);border-color:var(--c-hold)}
.k-clock_pair{color:var(--c-clk);border-color:var(--c-clk)}
.k-generic{color:var(--c-gen);border-color:var(--c-gen)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:6px 0}
.fact{background:var(--bg);border-radius:6px;padding:8px}
.fact .l{font-size:12px;color:var(--muted)}
.fact .v{font-size:15px;font-weight:600;overflow-wrap:anywhere}
.files{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.files a{color:var(--accent);font-size:14px}
.warns{color:var(--warn);font-size:13px;margin:6px 0;padding-left:18px}
iframe{width:100%;height:75vh;border:1px solid var(--line);border-radius:8px;background:#fff;margin-top:8px}
.tbl{overflow-x:auto;margin-top:8px}
table{border-collapse:collapse;font-size:13px;min-width:100%}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}
td.v-NG{color:var(--ng);font-weight:700}
td.v-warn{color:var(--warn);font-weight:700}
td.v-OK{color:var(--ok)}
.jobs{list-style:none;padding:0;margin:0;display:grid;gap:6px}
.jobs button{width:100%;text-align:left;background:var(--bg);color:var(--ink);border:1px solid var(--line);
  display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.jobs .m{color:var(--muted);font-size:13px}
.hidden{display:none !important}
.note{font-size:13px;color:var(--muted)}
.boot{margin-top:8px;padding:8px 10px;border-radius:6px;background:var(--code);font-size:14px}
.boot.err{color:var(--ng)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere}
</style>
</head>
<body>
<header>
  <h1>racepath ローカル <span class="sub">v__VERSION__</span></h1>
  <div class="sub">ネットリストからレーシングパスを検出します。データはこのPCの外には出ません。</div>
  <div class="boot hidden" id="boot" role="status"></div>
</header>
<main>

<section id="sec-analyze">
  <h2><span class="n">1</span>解析</h2>
  <div class="tabs srv-only" role="tablist">
    <button type="button" role="tab" id="tab-file" aria-selected="true">ファイルを選ぶ</button>
    <button type="button" role="tab" id="tab-path" aria-selected="false">パスを指定</button>
  </div>
  <div id="pane-file">
    <div class="drop" id="drop">
      ネットリスト(.cdl / .sp / .scs など)をここにドロップ
      <input type="file" id="netfile">
    </div>
  </div>
  <div id="pane-path" class="hidden">
    <label for="netpath">このPC上のネットリストのパス(大きなファイルはこちらが速い)</label>
    <input type="text" id="netpath" placeholder="/home/me/macro/sram.cdl" autocomplete="off">
  </div>
  <details id="opts">
    <summary>オプション(自動判定でよければ空欄のまま)</summary>
    <div class="row">
      <div><label for="o-top">トップセル(-t)</label><input type="text" id="o-top" placeholder="自動"></div>
      <div><label for="o-src">起点ネット(-s、空白かカンマ区切り)</label><input type="text" id="o-src" placeholder="自動(例: CLK)"></div>
      <div><label for="o-fmt">形式(-f)</label>
        <select id="o-fmt"><option value="auto">自動</option><option value="cdl">CDL</option>
        <option value="spice">SPICE</option><option value="hspice">HSPICE</option><option value="spectre">Spectre</option></select></div>
      <div><label for="o-prefix">プローブの接頭辞(--prefix)</label><input type="text" id="o-prefix" placeholder="XDUT."></div>
    </div>
    <label><input type="checkbox" id="o-nogroup"> 同型の競合をまとめずに全件出す(--no-group)</label>
    <label for="o-cfg">設定(racepath.json の中身、JSON)</label>
    <textarea id="o-cfg" placeholder='{"supplies": ["VDD*","VSS*","VCS*"]}'></textarea>
  </details>
  <div class="actions">
    <button type="button" id="btn-analyze">解析する</button>
    <span class="status" id="st-analyze" role="status"></span>
  </div>
</section>

<section id="sec-result" class="hidden">
  <h2>解析結果 <span class="sub mono" id="r-job"></span></h2>
  <div class="facts" id="r-facts"></div>
  <div class="chips" id="r-chips"></div>
  <ul class="warns" id="r-warns"></ul>
  <div class="files" id="r-files"></div>
  <details><summary>コンソール出力</summary><pre id="r-log"></pre></details>
  <iframe id="r-frame" title="レポート"></iframe>
</section>

<section id="sec-decks" class="hidden">
  <h2><span class="n">2</span>コーナーデッキを作る</h2>
  <p class="note">モデルファイルのパス・セクション、VDD、温度、クロックを書き換えてください。シミュレーションはこの画面では実行しません。zip を展開して <span class="mono">run_all.sh</span> を手元で実行してください。</p>
  <div class="row">
    <div><label for="d-sim">シミュレータ</label>
      <select id="d-sim"><option value="spectre">Spectre</option><option value="hspice">HSPICE</option></select></div>
  </div>
  <label><input type="checkbox" id="d-portable"> ネットリストと races.meas を zip に入れ、ファイル名で参照する(別のPCで実行する場合)</label>
  <label for="d-corners">コーナー定義(corners.json)</label>
  <textarea id="d-corners" rows="14"></textarea>
  <div class="actions">
    <button type="button" id="btn-decks">デッキを作る</button>
    <span class="status" id="st-decks" role="status"></span>
  </div>
  <div id="d-out"></div>
</section>

<section id="sec-collect" class="hidden">
  <h2><span class="n">3</span>結果を集計する</h2>
  <div class="tabs srv-only" role="tablist">
    <button type="button" role="tab" id="ctab-up" aria-selected="true">フォルダ/ファイルを選ぶ</button>
    <button type="button" role="tab" id="ctab-path" aria-selected="false">パスを指定</button>
  </div>
  <div id="cpane-up">
    <p class="note">シミュレーション結果のフォルダごと選べます。送るのは .measure / .mt0 / .lis / .log などの測定結果だけです。コーナー名はファイル名(tran1 などの場合は親フォルダ名)から取ります。</p>
    <label for="c-dir">フォルダ</label><input type="file" id="c-dir" webkitdirectory multiple>
    <label for="c-files">ファイル</label><input type="file" id="c-files" multiple>
  </div>
  <div id="cpane-path" class="hidden">
    <label for="c-path">結果のフォルダまたはファイルのパス</label>
    <input type="text" id="c-path" placeholder="/home/me/sim/decks" autocomplete="off">
  </div>
  <div class="row">
    <div><label for="c-warn">時間マージンの警告閾値 [ps]</label><input type="number" id="c-warn" value="20" step="any"></div>
    <div><label for="c-warnv">SA差電圧マージンの警告閾値 [V]</label><input type="number" id="c-warnv" value="0.02" step="any"></div>
    <div><label for="c-skew">スキューの要確認閾値 [ps]</label><input type="number" id="c-skew" value="10" step="any"></div>
  </div>
  <div class="actions">
    <button type="button" id="btn-collect">集計する</button>
    <span class="status" id="st-collect" role="status"></span>
  </div>
  <div id="c-out"></div>
</section>

<section id="sec-jobs">
  <h2>過去の解析</h2>
  <ul class="jobs" id="jobs"></ul>
  <p class="note" id="jobs-empty">まだありません。</p>
</section>

</main>
<!--LOCAL_BACKEND-->
<script>
(function(){
"use strict";
var TOKEN = "__TOKEN__";
var CLASS_JA = {sae_bl:"SAE vs BL振幅", wl_pre:"WL vs プリチャージ", hold:"ラッチ ホールド",
                clock_pair:"クロック対", generic:"汎用"};
var ORDER = ["sae_bl","wl_pre","hold","clock_pair","generic"];
var state = {job:null, mode:"file", cmode:"up"};
function $(id){ return document.getElementById(id); }
function el(tag, attrs, text){
  var e = document.createElement(tag);
  if(attrs) Object.keys(attrs).forEach(function(k){ e.setAttribute(k, attrs[k]); });
  if(text !== undefined && text !== null) e.textContent = String(text);
  return e;
}
function setStatus(id, msg, kind){ var s=$(id); s.textContent=msg||""; s.className="status"+(kind?" "+kind:""); }
var LOCAL = window.racepathLocal || null;
function api(method, path, body, raw){
  if(LOCAL) return LOCAL.api(method, path, body, raw);
  var opt = {method:method, headers:{"X-Racepath-Token":TOKEN}};
  if(body !== undefined){
    if(raw){ opt.body = body; opt.headers["Content-Type"]="application/octet-stream"; }
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"]="application/json"; }
  }
  return fetch(path, opt).then(function(r){
    return r.json().catch(function(){ return {error:"応答を読めません("+r.status+")"}; })
      .then(function(j){ if(!r.ok || j.error){ var e=new Error(j.error||("HTTP "+r.status)); e.log=j.log||""; throw e; } return j; });
  });
}
function tabs(a, b, pa, pb, key, val){
  $(a).addEventListener("click", function(){ $(a).setAttribute("aria-selected","true"); $(b).setAttribute("aria-selected","false");
    $(pa).classList.remove("hidden"); $(pb).classList.add("hidden"); state[key]=val[0]; });
  $(b).addEventListener("click", function(){ $(b).setAttribute("aria-selected","true"); $(a).setAttribute("aria-selected","false");
    $(pb).classList.remove("hidden"); $(pa).classList.add("hidden"); state[key]=val[1]; });
}
tabs("tab-file","tab-path","pane-file","pane-path","mode",["file","path"]);
tabs("ctab-up","ctab-path","cpane-up","cpane-path","cmode",["up","path"]);
if(LOCAL){
  // offline build: no access to local paths, decks are always self-contained
  [].forEach.call(document.querySelectorAll(".srv-only"), function(e){ e.classList.add("hidden"); });
  $("d-portable").checked = true; $("d-portable").disabled = true;
  var boot = $("boot"), ab = $("btn-analyze");
  boot.classList.remove("hidden"); ab.disabled = true;
  boot.textContent = "解析エンジン(ブラウザ内の Python)を準備しています…(初回は数秒〜十数秒)";
  LOCAL.ready.then(function(){ boot.classList.add("hidden"); ab.disabled = false; },
    function(e){ boot.className = "boot err"; boot.textContent = "解析エンジンを起動できませんでした: " + e.message; });
}

var drop = $("drop");
["dragenter","dragover"].forEach(function(t){ drop.addEventListener(t, function(e){ e.preventDefault(); drop.classList.add("over"); }); });
["dragleave","drop"].forEach(function(t){ drop.addEventListener(t, function(e){ e.preventDefault(); drop.classList.remove("over"); }); });
drop.addEventListener("drop", function(e){ if(e.dataTransfer.files.length){ $("netfile").files = e.dataTransfer.files; } });

function options(){
  return {top:$("o-top").value, sources:$("o-src").value, format:$("o-fmt").value,
          prefix:$("o-prefix").value, no_group:$("o-nogroup").checked, config:$("o-cfg").value};
}
$("btn-analyze").addEventListener("click", function(){
  var btn = this; btn.disabled = true;
  var p;
  if(state.mode === "file"){
    var f = $("netfile").files[0];
    if(!f){ setStatus("st-analyze","ネットリストを選んでください","err"); btn.disabled=false; return; }
    setStatus("st-analyze","アップロード中…");
    p = api("POST","/api/upload?name="+encodeURIComponent(f.name), f, true).then(function(up){
      setStatus("st-analyze","解析中…");
      var o = options(); o.job = up.job; return api("POST","/api/analyze", o);
    });
  } else {
    var path = $("netpath").value.trim();
    if(!path){ setStatus("st-analyze","パスを入力してください","err"); btn.disabled=false; return; }
    setStatus("st-analyze","解析中…");
    var o = options(); o.path = path; p = api("POST","/api/analyze", o);
  }
  p.then(function(res){
    var n = res.races ? res.races.length : 0;
    setStatus("st-analyze","完了: 競合 "+n+" 件","ok");
    show(res); loadJobs();
  }).catch(function(e){
    setStatus("st-analyze", e.message, "err");
    if(e.log){ $("sec-result").classList.remove("hidden"); $("r-log").textContent=e.log;
      $("r-log").parentNode.open = true; }
  }).then(function(){ btn.disabled=false; });
});

function fileUrl(rel, dl){ return "/files/"+encodeURIComponent(state.job)+"/"+rel+(dl?"?dl=1":""); }
// link to a job file; in the offline build the bytes come from the in-browser file system
function fileLink(rel, text, dl, newTab){
  var a = el("a",{href:fileUrl(rel, dl)}, text);
  if(newTab){ a.target = "_blank"; a.rel = "noopener"; }
  if(LOCAL){
    var job = state.job;
    a.addEventListener("click", function(e){
      e.preventDefault();
      var w = (!dl) ? window.open("", "_blank") : null;
      LOCAL.file(job, rel).then(function(f){
        var url = URL.createObjectURL(f.blob);
        if(w){ w.location.href = url; }
        else { var t = el("a",{href:url, download:f.name}); document.body.appendChild(t); t.click(); t.remove(); }
        setTimeout(function(){ URL.revokeObjectURL(url); }, 60000);
      }).catch(function(err){ if(w) w.close(); alert(err.message); });
    });
  }
  return a;
}
function show(res){
  state.job = res.job;
  $("sec-result").classList.remove("hidden");
  $("r-job").textContent = res.job + (res.netlist_name ? "  ("+res.netlist_name+")" : "");
  $("r-log").textContent = res.log || "";
  var facts = $("r-facts"); facts.textContent = "";
  var chips = $("r-chips"); chips.textContent = "";
  var warns = $("r-warns"); warns.textContent = "";
  var files = $("r-files"); files.textContent = "";
  if(!res.analyzed){ $("r-frame").classList.add("hidden"); return; }
  [["トップ",res.top],["形式",res.format],["トランジスタ",res.n_mos],["CCC",res.n_ccc],
   ["起点",(res.sources||[]).join(", ")],["競合",res.races.length]].forEach(function(f){
    var d = el("div",{"class":"fact"}); d.appendChild(el("div",{"class":"l"},f[0])); d.appendChild(el("div",{"class":"v"},f[1]));
    facts.appendChild(d);
  });
  ORDER.forEach(function(c){ var n=(res.counts||{})[c]; if(!n) return;
    var s = el("span",{"class":"chip k-"+c}, CLASS_JA[c]+" "); s.appendChild(el("b",null,n)); chips.appendChild(s); });
  (res.warnings||[]).slice(0,20).forEach(function(w){ warns.appendChild(el("li",null,w)); });
  files.appendChild(fileLink("out/report.html","レポートを新しいタブで開く",false,true));
  (res.files||[]).forEach(function(f){ files.appendChild(fileLink("out/"+f, f+" ⇩", true)); });
  var fr = $("r-frame"); fr.classList.remove("hidden");
  if(LOCAL){
    LOCAL.file(res.job,"out/report.html").then(function(f){
      if(state.reportUrl) URL.revokeObjectURL(state.reportUrl);
      state.reportUrl = URL.createObjectURL(f.blob); fr.src = state.reportUrl; }); }
  else { fr.src = fileUrl("out/report.html"); }
  $("sec-decks").classList.remove("hidden"); $("sec-collect").classList.remove("hidden");
  $("d-out").textContent = ""; $("c-out").textContent = "";
  setStatus("st-decks",""); setStatus("st-collect","");
  if(res.decks) showDecks(res.decks);
  if(res.collect) showCollect(res.collect);
}

api("GET","/api/default-corners").then(function(c){ $("d-corners").value = JSON.stringify(c, null, 2); }).catch(function(){});
$("btn-decks").addEventListener("click", function(){
  var btn=this; btn.disabled=true; setStatus("st-decks","作成中…");
  api("POST","/api/decks",{job:state.job, corners:$("d-corners").value, sim:$("d-sim").value, portable:$("d-portable").checked})
    .then(function(r){ setStatus("st-decks","作成しました("+r.decks.length+" ファイル)","ok"); showDecks(r.decks); })
    .catch(function(e){ setStatus("st-decks", e.message, "err"); if(e.log){ var p=el("pre",null,e.log); $("d-out").textContent=""; $("d-out").appendChild(p);} })
    .then(function(){ btn.disabled=false; });
});
function showDecks(list){
  var o = $("d-out"); o.textContent = "";
  var f = el("div",{"class":"files"}); f.appendChild(fileLink("decks.zip","decks.zip をダウンロード ⇩",true)); o.appendChild(f);
  o.appendChild(el("pre",null,list.join("\n")));
}

var RX = /\.(mt\d*|ms\d*|measure|lis|log|out)$/i;
$("btn-collect").addEventListener("click", function(){
  var btn=this; btn.disabled=true;
  var thr = {warn_ps:$("c-warn").value, warn_v:$("c-warnv").value, skew_warn_ps:$("c-skew").value};
  var p;
  if(state.cmode === "path"){
    var path=$("c-path").value.trim();
    if(!path){ setStatus("st-collect","パスを入力してください","err"); btn.disabled=false; return; }
    setStatus("st-collect","集計中…"); thr.job=state.job; thr.path=path; p = api("POST","/api/collect", thr);
  } else {
    var files = [].slice.call($("c-dir").files).concat([].slice.call($("c-files").files))
      .filter(function(f){ return RX.test(f.name); });
    if(!files.length){ setStatus("st-collect","測定結果のファイル(.measure / .mt0 / .lis / .log など)が選ばれていません","err"); btn.disabled=false; return; }
    var i = 0;
    p = api("POST","/api/clear-results",{job:state.job}).then(function next(){
      if(i >= files.length){ setStatus("st-collect","集計中…"); thr.job=state.job; return api("POST","/api/collect", thr); }
      var f = files[i++]; setStatus("st-collect","アップロード中… "+i+"/"+files.length);
      var name = f.webkitRelativePath || f.name;
      return api("POST","/api/upload-result?job="+encodeURIComponent(state.job)+"&name="+encodeURIComponent(name), f, true).then(next);
    });
  }
  p.then(function(r){ setStatus("st-collect","集計しました("+r.rows.length+" 指標)","ok"); showCollect(r); })
   .catch(function(e){ setStatus("st-collect", e.message, "err"); if(e.log){ $("c-out").textContent=""; $("c-out").appendChild(el("pre",null,e.log)); } })
   .then(function(){ btn.disabled=false; });
});
function vclass(v){ if(!v) return ""; if(v.indexOf("NG")>=0) return "v-NG"; if(v==="OK") return "v-OK"; return "v-warn"; }
function showCollect(r){
  var o = $("c-out"); o.textContent = "";
  var f = el("div",{"class":"files"}); f.appendChild(fileLink("summary.csv","summary.csv をダウンロード ⇩",true)); o.appendChild(f);
  // show the verdict columns first so they are visible on a phone without scrolling
  // (summary.csv itself keeps the CLI column order)
  var H = r.header, n = H.length;
  var first = ["id","metric","verdict","worst","worst_corner"];
  var order = first.map(function(h){ return H.indexOf(h); }).filter(function(i){ return i>=0; });
  for(var k=0;k<n;k++){ if(order.indexOf(k)<0) order.push(k); }
  var vi = H.indexOf("verdict");
  var wrap = el("div",{"class":"tbl"}); var t = el("table"); var tr = el("tr");
  order.forEach(function(i){ tr.appendChild(el("th",null,H[i])); }); t.appendChild(tr);
  r.rows.forEach(function(row){ var tr=el("tr");
    order.forEach(function(i){ var td=el("td",null,row[i]); if(i===vi){ td.className=vclass(row[i]); } tr.appendChild(td); });
    t.appendChild(tr); });
  wrap.appendChild(t); o.appendChild(wrap);
}

function loadJobs(){
  api("GET","/api/jobs").then(function(r){
    var ul=$("jobs"); ul.textContent="";
    $("jobs-empty").classList.toggle("hidden", r.jobs.length>0);
    r.jobs.forEach(function(j){
      var li=el("li"); var b=el("button",{type:"button"});
      b.appendChild(el("span",{"class":"mono"},j.netlist||j.job));
      b.appendChild(el("span",{"class":"m"},(j.created||"")+"  "+(j.races===null?"未解析":"競合 "+j.races+" 件")));
      b.addEventListener("click", function(){
        api("GET","/api/job?job="+encodeURIComponent(j.job)).then(function(res){ show(res); window.scrollTo({top:$("sec-result").offsetTop-8, behavior:"smooth"}); })
          .catch(function(e){ alert(e.message); });
      });
      li.appendChild(b); ul.appendChild(li);
    });
  }).catch(function(){});
}
loadJobs();
})();
</script>
</body>
</html>
"""
