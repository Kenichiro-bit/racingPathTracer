"""Tests for the local browser UI server (racepath serve)."""
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest

from racepath import web

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
TOKEN = "test-token"


@pytest.fixture()
def server(tmp_path):
    httpd = web.make_server("127.0.0.1", 0, str(tmp_path / "work"), token=TOKEN)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
    httpd.shutdown()
    httpd.server_close()


def call(base, method, path, body=None, raw=False, token=TOKEN, headers=None):
    h = dict(headers or {})
    if token is not None:
        h["X-Racepath-Token"] = token
    data = None
    if body is not None:
        data = body if raw else json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/octet-stream" if raw else "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def j(resp):
    return json.loads(resp[1].decode("utf-8"))


def upload_and_analyze(base, netlist, **opts):
    st, body, _ = call(base, "POST", "/api/upload?name=" + urllib.parse.quote(netlist.name),
                       netlist.read_bytes(), raw=True)
    assert st == 200, body
    job = json.loads(body)["job"]
    opts["job"] = job
    resp = call(base, "POST", "/api/analyze", opts)
    assert resp[0] == 200, resp[1]
    return job, j(resp)


EXPECTED = {
    ("sae_bl", "SAE", "WL0"), ("wl_pre", "PRE_N", "WL0"), ("hold", "CKBI", "SAT"),
    ("hold", "CKI", "SAT"), ("clock_pair", "CKBI", "CKI"), ("generic", "PRE_N", "SAE"),
}


def test_index_has_token_and_japanese_ui(server):
    base, _ = server
    st, body, hdr = call(base, "GET", "/", token=None)
    html = body.decode("utf-8")
    assert st == 200 and hdr["Content-Type"].startswith("text/html")
    assert TOKEN in html and "解析する" in html and 'name="viewport"' in html


def test_api_requires_token(server):
    base, _ = server
    assert call(base, "GET", "/api/jobs", token=None)[0] == 403
    assert call(base, "POST", "/api/analyze", {"path": "x"}, token="wrong")[0] == 403
    assert call(base, "GET", "/api/jobs")[0] == 200


def test_rejects_foreign_host_header(server):
    base, _ = server
    st, _, _ = call(base, "GET", "/", headers={"Host": "evil.example:8765"})
    assert st == 403


def test_upload_analyze_six_races(server):
    base, _ = server
    job, res = upload_and_analyze(base, EXAMPLES / "sram_demo.cdl")
    assert res["analyzed"] and res["top"] == "SRAM_DEMO" and res["n_mos"] == 60
    assert {(r["a"], r["b"]) for r in res["races"]} == {(a, b) for _, a, b in EXPECTED}
    assert res["pins"] == [{"pin": "CLK", "count": 6}] and res["manual"] == 0
    assert "races: 6" in res["log"]
    assert set(res["files"]) == set(web.OUTPUT_FILES)
    st, body, hdr = call(base, "GET", f"/files/{job}/out/report.html", token=None)
    assert st == 200 and b"rp006" in body and hdr["Content-Type"].startswith("text/html")
    st, body, hdr = call(base, "GET", f"/files/{job}/out/races.meas?dl=1", token=None)
    assert st == 200 and "attachment" in hdr["Content-Disposition"] and b".measure" in body
    # job list and reopen
    jobs = j(call(base, "GET", "/api/jobs"))["jobs"]
    assert jobs[0]["job"] == job and jobs[0]["races"] == 6
    again = j(call(base, "GET", "/api/job?job=" + job))
    assert len(again["races"]) == 6


def test_analyze_by_path_spectre_with_options(server):
    base, _ = server
    resp = call(base, "POST", "/api/analyze", {"path": str(EXAMPLES / "sram_demo.scs"),
                                                "top": "SRAM_DEMO", "sources": "CLK",
                                                "no_group": True, "config": '{"vdd": 0.75}'})
    assert resp[0] == 200, resp[1]
    res = j(resp)
    assert res["format"] == "spectre"
    assert {(r["a"], r["b"]) for r in res["races"]} == {(a, b) for _, a, b in EXPECTED}
    assert res["options"]["top"] == "SRAM_DEMO" and res["options"]["no_group"] is True


def test_analyze_with_static_inputs(server):
    base, _ = server
    case = ROOT / "tests" / "cases" / "testmode.cdl"
    res = j(call(base, "POST", "/api/analyze", {"path": str(case)}))
    assert ("TMB", "TMBB") in {(r["a"], r["b"]) for r in res["races"]}
    res = j(call(base, "POST", "/api/analyze", {"path": str(case), "static": "TM, scan_en"}))
    assert res["sources"] == ["CLK"] and res["static_inputs"] == ["TM"]
    assert {(r["a"], r["b"]) for r in res["races"]} == {(a, b) for _, a, b in EXPECTED}
    assert res["options"]["static"] == "TM scan_en"
    assert "static inputs: TM" in res["log"]


def test_analyze_errors_are_reported(server, tmp_path):
    base, _ = server
    st, body, _ = call(base, "POST", "/api/analyze", {"path": str(tmp_path / "missing.cdl")})
    assert st == 400 and "見つかりません" in json.loads(body)["error"]
    st, body, _ = call(base, "POST", "/api/analyze",
                       {"path": str(EXAMPLES / "sram_demo.cdl"), "config": "{not json"})
    assert st == 400 and "JSON" in json.loads(body)["error"]
    st, body, _ = call(base, "POST", "/api/analyze",
                       {"path": str(EXAMPLES / "sram_demo.cdl"), "top": "NO_SUCH_CELL"})
    assert st == 422
    assert json.loads(body)["log"]


def test_file_serving_is_restricted(server):
    base, httpd = server
    job, _ = upload_and_analyze(base, EXAMPLES / "sram_demo.cdl")
    for p in (f"/files/{job}/sram_demo.cdl", f"/files/{job}/job.json",
              f"/files/{job}/out/../job.json", "/files/../../etc/passwd",
              f"/files/{job}/out/%2e%2e/job.json", "/files/nojob/out/report.html"):
        assert call(base, "GET", p, token=None)[0] == 404, p


def test_decks_zip(server):
    base, _ = server
    job, _ = upload_and_analyze(base, EXAMPLES / "sram_demo.cdl")
    corners = j(call(base, "GET", "/api/default-corners"))
    resp = call(base, "POST", "/api/decks", {"job": job, "corners": json.dumps(corners),
                                             "sim": "hspice"})
    assert resp[0] == 200, resp[1]
    decks = j(resp)["decks"]
    assert "run_all.sh" in decks and "tt_0p80v_25c.sp" in decks
    st, body, hdr = call(base, "GET", f"/files/{job}/decks.zip", token=None)
    assert st == 200 and "attachment" in hdr["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = z.namelist()
        assert "decks/run_all.sh" in names
        assert z.getinfo("decks/run_all.sh").external_attr >> 16 & 0o111
    assert call(base, "POST", "/api/decks", {"job": job, "corners": "{bad"})[0] == 400


def test_default_corners_match_example():
    assert web.DEFAULT_CORNERS == json.loads((EXAMPLES / "corners.json").read_text())


def test_collect_upload_with_folder_paths(server):
    base, httpd = server
    job, _ = upload_and_analyze(base, EXAMPLES / "sram_demo.cdl")
    # a path that tries to escape is flattened into the job's results folder
    st, body, _ = call(base, "POST", f"/api/upload-result?job={job}&name=" + urllib.parse.quote("../../escape.measure"),
                       b"rp006_margin = 1\n", raw=True)
    assert st == 200 and json.loads(body)["saved"] == "escape.measure"
    assert (Path(httpd.app.workdir) / job / "results" / "escape.measure").is_file()
    assert call(base, "POST", "/api/clear-results", {"job": job})[0] == 200
    files = {
        "decks/tt_0p80v_25c.raw/tran1.measure":
            "rp006_margin = 120m\nrp001_margin = 50p\nrp001_margin_end = 40p\n"
            "rp003_margin = 30p\nrp004_margin = 25p\nrp002_skew = 5p\nrp005_skew = 100p\n",
        "decks/ss_0p72v_m40c.mt0":
            "$DATA1 SOURCE='HSPICE'\n.TITLE 'deck'\n"
            "rp006_margin rp001_margin rp001_margin_end rp003_margin rp004_margin "
            "rp002_skew rp005_skew temper alter#\n"
            "1.000e-02 -1.0e-11 3.0e-11 1.0e-11 failed -5.0e-12 9.0e-11 -40.0 1\n",
        "decks/tt_0p80v_25c.raw/tran1.tran.tran": "binary waveform, must be skipped",
    }
    for name, text in files.items():
        st, body, _ = call(base, "POST", f"/api/upload-result?job={job}&name=" + urllib.parse.quote(name),
                           text.encode(), raw=True)
        assert st == 200, body
        if name.endswith(".tran"):
            assert "skipped" in json.loads(body)
    resp = call(base, "POST", "/api/collect", {"job": job, "warn_ps": "20", "warn_v": "0.02"})
    assert resp[0] == 200, resp[1]
    res = j(resp)
    hdr = res["header"]
    assert "tt_0p80v_25c" in hdr and "ss_0p72v_m40c" in hdr
    rows = {(r[0], r[1]): dict(zip(hdr, r)) for r in res["rows"]}
    assert rows[("rp001", "margin")]["verdict"] == "NG"
    assert rows[("rp002", "skew")]["verdict"] == "順序反転"
    assert call(base, "GET", f"/files/{job}/summary.csv?dl=1", token=None)[0] == 200
    # stricter time threshold turns 25ps/30ps hold margins into warnings
    res = j(call(base, "POST", "/api/collect", {"job": job, "warn_ps": "1000"}))
    rows = {(r[0], r[1]): dict(zip(res["header"], r)) for r in res["rows"]}
    assert rows[("rp001", "margin_end")]["verdict"] == "要注意"


def test_collect_by_path(server, tmp_path):
    base, _ = server
    job, _ = upload_and_analyze(base, EXAMPLES / "sram_demo.cdl")
    d = tmp_path / "sim"
    d.mkdir()
    (d / "tt.measure").write_text("rp006_margin = 0.2\n")
    res = j(call(base, "POST", "/api/collect", {"job": job, "path": str(d)}))
    assert res["header"][4] == "tt"
    assert call(base, "POST", "/api/collect", {"job": job, "path": str(d / "nope")})[0] == 400


def test_serve_help():
    import subprocess
    import sys
    p = subprocess.run([sys.executable, "-m", "racepath", "serve", "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0 and "--port" in p.stdout


def test_portable_decks_bundle_netlist_and_meas(server):
    base, httpd = server
    job, _ = upload_and_analyze(base, EXAMPLES / "sram_demo.cdl")
    corners = json.dumps(j(call(base, "GET", "/api/default-corners")))
    res = j(call(base, "POST", "/api/decks", {"job": job, "corners": corners, "sim": "spectre",
                                              "portable": True}))
    assert res["portable"] is True
    assert {"races.meas", "sram_demo.cdl", "run_all.sh"} <= set(res["decks"])
    work = Path(httpd.app.workdir)
    deck = (work / job / "decks" / "tt_0p80v_25c.scs").read_text()
    assert ".include 'sram_demo.cdl'" in deck and ".include 'races.meas'" in deck
    assert str(work) not in deck
    # default (serve): absolute paths, nothing bundled
    res = j(call(base, "POST", "/api/decks", {"job": job, "corners": corners, "sim": "hspice"}))
    assert res["portable"] is False and "races.meas" not in res["decks"]
    deck = (work / job / "decks" / "tt_0p80v_25c.sp").read_text()
    assert str(work / job / "out" / "races.meas") in deck
