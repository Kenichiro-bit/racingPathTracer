#!/usr/bin/env python3
"""Build dist/racepath.html: a single-file, fully offline racepath.

The page is the same UI as ``racepath serve`` (racepath.web.INDEX_HTML), but its
API calls are answered by Pyodide (CPython compiled to WebAssembly) running the
racepath package inside the browser. Pyodide, the Python standard library and
racepath's sources are embedded gzip+base64, and ``fetch`` is redirected to the
embedded bytes, so the file works from file:// with no network and no install.

usage:
  python scripts/build_offline_html.py                 # downloads Pyodide once into build/
  python scripts/build_offline_html.py --tarball pyodide-0.29.5.tgz -o dist/racepath.html

Only the standard library is used.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

PYODIDE_VERSION = "0.29.5"
PYODIDE_SHA256 = "6749b10ee515a1458ecea36d860c8ce2a48366e85b3f92d749ad1840e4aff181"
PYODIDE_URL = f"https://registry.npmjs.org/pyodide/-/pyodide-{PYODIDE_VERSION}.tgz"
PYODIDE_FILES = ["pyodide.asm.wasm", "python_stdlib.zip", "pyodide.asm.js", "pyodide.js",
                 "pyodide-lock.json"]
RACEPATH_FILES = ["__init__.py", "cli.py", "jobs.py"]  # web.py (HTTP server) is not needed

# Python side of the bridge: routes the same API paths as the HTTP server to racepath.jobs.App.
BRIDGE_PY = r'''
import json, sys
sys.path.insert(0, "/racepath/lib")
from racepath.jobs import App, ApiError, DEFAULT_CORNERS

_app = App("/racepath/work", "offline", portable_decks=True)


def _route(method, path, q, body, data):
    if method == "GET":
        if path == "/api/jobs":
            return {"jobs": _app.list_jobs()}
        if path == "/api/job":
            return _app.summary(q.get("job", ""))
        if path == "/api/default-corners":
            return DEFAULT_CORNERS
    else:
        if path in ("/api/upload", "/api/upload-result"):
            import io
            raw = data.to_bytes() if data is not None else b""
            if path == "/api/upload":
                return _app.upload_netlist(q.get("name", ""), io.BytesIO(raw), len(raw))
            return _app.upload_result(q.get("job"), q.get("name", ""), io.BytesIO(raw), len(raw))
        if path == "/api/analyze":
            if body.get("path"):
                raise ApiError(400, "オフライン版ではパスを指定できません。ファイルを選んでください。")
            return _app.analyze(body)
        if path == "/api/decks":
            return _app.decks(body)
        if path == "/api/collect":
            if body.get("path"):
                raise ApiError(400, "オフライン版ではパスを指定できません。ファイルを選んでください。")
            return _app.collect(body)
        if path == "/api/clear-results":
            return _app.clear_results(body.get("job"))
    raise ApiError(404, "見つかりません")


def rp_call(method, path, query_json, body_json, data=None):
    try:
        body = json.loads(body_json) if body_json else {}
        res = {"status": 200, "body": _route(method, path, json.loads(query_json), body, data)}
    except ApiError as e:
        res = {"status": e.status, "body": {"error": e.message, "log": e.log}}
    except Exception:
        import traceback
        res = {"status": 500, "body": {"error": "内部エラーです", "log": traceback.format_exc()}}
    return json.dumps(res, ensure_ascii=False)


def rp_file(job, rel):
    try:
        return json.dumps({"path": _app.file_path(job, rel)})
    except ApiError as e:
        return json.dumps({"error": e.message}, ensure_ascii=False)
'''

# JavaScript side: boots Pyodide from the embedded assets and exposes window.racepathLocal,
# which INDEX_HTML uses instead of the HTTP API.
BACKEND_JS = r'''
(function(){
"use strict";
var A = JSON.parse(document.getElementById("rp-assets").textContent);
var BASE = "https://racepath.invalid/pyodide/";
function b64(s){ var bin = atob(s), n = bin.length, u = new Uint8Array(n);
  for(var i=0;i<n;i++) u[i] = bin.charCodeAt(i); return u; }
function gunzip(s){
  var st = new Blob([b64(s)]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Response(st).arrayBuffer().then(function(b){ return new Uint8Array(b); });
}
function loadScript(bytes){
  return new Promise(function(res, rej){
    var url = URL.createObjectURL(new Blob([bytes], {type:"text/javascript"}));
    var s = document.createElement("script");
    s.src = url; s.onload = function(){ URL.revokeObjectURL(url); res(); };
    s.onerror = function(){ rej(new Error("埋め込みスクリプトを読み込めません")); };
    document.head.appendChild(s);
  });
}
var py = null, call = null, fileOf = null;
var ready = (async function(){
  if(typeof DecompressionStream === "undefined" || typeof WebAssembly === "undefined"){
    throw new Error("このブラウザでは動きません。新しい Chrome / Edge / Firefox / Safari で開いてください。");
  }
  var P = A.pyodide;
  var got = await Promise.all(["pyodide.asm.wasm","python_stdlib.zip","pyodide.asm.js","pyodide.js","pyodide-lock.json"]
    .map(function(k){ return gunzip(P[k]); }));
  var files = {"pyodide.asm.wasm": [got[0], "application/wasm"], "python_stdlib.zip": [got[1], "application/zip"]};
  var origFetch = window.fetch;
  // Pyodide fetches its wasm and stdlib relative to indexURL; serve them from memory.
  window.fetch = function(input, init){
    var url = (typeof input === "string") ? input : ((input && input.url) || String(input));
    if(url.indexOf(BASE) === 0){
      var f = files[url.slice(BASE.length)];
      if(f) return Promise.resolve(new Response(f[0], {status:200, headers:{"Content-Type": f[1]}}));
      return Promise.reject(new Error("オフライン版に含まれていないファイルです: " + url));
    }
    return origFetch.apply(window, arguments);
  };
  await loadScript(got[2]);   // pyodide.asm.js -> globalThis._createPyodideModule
  await loadScript(got[3]);   // pyodide.js     -> globalThis.loadPyodide
  py = await globalThis.loadPyodide({indexURL: BASE, lockFileContents: new TextDecoder().decode(got[4]),
                                     stdout: function(){}, stderr: function(){}});
  files = null;
  py.FS.mkdirTree("/racepath/lib/racepath");
  for(var name in A.racepath){
    py.FS.writeFile("/racepath/lib/racepath/" + name, await gunzip(A.racepath[name]));
  }
  py.runPython(A.bridge);
  call = py.globals.get("rp_call");
  fileOf = py.globals.get("rp_file");
})();
function tick(){ return new Promise(function(r){ setTimeout(r, 30); }); }  // let the status text paint
var TYPES = {html:"text/html", csv:"text/csv", json:"application/json", zip:"application/zip"};
window.racepathLocal = {
  ready: ready,
  api: async function(method, path, body, raw){
    await ready;
    var u = new URL(path, "http://offline/"), q = {};
    u.searchParams.forEach(function(v, k){ q[k] = v; });
    var data = null, bj = "";
    if(raw){ data = new Uint8Array(await body.arrayBuffer()); }
    else if(body !== undefined){ bj = JSON.stringify(body); }
    await tick();
    var out = JSON.parse(call(method, u.pathname, JSON.stringify(q), bj, data));
    if(out.status !== 200){ var e = new Error(out.body.error || ("エラー " + out.status)); e.log = out.body.log || ""; throw e; }
    return out.body;
  },
  file: async function(job, rel){
    await ready;
    var r = JSON.parse(fileOf(job, rel));
    if(r.error) throw new Error(r.error);
    var ext = rel.split(".").pop().toLowerCase();
    var name = rel.split("/").pop();
    if(name === "decks.zip") name = job + "_decks.zip";
    return {name: name, blob: new Blob([py.FS.readFile(r.path)], {type: TYPES[ext] || "text/plain"})};
  }
};
})();
'''

NOTICE = ("このファイルには Pyodide {ver}(Mozilla Public License 2.0)と、"
          "CPython の標準ライブラリ(Python Software Foundation License)が含まれています。"
          "解析はすべてこのブラウザの中で行い、ネットリストを外部に送信しません。")


def _gz_b64(data: bytes) -> str:
    return base64.b64encode(gzip.compress(data, compresslevel=9, mtime=0)).decode("ascii")


def fetch_tarball(cache_dir: str) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"pyodide-{PYODIDE_VERSION}.tgz")
    if not os.path.isfile(path):
        print(f"downloading {PYODIDE_URL}")
        with urllib.request.urlopen(PYODIDE_URL) as r, open(path + ".part", "wb") as f:
            f.write(r.read())
        os.replace(path + ".part", path)
    return path


def read_pyodide(tarball: str) -> dict:
    with open(tarball, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != PYODIDE_SHA256:
        raise SystemExit(f"checksum mismatch for {tarball}: {digest} (expected {PYODIDE_SHA256})")
    out = {}
    with tarfile.open(tarball, "r:gz") as t:
        meta = json.load(t.extractfile("package/package.json"))
        if meta.get("version") != PYODIDE_VERSION:
            raise SystemExit(f"unexpected Pyodide version {meta.get('version')}")
        for name in PYODIDE_FILES:
            out[name] = t.extractfile(f"package/{name}").read()
    return out


def build(tarball: str, out_path: str) -> int:
    from racepath import __version__
    from racepath.web import INDEX_HTML

    pyo = read_pyodide(tarball)
    src = os.path.join(ROOT, "src", "racepath")
    assets = {
        "pyodide": {k: _gz_b64(v) for k, v in pyo.items()},
        "racepath": {n: _gz_b64(open(os.path.join(src, n), "rb").read()) for n in RACEPATH_FILES},
        "bridge": BRIDGE_PY,
    }
    assets_json = json.dumps(assets, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    notice = NOTICE.format(ver=PYODIDE_VERSION)
    backend = (f'<script type="application/json" id="rp-assets">{assets_json}</script>\n'
               f"<script>{BACKEND_JS}</script>\n"
               f'<footer class="note" style="max-width:1100px;margin:0 auto;padding:0 16px 24px">{notice}</footer>')

    page = INDEX_HTML
    for old, new in (("<title>racepath ローカル</title>", "<title>racepath オフライン版</title>"),
                     ("<h1>racepath ローカル <span", "<h1>racepath オフライン版 <span"),
                     ("<!--LOCAL_BACKEND-->", backend)):
        if page.count(old) != 1:
            raise SystemExit(f"template marker not found exactly once: {old!r}")
        page = page.replace(old, new)
    page = page.replace("__TOKEN__", "offline").replace("__VERSION__", __version__)
    page = page.replace("<!doctype html>", f"<!doctype html>\n<!-- racepath {__version__} offline build"
                        f" / Pyodide {PYODIDE_VERSION} (MPL-2.0) / CPython stdlib (PSF License) -->", 1)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    data = page.encode("utf-8")
    with open(out_path, "wb") as f:
        f.write(data)
    return len(data)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tarball", help=f"pyodide-{PYODIDE_VERSION}.tgz from the npm registry "
                                      "(default: download into build/)")
    ap.add_argument("-o", "--out", default=os.path.join(ROOT, "dist", "racepath.html"))
    args = ap.parse_args(argv)
    tarball = args.tarball or fetch_tarball(os.path.join(ROOT, "build"))
    size = build(tarball, args.out)
    print(f"wrote {args.out} ({size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
