"""Acceptance tests for racepath (see docs/BUILD_SPEC.md section 7)."""
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_array  # noqa: E402

EXPECTED = {
    ("sae_bl", "SAE", "WL0"),
    ("wl_pre", "PRE_N", "WL0"),
    ("hold", "CKBI", "SAT"),
    ("hold", "CKI", "SAT"),
    ("clock_pair", "CKBI", "CKI"),
    ("generic", "PRE_N", "SAE"),
}


def run(*args, check=True):
    p = subprocess.run([sys.executable, "-m", "racepath", *map(str, args)],
                       capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode != 0:
        raise AssertionError(f"racepath {args} failed\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
    return p


def analyze(netlist, out, *extra):
    run("analyze", netlist, "-o", out, *extra)
    with open(Path(out) / "races.json", encoding="utf-8") as f:
        return json.load(f)


def race_set(data):
    return {(r["class"], r["a"], r["b"]) for r in data["races"]}


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo")
    return out, analyze(EXAMPLES / "sram_demo.cdl", out)


# ---------------------------------------------------------------- analyze

def test_version():
    import racepath
    assert run("--version").stdout.strip() == racepath.__version__


def test_demo_cdl_six_races(demo):
    _, data = demo
    assert len(data["races"]) == 6
    assert race_set(data) == EXPECTED
    assert data["meta"]["top"] == "SRAM_DEMO"
    assert data["meta"]["sources"] == ["CLK"]


def test_demo_spectre_same_races(tmp_path):
    data = analyze(EXAMPLES / "sram_demo.scs", tmp_path)
    assert data["meta"]["format"] == "spectre"
    assert len(data["races"]) == 6
    assert race_set(data) == EXPECTED


def test_skill_template_matches_example():
    tpl = ROOT / "skill" / "racepath" / "templates" / "sram_demo.cdl"
    assert tpl.read_text() == (EXAMPLES / "sram_demo.cdl").read_text()


def test_sae_bl_details(demo):
    _, data = demo
    r = next(r for r in data["races"] if r["class"] == "sae_bl")
    assert r["divergence"] == "CLKBUF"
    assert r["stages_a"] == 6 and r["stages_b"] == 2
    p = r["probes"]
    assert (p["sae"], p["wl"]) == ("SAE", "WL0")
    assert {p["sa_t"], p["sa_c"]} == {"SAT", "SAC"}
    assert {p["bl_t"], p["bl_c"]} == {"BL", "BLB"}
    assert any("rp001_dvbl" in m for m in r["meas"])


def test_hold_data_node(demo):
    _, data = demo
    holds = [r for r in data["races"] if r["class"] == "hold"]
    assert len(holds) == 2
    for r in holds:
        assert r["probes"]["data_node"] == "XLAT.DL"
        assert r["has_feedback"]


def test_outputs_written(demo):
    out, data = demo
    for name in ("races.json", "races.csv", "races.meas", "races_params.inc", "report.html"):
        assert (out / name).is_file(), name
    meas = (out / "races.meas").read_text(encoding="utf-8")
    assert "v(XDUT.SAE)" in meas and "NG条件" in meas
    params = (out / "races_params.inc").read_text(encoding="utf-8")
    for p in ("vdd_m", "t_m", "th_mid", "th_pre", "sa_dv_req"):
        assert p in params
    raw = (out / "races.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "viewport" in html and "rp001" in html
    for r in data["races"]:
        assert r["explain"]


def test_array_groups(tmp_path):
    net = tmp_path / "arr.cdl"
    gen_array.main(["32", "8", str(net)])
    text = net.read_text()
    assert " / " in text and "$PINS" in text and "\nRWL" in text
    data = analyze(net, tmp_path / "out")
    assert len(data["races"]) == 6
    assert all(r["group_size"] == 8 for r in data["races"])
    assert {r["class"] for r in data["races"]} == {"sae_bl", "wl_pre", "hold", "clock_pair", "generic"}
    data = analyze(net, tmp_path / "out_ng", "--no-group")
    assert len(data["races"]) == 48


def test_large_array_speed(tmp_path):
    net = tmp_path / "big.cdl"
    gen_array.main(["256", "64", str(net)])
    t0 = time.time()
    data = analyze(net, tmp_path / "out", "-q")
    dt = time.time() - t0
    assert data["meta"]["n_mos"] > 100000
    assert len(data["races"]) == 6
    assert dt < 30, f"took {dt:.1f}s"


# ---------------------------------------------------------------- decks

@pytest.mark.parametrize("sim,ext", [("spectre", ".scs"), ("hspice", ".sp")])
def test_decks(demo, tmp_path, sim, ext):
    out, _ = demo
    run("decks", out / "races.json", EXAMPLES / "corners.json", "-o", tmp_path, "--sim", sim)
    decks = sorted(p.name for p in tmp_path.glob("*" + ext))
    corners = json.loads((EXAMPLES / "corners.json").read_text())["corners"]
    assert decks == sorted(c["name"] + ext for c in corners)
    assert os.access(tmp_path / "run_all.sh", os.X_OK)
    deck = (tmp_path / ("tt_0p80v_25c" + ext)).read_text()
    assert "races.meas" in deck and "SRAM_DEMO" in deck
    if sim == "spectre":
        assert "simulator lang=spice" in deck and "type=pulse" in deck
    else:
        assert ".temp 25" in deck and "pulse(" in deck


# ---------------------------------------------------------------- collect

def _read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_collect(demo, tmp_path):
    out, _ = demo
    res = tmp_path / "res"
    # Spectre-style key = value results, corner name taken from parent dir
    (res / "tt_0p80v_25c.raw").mkdir(parents=True)
    (res / "tt_0p80v_25c.raw" / "tran1.measure").write_text(
        "rp001_margin = 120m\nrp002_margin = 50p\nrp002_margin_end = 40p\n"
        "rp003_margin = 30p\nrp004_margin = 25p\nrp005_skew = 5p\nrp006_skew = 100p\n")
    # HSPICE .mt0 tabular results, with a failed measurement
    names = "rp001_margin rp002_margin rp002_margin_end rp003_margin rp004_margin rp005_skew rp006_skew temper alter#"
    (res / "ss_0p72v_m40c.mt0").write_text(
        "$DATA1 SOURCE='HSPICE'\n.TITLE 'deck'\n" + names + "\n"
        "1.000e-02 -1.0e-11 3.0e-11 1.0e-11 failed -5.0e-12 9.0e-11 -40.0 1\n")
    summary = tmp_path / "summary.csv"
    run("collect", out / "races.json", res, "-o", summary)
    rows = {(r["id"], r["metric"]): r for r in _read_csv(summary)}
    assert rows[("rp001", "margin")]["verdict"] == "要注意"       # 10mV < warn-v 20mV
    assert rows[("rp001", "margin")]["worst_corner"] == "ss_0p72v_m40c"
    assert rows[("rp002", "margin")]["verdict"] == "NG"
    assert rows[("rp002", "margin_end")]["verdict"] == "OK"
    assert rows[("rp003", "margin")]["verdict"] == "要注意"       # 10ps < 20ps
    assert "測定失敗あり" in rows[("rp004", "margin")]["verdict"]
    assert rows[("rp005", "skew")]["verdict"] == "順序反転"
    assert rows[("rp006", "skew")]["verdict"] == ""
    assert summary.read_bytes().startswith(b"\xef\xbb\xbf")
