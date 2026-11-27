"""static_inputs: pins held constant in normal operation (test mode, scan enable, ...)
are not used as launch nets, so races that only exist through them are not reported."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASE = ROOT / "tests" / "cases" / "testmode.cdl"
CLK_RACES = {
    ("sae_bl", "SAE", "WL0"), ("wl_pre", "PRE_N", "WL0"), ("hold", "CKBI", "SAT"),
    ("hold", "CKI", "SAT"), ("clock_pair", "CKBI", "CKI"), ("generic", "PRE_N", "SAE"),
}


def analyze(tmp_path, *args):
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, "-m", "racepath", "analyze", str(CASE), "-o", str(out), *map(str, args)],
                       capture_output=True, text=True, check=True)
    data = json.loads((out / "races.json").read_text(encoding="utf-8"))
    return data, p.stdout, out


def race_set(data):
    return {(r["class"], r["a"], r["b"]) for r in data["races"]}


def test_without_setting_tm_only_race_is_reported(tmp_path):
    data, log, _ = analyze(tmp_path)
    assert data["meta"]["sources"] == ["CLK", "TM"]
    assert ("generic", "TMB", "TMBB") in race_set(data)
    assert data["meta"]["static_inputs"] == []


def test_static_inputs_in_config(tmp_path):
    cfg = tmp_path / "racepath.json"
    cfg.write_text(json.dumps({"static_inputs": ["tm"]}))   # case-insensitive glob
    data, log, out = analyze(tmp_path, "-c", cfg)
    assert data["meta"]["sources"] == ["CLK"]
    assert data["meta"]["static_inputs"] == ["TM"]
    assert race_set(data) == CLK_RACES          # the TM-gated word line race is still found
    assert "static inputs: TM" in log
    assert "固定端子 TM" in (out / "report.html").read_text(encoding="utf-8")


def test_static_option_and_glob(tmp_path):
    data, log, _ = analyze(tmp_path, "--static", "T*")
    assert data["meta"]["sources"] == ["CLK"] and data["meta"]["static_inputs"] == ["TM"]
    assert race_set(data) == CLK_RACES


def test_explicit_source_that_is_static_is_dropped_with_warning(tmp_path):
    data, log, _ = analyze(tmp_path, "-s", "CLK", "-s", "TM", "--static", "TM")
    assert data["meta"]["sources"] == ["CLK"]
    assert "warning: source 'TM' is a static input (static_inputs); ignored" in log
    assert race_set(data) == CLK_RACES


def test_pattern_that_matches_nothing(tmp_path):
    data, log, _ = analyze(tmp_path, "--static", "SCAN_EN")
    assert data["meta"]["static_inputs"] == []
    assert "static inputs: (none matched SCAN_EN)" in log
    assert ("generic", "TMB", "TMBB") in race_set(data)


def test_demo_unchanged_without_setting(tmp_path):
    out = tmp_path / "demo"
    subprocess.run([sys.executable, "-m", "racepath", "analyze", str(ROOT / "examples" / "sram_demo.cdl"),
                    "-o", str(out)], check=True, capture_output=True)
    data = json.loads((out / "races.json").read_text(encoding="utf-8"))
    assert race_set(data) == CLK_RACES
    assert "固定端子" not in (out / "report.html").read_text(encoding="utf-8")
