"""Job management shared by the local browser UI (``racepath serve``) and the
offline single-file HTML build.

Every action runs the existing CLI subcommands (analyze / decks / collect)
in-process, so detection logic and output formats are exactly those of the CLI.
This module has no HTTP dependency so that it also runs inside Pyodide.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import re
import shutil
import threading
import time
import traceback
import zipfile

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
    def __init__(self, workdir, token, portable_decks=False):
        self.workdir = os.path.abspath(workdir)
        self.token = token
        # always bundle netlist + races.meas into the deck zip (offline HTML build,
        # where the analysis runs on a virtual file system)
        self.portable_decks = portable_decks
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
                "static_inputs": m.get("static_inputs", []),
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
        stat = [s for s in re.split(r"[\s,]+", str(req.get("static") or "")) if s]
        for s in stat:
            argv += ["--static", s]
        if stat:
            opts["static"] = " ".join(stat)
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
        portable = bool(req.get("portable")) or self.portable_decks
        if portable:
            self._make_portable(rj, ddir)
        zpath = os.path.join(d, "decks.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(os.listdir(ddir)):
                info = zipfile.ZipInfo(os.path.join("decks", name), time.localtime()[:6])
                info.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(os.path.join(ddir, name), "rb") as f:
                    z.writestr(info, f.read())
        return {"log": log, "decks": self._deck_files(d), "portable": portable}

    @staticmethod
    def _make_portable(races_json, ddir):
        """Copy the netlist and races.meas next to the decks and refer to them by
        file name, so the deck folder can be moved to another machine."""
        with open(races_json, encoding="utf-8") as f:
            netlist = json.load(f)["meta"]["netlist"]
        meas = os.path.join(os.path.dirname(races_json), "races.meas")
        decks = [n for n in os.listdir(ddir) if n.endswith((".scs", ".sp"))]
        taken = set(os.listdir(ddir)) | {"races.meas"}
        net_name = os.path.basename(netlist)
        if net_name in taken:
            net_name = "netlist_" + net_name
        shutil.copyfile(meas, os.path.join(ddir, "races.meas"))
        shutil.copyfile(netlist, os.path.join(ddir, net_name))
        for n in decks:
            fp = os.path.join(ddir, n)
            with open(fp, encoding="utf-8") as f:
                txt = f.read()
            for old, new in ((netlist, net_name), (os.path.abspath(meas), "races.meas")):
                txt = txt.replace(f"'{old}'", f"'{new}'").replace(f'"{old}"', f'"{new}"')
            with open(fp, "w", encoding="utf-8") as f:
                f.write(txt)

    def file_path(self, job, rel):
        """Absolute path of a downloadable job file, or ApiError(404).
        Only the analysis outputs, decks.zip and summary.csv are exposed."""
        d = self.job_dir(job)
        allowed = rel in ("decks.zip", "summary.csv") or \
            (rel.startswith("out/") and rel[4:] in OUTPUT_FILES)
        path = os.path.join(d, *rel.split("/"))
        if not allowed or not within(d, path) or not os.path.isfile(path):
            raise ApiError(404, "見つかりません")
        return path

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
