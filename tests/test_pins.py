"""Output per input pin (v0.6.0): races are listed under the input pins (launch nets) they start
from, without race class names; the measures are unchanged."""
import csv
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
CASES = ROOT / "tests" / "cases"
CLASSES = ("sae_bl", "wl_pre", "hold", "clock_pair", "generic")


def analyze(tmp_path, netlist, *args):
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, "-m", "racepath", "analyze", str(netlist), "-o", str(out), *map(str, args)],
                       capture_output=True, text=True, check=True)
    return json.loads((out / "races.json").read_text(encoding="utf-8")), p.stdout, out


def test_demo_is_listed_under_its_input_pin(tmp_path):
    data, log, out = analyze(tmp_path, EXAMPLES / "sram_demo.cdl")
    ids = [r["id"] for r in data["races"]]
    assert data["pins"] == [{"pin": "CLK", "races": ids}]
    assert all(r["pins"] == ["CLK"] for r in data["races"])
    # console: one block per pin, no class names
    assert "  ■ 入力端子 CLK  競合 6件" in log
    assert "races: 6  (入力端子 1本)" in log
    lines = [ln for ln in log.splitlines() if ln.startswith("   rp")]
    assert len(lines) == 6 and not any(c in ln for ln in lines for c in CLASSES)
    # ordered by stage difference (tightest first), not by class
    diffs = [r["stage_diff"] for r in data["races"]]
    assert diffs == sorted(diffs)
    # measures are unchanged (still class-specific), but the header names the pin, not the class
    meas = (out / "races.meas").read_text(encoding="utf-8")
    sae = next(r for r in data["races"] if r["class"] == "sae_bl")
    assert f"{sae['id']}_tsae" in meas and f"{sae['id']}_margin param=" in meas
    assert f"* [{sae['id']}] 入力端子 CLK : CCC#0" in meas
    assert not any(f"] {c} :" in meas for c in CLASSES)
    # csv and report without classes
    rows = list(csv.reader(io.StringIO((out / "races.csv").read_text(encoding="utf-8-sig"))))
    assert rows[0][:3] == ["id", "pins", "ccc"] and "class" not in rows[0]
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "入力端子 CLK" in html and 'id="p-0"' in html
    assert "SAE vs BL振幅" not in html and "t-sae_bl" not in html


def test_grouped_races_are_listed_under_every_pin(tmp_path):
    data, log, out = analyze(tmp_path, CASES / "twobits.cdl")
    assert [p["pin"] for p in data["pins"]] == ["D<0>", "D<1>", "EN"]
    assert len(data["races"]) == 1
    r = data["races"][0]
    assert r["group_size"] == 2 and r["pins"] == ["D<0>", "D<1>"]
    by_pin = {p["pin"]: p["races"] for p in data["pins"]}
    assert by_pin == {"D<0>": [r["id"]], "D<1>": [r["id"]], "EN": []}
    assert "  ■ 入力端子 D<1>  競合 1件" in log and "  ■ 入力端子 EN  競合 0件" in log
    html = (out / "report.html").read_text(encoding="utf-8")
    assert html.count(f'id="r-{r["id"]}"') == 1          # one card, linked from the other pin
    assert f'href="#r-{r["id"]}"' in html
    data, log, out = analyze(tmp_path, CASES / "twobits.cdl", "--no-group")
    assert [p["races"] for p in data["pins"]][:2] == [["rp001"], ["rp002"]] or \
        sorted(sum([p["races"] for p in data["pins"]], [])) == ["rp001", "rp002"]


def test_manual_checks_have_their_own_section(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"extra_checks": [{"name": "m1", "a": "WL0", "b": "SAE"}]}))
    data, log, out = analyze(tmp_path, EXAMPLES / "sram_demo.cdl", "-c", cfg)
    man = [r for r in data["races"] if r.get("manual")]
    assert man and man[0]["pins"] == []
    assert "  ■ 手動の測定  1件" in log
    assert "手動の測定" in (out / "report.html").read_text(encoding="utf-8")


def test_collect_summary_has_no_class_column(tmp_path):
    data, log, out = analyze(tmp_path, EXAMPLES / "sram_demo.cdl")
    res = tmp_path / "res"
    res.mkdir()
    (res / "tt.measure").write_text("rp001_skew = 1p\n")
    summ = tmp_path / "s.csv"
    p = subprocess.run([sys.executable, "-m", "racepath", "collect", str(out / "races.json"), str(res), "-o", str(summ)],
                       capture_output=True, text=True, check=True)
    hdr = next(csv.reader(io.StringIO(summ.read_text(encoding="utf-8-sig"))))
    assert hdr[:4] == ["id", "metric", "input_a", "input_b"] and "class" not in hdr
    assert not any(c in p.stdout for c in CLASSES)
