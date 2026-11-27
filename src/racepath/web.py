"""Local browser UI for racepath (``racepath serve``).

A small single-page app served by the standard library ``http.server``.
It drives the existing CLI subcommands (analyze / decks / collect) in-process,
so detection logic and output formats are exactly those of the CLI.

Security model: the server binds to 127.0.0.1 by default, checks the Host
header (DNS rebinding), and requires a per-process random token on every API
call (CSRF from other web pages). Files are only served from the work dir.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from racepath import __version__

# Same content as examples/corners.json (kept in sync by a test).
DEFAULT_CORNERS = {
    "simulator": "spectre",
    "tran_stop": "12n",
    "tran_step": "1p",
    "t_m": "1n",
    "stimulus": {
        "CLK": {"delay": "1n", "period": "4n", "width": "2n", "tr": "30p"}
    },
    "static": {},
    "supplies": {"VDD": "vdd_m", "VSS": 0},
    "corners": [
        {"name": "tt_0p80v_25c", "lib": ["/path/to/models.scs", "tt"], "vdd": 0.80, "temp": 25},
        {"name": "ss_0p72v_m40c", "lib": ["/path/to/models.scs", "ss"], "vdd": 0.72, "temp": -40},
        {"name": "ss_0p72v_125c", "lib": ["/path/to/models.scs", "ss"], "vdd": 0.72, "temp": 125},
        {"name": "ff_0p88v_m40c", "lib": ["/path/to/models.scs", "ff"], "vdd": 0.88, "temp": -40},
        {"name": "sf_0p72v_m40c", "lib": ["/path/to/models.scs", "sf"], "vdd": 0.72, "temp": -40,
         "params": {"sa_dv_req": 0.07}},
        {"name": "fs_0p72v_m40c", "lib": ["/path/to/models.scs", "fs"], "vdd": 0.72, "temp": -40},
    ],
}

RESULT_EXT = re.compile(r"\.(mt\d*|ms\d*|measure|lis|log|out)$", re.I)
JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
OUTPUT_FILES = ["report.html", "races.meas", "races_params.inc", "races.csv", "races.json"]
CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".zip": "application/zip",
    ".png": "image/png",
}

_RUN_LOCK = threading.Lock()  # redirect_stdout is process-wide


class ApiError(Exception):
    def __init__(self, status, message, log=""):
        super().__init__(message)
        self.status, self.message, self.log = status, message, log


def run_cli(argv):
    """Run ``racepath <argv>`` in-process; return (ok, console text)."""
    from racepath.cli import main

    buf = io.StringIO()
    ok = True
    with _RUN_LOCK, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            main(argv)
        except SystemExit as e:
            if e.code not in (0, None):
                ok = False
                if isinstance(e.code, str):
                    print(e.code)
        except Exception:  # report parser/flatten errors to the UI
            ok = False
            traceback.print_exc()
    return ok, buf.getvalue()


def safe_name(name, default="file"):
    name = re.sub(r"[^A-Za-z0-9._<>+-]", "_", name.strip()).strip("._")
    return name[:120] or default


def safe_relpath(rel):
    parts = [safe_name(p) for p in re.split(r"[\\/]+", rel) if p not in ("", ".", "..")]
    return os.path.join(*parts) if parts else "file"


def within(base, path):
    base = os.path.realpath(base)
    path = os.path.realpath(path)
    return path == base or path.startswith(base + os.sep)


class App:
    def __init__(self, workdir, token):
        self.workdir = os.path.abspath(workdir)
        self.token = token
        os.makedirs(self.workdir, exist_ok=True)
        self._job_lock = threading.Lock()

    # ------------------------------------------------------------------ jobs
    def job_dir(self, job):
        if not isinstance(job, str) or not JOB_RE.match(job):
            raise ApiError(404, "解析ジョブが見つかりません")
        d = os.path.join(self.workdir, job)
        if not os.path.isdir(d) or not within(self.workdir, d):
            raise ApiError(404, f"解析ジョブ {job} が見つかりません")
        return d

    def new_job(self, stem):
        base = time.strftime("%Y%m%d-%H%M%S") + "_" + safe_name(stem, "netlist")
        with self._job_lock:
            job, i = base, 1
            while os.path.exists(os.path.join(self.workdir, job)):
                i += 1
                job = f"{base}-{i}"
            os.makedirs(os.path.join(self.workdir, job))
        return job

    def read_meta(self, d):
        try:
            with open(os.path.join(d, "job.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def write_meta(self, d, meta):
        with open(os.path.join(d, "job.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)

    def list_jobs(self):
        out = []
        for name in sorted(os.listdir(self.workdir), reverse=True):
            d = os.path.join(self.workdir, name)
            if not (JOB_RE.match(name) and os.path.isdir(d)):
                continue
            m = self.read_meta(d)
            races = None
            rj = os.path.join(d, "out", "races.json")
            if os.path.isfile(rj):
                try:
                    with open(rj, encoding="utf-8") as f:
                        races = len(json.load(f)["races"])
                except (OSError, ValueError, KeyError):
                    pass
            out.append({"job": name, "netlist": m.get("netlist_name", ""), "races": races,
                        "created": m.get("created", "")})
        return out[:100]

    def summary(self, job):
        d = self.job_dir(job)
        meta = self.read_meta(d)
        res = {"job": job, "netlist_name": meta.get("netlist_name", ""),
               "netlist_path": meta.get("netlist_path", ""), "options": meta.get("options", {}),
               "log": "", "analyzed": False}
        try:
            with open(os.path.join(d, "analyze.log"), encoding="utf-8") as f:
                res["log"] = f.read()
        except OSError:
            pass
        rj = os.path.join(d, "out", "races.json")
        if os.path.isfile(rj):
            with open(rj, encoding="utf-8") as f:
                data = json.load(f)
            m = data["meta"]
            counts = {}
            for r in data["races"]:
                counts[r["class"]] = counts.get(r["class"], 0) + 1
            res.update({
                "analyzed": True,
                "top": m.get("top"), "format": m.get("format"), "n_mos": m.get("n_mos"),
                "n_ccc": m.get("n_ccc"), "sources": m.get("sources", []),
                "warnings": m.get("warnings", []), "counts": counts,
                "races": [{"id": r["id"], "class": r["class"], "a": r["a"], "b": r["b"],
                           "stages_a": r.get("stages_a"), "stages_b": r.get("stages_b"),
                           "divergence": r.get("divergence"), "group_size": r.get("group_size", 1),
                           "derived": r.get("derived", False), "explain": r.get("explain", "")}
                          for r in data["races"]],
                "files": [f for f in OUTPUT_FILES if os.path.isfile(os.path.join(d, "out", f))],
            })
        if os.path.isfile(os.path.join(d, "decks.zip")):
            res["decks"] = self._deck_files(d)
        if os.path.isfile(os.path.join(d, "summary.csv")):
            res["collect"] = self._read_summary(d)
        return res

    # --------------------------------------------------------------- actions
    def upload_netlist(self, name, stream, length):
        name = safe_name(os.path.basename(name or "netlist.cdl"), "netlist.cdl")
        job = self.new_job(os.path.splitext(name)[0])
        d = os.path.join(self.workdir, job)
        path = os.path.join(d, name)
        _copy_stream(stream, path, length)
        self.write_meta(d, {"netlist_name": name, "netlist_path": path, "uploaded": True,
                            "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        return {"job": job, "netlist_name": name}

    def analyze(self, req):
        job = req.get("job")
        if job:
            d = self.job_dir(job)
            meta = self.read_meta(d)
            netlist = meta.get("netlist_path", "")
        else:
            netlist = os.path.abspath(os.path.expanduser(str(req.get("path", "")).strip()))
            if not req.get("path") or not os.path.isfile(netlist):
                raise ApiError(400, f"ネットリストが見つかりません: {req.get('path', '')}")
            job = self.new_job(os.path.splitext(os.path.basename(netlist))[0])
            d = os.path.join(self.workdir, job)
            meta = {"netlist_name": os.path.basename(netlist), "netlist_path": netlist,
                    "uploaded": False, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        if not os.path.isfile(netlist):
            raise ApiError(400, f"ネットリストが見つかりません: {netlist}")

        out = os.path.join(d, "out")
        if os.path.isdir(out):
            shutil.rmtree(out)
        argv = ["analyze", netlist, "-o", out]
        opts = {}
        fmt = req.get("format") or "auto"
        if fmt not in ("auto", "cdl", "spice", "hspice", "spectre"):
            raise ApiError(400, f"形式 {fmt} には対応していません")
        argv += ["-f", fmt]
        opts["format"] = fmt
        top = str(req.get("top") or "").strip()
        if top:
            argv += ["-t", top]
            opts["top"] = top
        srcs = [s for s in re.split(r"[\s,]+", str(req.get("sources") or "")) if s]
        for s in srcs:
            argv += ["-s", s]
        if srcs:
            opts["sources"] = " ".join(srcs)
        prefix = str(req.get("prefix") or "").strip()
        if prefix:
            argv += ["--prefix", prefix]
            opts["prefix"] = prefix
        if req.get("no_group"):
            argv.append("--no-group")
            opts["no_group"] = True
        cfg_text = str(req.get("config") or "").strip()
        cfg_path = os.path.join(d, "config.json")
        if cfg_text:
            try:
                json.loads(cfg_text)
            except ValueError as e:
                raise ApiError(400, f"設定(JSON)を読めません: {e}")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(cfg_text)
            argv += ["-c", cfg_path]
            opts["config"] = cfg_text
        elif os.path.exists(cfg_path):
            os.remove(cfg_path)
        argv += ["--show", "200"]

        ok, log = run_cli(argv)
        with open(os.path.join(d, "analyze.log"), "w", encoding="utf-8") as f:
            f.write(log)
        meta["options"] = opts
        self.write_meta(d, meta)
        for stale in ("decks", "decks.zip", "summary.csv"):
            p = os.path.join(d, stale)
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.exists(p):
                os.remove(p)
        if not ok:
            raise ApiError(422, "解析に失敗しました。ログを確認してください。", log)
        return self.summary(job)

    def decks(self, req):
        d = self.job_dir(req.get("job"))
        rj = os.path.join(d, "out", "races.json")
        if not os.path.isfile(rj):
            raise ApiError(400, "先に解析を実行してください")
        text = str(req.get("corners") or "").strip()
        try:
            json.loads(text)
        except ValueError as e:
            raise ApiError(400, f"コーナー定義(JSON)を読めません: {e}")
        cpath = os.path.join(d, "corners.json")
        with open(cpath, "w", encoding="utf-8") as f:
            f.write(text)
        sim = req.get("sim") or "spectre"
        if sim not in ("spectre", "hspice"):
            raise ApiError(400, f"シミュレータ {sim} には対応していません")
        ddir = os.path.join(d, "decks")
        if os.path.isdir(ddir):
            shutil.rmtree(ddir)
        ok, log = run_cli(["decks", rj, cpath, "-o", ddir, "--sim", sim])
        if not ok:
            raise ApiError(422, "デッキを作れませんでした。", log)
        zpath = os.path.join(d, "decks.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(os.listdir(ddir)):
                info = zipfile.ZipInfo(os.path.join("decks", name), time.localtime()[:6])
                info.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(os.path.join(ddir, name), "rb") as f:
                    z.writestr(info, f.read())
        return {"log": log, "decks": self._deck_files(d)}

    def _deck_files(self, d):
        ddir = os.path.join(d, "decks")
        return sorted(os.listdir(ddir)) if os.path.isdir(ddir) else []

    def upload_result(self, job, rel, stream, length):
        d = self.job_dir(job)
        rel = safe_relpath(rel or "result.measure")
        if not RESULT_EXT.search(rel):
            _drain(stream, length)
            return {"skipped": rel}
        rdir = os.path.join(d, "results")
        path = os.path.join(rdir, rel)
        if not within(rdir, path):
            raise ApiError(400, "不正なファイル名です")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _copy_stream(stream, path, length)
        return {"saved": rel}

    def clear_results(self, job):
        d = self.job_dir(job)
        rdir = os.path.join(d, "results")
        if os.path.isdir(rdir):
            shutil.rmtree(rdir)
        return {"ok": True}

    def collect(self, req):
        d = self.job_dir(req.get("job"))
        rj = os.path.join(d, "out", "races.json")
        if not os.path.isfile(rj):
            raise ApiError(400, "先に解析を実行してください")
        path = str(req.get("path") or "").strip()
        if path:
            src = os.path.abspath(os.path.expanduser(path))
            if not os.path.exists(src):
                raise ApiError(400, f"結果のフォルダ/ファイルが見つかりません: {path}")
        else:
            src = os.path.join(d, "results")
            if not os.path.isdir(src):
                raise ApiError(400, "結果ファイルをアップロードするか、フォルダのパスを指定してください")
        argv = ["collect", rj, src, "-o", os.path.join(d, "summary.csv")]
        for key, flag, scale in (("warn_ps", "--warn", 1e-12), ("warn_v", "--warn-v", 1.0),
                                 ("skew_warn_ps", "--skew-warn", 1e-12)):
            v = req.get(key)
            if v not in (None, ""):
                try:
                    argv += [flag, repr(float(v) * scale)]
                except (TypeError, ValueError):
                    raise ApiError(400, f"{key} が数値ではありません: {v}")
        sp = os.path.join(d, "summary.csv")
        if os.path.exists(sp):
            os.remove(sp)
        ok, log = run_cli(argv)
        if not ok:
            raise ApiError(422, "集計できませんでした。", log)
        res = self._read_summary(d)
        res["log"] = log
        return res

    def _read_summary(self, d):
        with open(os.path.join(d, "summary.csv"), encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        return {"header": rows[0] if rows else [], "rows": rows[1:]}


def _copy_stream(stream, path, length):
    remaining = length
    with open(path, "wb") as f:
        while remaining > 0:
            chunk = stream.read(min(1 << 20, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)


def _drain(stream, length):
    while length > 0:
        chunk = stream.read(min(1 << 20, length))
        if not chunk:
            break
        length -= len(chunk)


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
            d = app.job_dir(parts[0])
            rel = parts[1]
            allowed = rel in ("decks.zip", "summary.csv") or \
                (rel.startswith("out/") and rel[4:] in OUTPUT_FILES)
            path = os.path.join(d, *rel.split("/"))
            if not allowed or not within(d, path) or not os.path.isfile(path):
                return self._error(404, "見つかりません")
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
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere}
</style>
</head>
<body>
<header>
  <h1>racepath ローカル <span class="sub">v__VERSION__</span></h1>
  <div class="sub">ネットリストからレーシングパスを検出します。データはこのPCの外には出ません。</div>
</header>
<main>

<section id="sec-analyze">
  <h2><span class="n">1</span>解析</h2>
  <div class="tabs" role="tablist">
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
  <div class="tabs" role="tablist">
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
function api(method, path, body, raw){
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
  var a = el("a",{href:fileUrl("out/report.html"),target:"_blank",rel:"noopener"},"レポートを新しいタブで開く");
  files.appendChild(a);
  (res.files||[]).forEach(function(f){ files.appendChild(el("a",{href:fileUrl("out/"+f,true)},f+" ⇩")); });
  var fr = $("r-frame"); fr.classList.remove("hidden"); fr.src = fileUrl("out/report.html");
  $("sec-decks").classList.remove("hidden"); $("sec-collect").classList.remove("hidden");
  $("d-out").textContent = ""; $("c-out").textContent = "";
  setStatus("st-decks",""); setStatus("st-collect","");
  if(res.decks) showDecks(res.decks);
  if(res.collect) showCollect(res.collect);
}

api("GET","/api/default-corners").then(function(c){ $("d-corners").value = JSON.stringify(c, null, 2); }).catch(function(){});
$("btn-decks").addEventListener("click", function(){
  var btn=this; btn.disabled=true; setStatus("st-decks","作成中…");
  api("POST","/api/decks",{job:state.job, corners:$("d-corners").value, sim:$("d-sim").value})
    .then(function(r){ setStatus("st-decks","作成しました("+r.decks.length+" ファイル)","ok"); showDecks(r.decks); })
    .catch(function(e){ setStatus("st-decks", e.message, "err"); if(e.log){ var p=el("pre",null,e.log); $("d-out").textContent=""; $("d-out").appendChild(p);} })
    .then(function(){ btn.disabled=false; });
});
function showDecks(list){
  var o = $("d-out"); o.textContent = "";
  var f = el("div",{"class":"files"}); f.appendChild(el("a",{href:fileUrl("decks.zip")},"decks.zip をダウンロード ⇩")); o.appendChild(f);
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
  var f = el("div",{"class":"files"}); f.appendChild(el("a",{href:fileUrl("summary.csv",true)},"summary.csv をダウンロード ⇩")); o.appendChild(f);
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
