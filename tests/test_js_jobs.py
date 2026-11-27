"""The in-browser job store (src/racepath/js/jobs.js) gives the page the same results as the
Python CLI, and its deck zip is valid and self-contained. Skipped without node."""
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_jobs_flow_matches_cli(tmp_path):
    res = tmp_path / "res"
    (res / "decks" / "tt_0p80v_25c.raw").mkdir(parents=True)
    (res / "decks" / "tt_0p80v_25c.raw" / "tran1.measure").write_text(
        "rp001_margin = 120m\nrp002_margin = 50p\nrp002_margin_end = 40p\n"
        "rp003_margin = 30p\nrp004_margin = 25p\nrp005_skew = 5p\nrp006_skew = 100p\n")
    (res / "decks" / "ss.mt0").write_text(
        "$DATA1\n.TITLE 'x'\nrp001_margin rp002_margin rp002_margin_end rp003_margin rp004_margin "
        "rp005_skew rp006_skew temper alter#\n1.0e-02 -1.0e-11 3.0e-11 1.0e-11 failed -5.0e-12 9.0e-11 -40 1\n")
    (res / "decks" / "wave.tran").write_text("binary")
    out = tmp_path / "out"
    subprocess.run([NODE, str(ROOT / "tests/js/jobs_flow.js"), str(EXAMPLES / "sram_demo.cdl"),
                    str(EXAMPLES / "corners.json"), str(res), str(out)], check=True)
    flow = json.loads((out / "flow.json").read_text())

    # same analysis as the CLI run with the same netlist name (the page has no real path)
    cli = tmp_path / "cli"
    cli.mkdir()
    shutil.copy(EXAMPLES / "sram_demo.cdl", cli / "sram_demo.cdl")
    p = subprocess.run([sys.executable, "-m", "racepath", "analyze", "sram_demo.cdl", "-o", "out", "--show", "200"],
                       cwd=cli, capture_output=True, text=True, check=True)
    assert flow["analyze"]["log"] == p.stdout
    for n in ("races.meas", "races_params.inc", "races.csv", "report.html", "races.json"):
        a = (cli / "out" / n).read_bytes().decode()
        if n in ("races.json", "report.html"):   # meta.netlist: absolute path in the CLI, file name in the page
            a = a.replace(str(cli / "sram_demo.cdl"), "sram_demo.cdl")
        assert a == (out / n).read_bytes().decode(), n
    assert flow["analyze"]["counts"] == {"sae_bl": 1, "wl_pre": 1, "hold": 2, "clock_pair": 1, "generic": 1}

    # decks zip: valid, self-contained, run_all.sh executable
    zname = [z for z in out.iterdir() if z.name.endswith("_decks.zip")][0]
    with zipfile.ZipFile(zname) as z:
        assert z.testzip() is None
        names = z.namelist()
        assert {"decks/races.meas", "decks/sram_demo.cdl", "decks/run_all.sh", "decks/tt_0p80v_25c.sp"} <= set(names)
        assert (z.getinfo("decks/run_all.sh").external_attr >> 16) & 0o111
        deck = z.read("decks/tt_0p80v_25c.sp").decode()
        assert ".include 'sram_demo.cdl'" in deck and ".include 'races.meas'" in deck
        assert z.read("decks/sram_demo.cdl") == (EXAMPLES / "sram_demo.cdl").read_bytes()
    assert flow["decks"]["portable"] is True

    # collect: binary waveform skipped, folder structure kept for corner names
    assert {"skipped": "decks/wave.tran"} in flow["uploads"]
    hdr = flow["collect"]["header"]
    assert hdr[5:7] == ["ss", "tt_0p80v_25c"]
    rows = {(r[0], r[2]): dict(zip(hdr, r)) for r in flow["collect"]["rows"]}
    assert rows[("rp002", "margin")]["verdict"] == "NG"
    assert rows[("rp005", "skew")]["verdict"] == "順序反転"
    assert (out / "summary.csv").read_bytes().startswith(b"\xef\xbb\xbf")

    # job list / summary for re-display
    assert flow["jobs"]["jobs"][0]["races"] == 6
    assert flow["summary"]["collect"]["rows"] == flow["collect"]["rows"]
    assert flow["summary"]["decks"] == flow["decks"]["decks"]

    e = flow["errors"]
    assert e["path"]["status"] == 400 and e["badcfg"]["status"] == 400
    assert e["nojob"]["status"] == 404 and e["badfile"]["status"] == 404
    assert e["notop"]["status"] == 422


def test_zip_and_names():
    script = r"""
      const J = require(process.argv[1]);
      const z = J.makeZip([{name: 'a/b.txt', bytes: new TextEncoder().encode('hello')}]);
      process.stdout.write(JSON.stringify({
        crc: J.crc32(new TextEncoder().encode('hello')), size: z.length,
        names: ['  a b.cdl ', '..x.', 'x/../y', '日本.cdl', ''].map(n => J.safeName(n, 'dflt')),
        rel: ['../../e.measure', 'a\\b/./c.lis', ''].map(J.safeRelpath)}));"""
    p = subprocess.run([NODE, "-e", script, str(ROOT / "src/racepath/js/jobs.js")],
                       capture_output=True, text=True, check=True)
    r = json.loads(p.stdout)
    import zlib
    from racepath.jobs import safe_name, safe_relpath
    assert r["crc"] == zlib.crc32(b"hello")
    assert r["names"] == [safe_name(n, "dflt") for n in ['  a b.cdl ', '..x.', 'x/../y', '日本.cdl', '']]
    assert r["rel"] == [safe_relpath(n) for n in ['../../e.measure', 'a\\b/./c.lis', '']]
