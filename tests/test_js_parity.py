"""Python <-> JavaScript parity: the browser engine (src/racepath/js/racepath.js) must produce
byte-identical outputs to the Python CLI for analyze, decks and collect.

Runs `python -m racepath ...` and `node tests/js/run_engine.js ...` with the same arguments in
two sibling folders and compares every output file and the console text. Skipped without node.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
RUNNER = ROOT / "tests" / "js" / "run_engine.js"
NODE = shutil.which("node")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_array  # noqa: E402
import gen_random  # noqa: E402

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")
OUTPUTS = ["races.json", "races.meas", "races_params.inc", "races.csv", "report.html"]


def run_py(cwd, *args):
    return subprocess.run([sys.executable, "-m", "racepath", *map(str, args)], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8")


def run_js(cwd, *args):
    return subprocess.run([NODE, str(RUNNER), *map(str, args)], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8")


def both(tmp, *args):
    py, js = tmp / "py", tmp / "js"
    py.mkdir(exist_ok=True)
    js.mkdir(exist_ok=True)
    return py, js, run_py(py, *args), run_js(js, *args)


def assert_same_dir(py, js, names, fix=lambda t: t):
    for n in names:
        a = fix((py / n).read_bytes().decode("utf-8"))
        b = (js / n).read_bytes().decode("utf-8")
        assert a == b, f"{n} differs:\n" + _first_diff(a, b)


def _first_diff(a, b):
    for i, (x, y) in enumerate(zip(a.splitlines(), b.splitlines())):
        if x != y:
            return f"line {i + 1}\n  py: {x!r}\n  js: {y!r}"
    return f"length py={len(a)} js={len(b)}"


def analyze_parity(tmp, netlist, *extra):
    py, js, rp, rj = both(tmp, "analyze", netlist, "-o", "out", *extra)
    assert rp.returncode == rj.returncode, (rp.stdout + rp.stderr, rj.stdout + rj.stderr)
    assert rp.stdout == rj.stdout
    if rp.returncode != 0:
        assert rp.stderr.strip().splitlines()[-1] == rj.stderr.strip().splitlines()[-1]
        return None
    assert_same_dir(py / "out", js / "out", OUTPUTS)
    return json.loads((py / "out" / "races.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- analyze

@pytest.mark.parametrize("netlist,extra", [
    ("sram_demo.cdl", []),
    ("sram_demo.scs", []),
    ("sram_demo.cdl", ["--no-group", "--show", "3"]),
    ("sram_demo.cdl", ["-s", "CLK", "-t", "SRAM_DEMO", "--prefix", "TB.XDUT."]),
    ("sram_demo.cdl", ["-s", "nope"]),
    ("sram_demo.cdl", ["-t", "NO_SUCH"]),
    ("sram_demo.scs", ["-f", "cdl"]),
    ("sram_demo.cdl", ["-q"]),
])
def test_demo(tmp_path, netlist, extra):
    analyze_parity(tmp_path, EXAMPLES / netlist, *extra)


CASES = ROOT / "tests" / "cases"


@pytest.mark.parametrize("case,extra", [
    ("testmode.cdl", []),
    ("testmode.cdl", ["--static", "TM"]),
    ("testmode.cdl", ["--static", "t*", "--static", "nothing"]),
    ("testmode.cdl", ["-s", "CLK", "-s", "TM", "--static", "TM"]),
    ("testmode.cdl", ["--static", "SCAN_EN"]),
])
def test_cases(tmp_path, case, extra):
    """Every reproduction case in tests/cases/ must also give identical results in both engines."""
    analyze_parity(tmp_path, CASES / case, *extra)


def test_all_cases_are_covered():
    covered = {c for c, _ in test_cases.pytestmark[0].args[1]} if hasattr(test_cases, "pytestmark") else set()
    assert {p.name for p in CASES.glob("*.cdl")} | {p.name for p in CASES.glob("*.scs")} <= covered


def test_config_and_extra_checks(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "supplies": ["VDD*", "VSS*"], "vdd": 0.75, "static_inputs": ["Q*"], "sa_dv_req": 0.07, "t_m": "2n",
        "ignore_nets": ["PRE*"], "max_races": 3,
        "extra_checks": [{"class": "hold", "name": "manual1", "clk": "CKI", "data_in": "SAT"},
                         {"a": "WL0", "b": "SAE"}, {"a": "MISSING", "b": "SAE"}],
    }))
    data = analyze_parity(tmp_path, EXAMPLES / "sram_demo.cdl", "-c", cfg)
    assert any(r.get("manual") for r in data["races"])


@pytest.mark.parametrize("rows,cols,extra", [(32, 8, []), (32, 8, ["--no-group"]), (16, 4, ["--show", "100"])])
def test_array(tmp_path, rows, cols, extra):
    net = tmp_path / "arr.cdl"
    gen_array.main([str(rows), str(cols), str(net)])
    analyze_parity(tmp_path, net, *extra)


@pytest.mark.parametrize("seed", range(40))
def test_random_netlists(tmp_path, seed):
    net = tmp_path / ("rand.scs" if seed % 4 == 3 else "rand.cdl")
    gen_random.main([str(seed), str(net)])
    analyze_parity(tmp_path, net, "--no-group" if seed % 2 else "--show", *([] if seed % 2 else ["200"]))


# ---------------------------------------------------------------- decks

CORNERS_EXTRA = {
    "simulator": "hspice", "tran_stop": "30n", "t_m": "1.5n",
    "stimulus": {"CLK": {"delay": "2n", "period": "8n", "width": "4n", "tr": "25p", "tf": "35p"}},
    "static": {"Q": 0.4}, "supplies": {"VSS": 0}, "tnom": 27,
    "corners": [{"name": "c1", "lib": "/m/models.lib", "vdd": 0.9, "temp": 0},
                {"name": "c2", "vdd": 0.7, "params": {"sa_dv_req": 0.08, "t_m": "3n"}}],
}


@pytest.mark.parametrize("netlist,corners,sim", [
    ("sram_demo.cdl", None, "spectre"), ("sram_demo.cdl", None, "hspice"),
    ("sram_demo.scs", None, "spectre"), ("sram_demo.cdl", CORNERS_EXTRA, None),
    ("sram_demo.cdl", dict(CORNERS_EXTRA, stimulus_file="/tb/stim.sp", simulator="spectre"), None),
])
def test_decks(tmp_path, netlist, corners, sim):
    analyze_parity(tmp_path, EXAMPLES / netlist)
    cpath = tmp_path / "corners.json"
    cpath.write_text(json.dumps(corners) if corners else (EXAMPLES / "corners.json").read_text())
    args = ["decks", "out/races.json", cpath, "-o", "decks"] + (["--sim", sim] if sim else [])
    py, js, rp, rj = both(tmp_path, *args)
    assert rp.returncode == 0 and rj.returncode == 0, (rp.stderr, rj.stderr)
    assert rp.stdout == rj.stdout
    names = sorted(os.listdir(py / "decks"))
    assert names == sorted(os.listdir(js / "decks"))
    assert_same_dir(py / "decks", js / "decks", names, fix=lambda t: t.replace(str(py), str(js)))
    assert os.access(js / "decks" / "run_all.sh", os.X_OK)


# ---------------------------------------------------------------- collect

MT0 = ("$DATA1 SOURCE='HSPICE'\n.TITLE 'deck'\n"
       "rp001_margin rp002_margin rp002_margin_end rp003_margin rp004_margin rp005_skew rp006_skew temper alter#\n"
       "1.000e-02 -1.0e-11 3.0e-11 1.0e-11 failed -5.0e-12 9.0e-11 -40.0 1\n")
MEASURE = ("rp001_margin = 120m\nrp002_margin = 50p\nrp002_margin_end = 40p\n"
           "rp003_margin = 3e-05\nRP004_MARGIN = 2.5e-11\nrp005_skew = 5p\nrp006_skew = 100p\n")


@pytest.mark.parametrize("extra", [[], ["--warn", "1e-9", "--warn-v", "0.5", "--skew-warn", "2e-10"]])
def test_collect(tmp_path, extra):
    analyze_parity(tmp_path, EXAMPLES / "sram_demo.cdl")
    for side in ("py", "js"):
        res = tmp_path / side / "res"
        (res / "tt_0p80v_25c.raw").mkdir(parents=True)
        (res / "tt_0p80v_25c.raw" / "tran1.measure").write_text(MEASURE)
        (res / "sub").mkdir()
        (res / "sub" / "ss_0p72v_m40c.mt0").write_text(MT0)
        (res / "ff.log").write_text("rp001_margin = 1\nrp006_skew = -1e-13\n")
        (res / ".hidden.measure").write_text("rp001_margin = -5\n")
        (res / "notes.txt").write_text("rp001_margin = -5\n")
        (res / "empty.lis").write_text("nothing here\n")
    py, js, rp, rj = both(tmp_path, "collect", "out/races.json", "res", "-o", "summary.csv", *extra)
    assert rp.returncode == 0 and rj.returncode == 0, (rp.stderr, rj.stderr)
    assert rp.stdout == rj.stdout
    assert_same_dir(py, js, ["summary.csv"])


def test_collect_no_results(tmp_path):
    analyze_parity(tmp_path, EXAMPLES / "sram_demo.cdl")
    (tmp_path / "py" / "res").mkdir()
    (tmp_path / "js" / "res").mkdir()
    py, js, rp, rj = both(tmp_path, "collect", "out/races.json", "res")
    assert rp.returncode != 0 and rj.returncode != 0
    assert "no measurement results found" in rp.stderr and "no measurement results found" in rj.stderr


# ---------------------------------------------------------------- helpers

def test_version_matches():
    import racepath
    src = (ROOT / "src" / "racepath" / "js" / "racepath.js").read_text(encoding="utf-8")
    assert f'var VERSION = "{racepath.__version__}";' in src


def test_float_repr_matches_python():
    import random
    rnd = random.Random(1)
    vals = [0.0, -0.0, 1.0, 0.5, 1e-5, 1.2e-10, 123456.0, 1e16, 1.5e16, 1e-4, 9.999e-5, 0.1 + 0.2, -2.5e-12,
            12345678901234567.0, 5e-324, 1.7976931348623157e308]
    vals += [rnd.uniform(-1, 1) * 10 ** rnd.randint(-15, 18) for _ in range(300)]
    script = ("const E=require(%r);const v=JSON.parse(require('fs').readFileSync(0,'utf8'));"
              "process.stdout.write(JSON.stringify(v.map(E._pyFloat)))") % str(ROOT / "src/racepath/js/racepath.js")
    p = subprocess.run([NODE, "-e", script], input=json.dumps(vals), capture_output=True, text=True, check=True)
    assert json.loads(p.stdout) == [repr(float(x)) for x in vals]
