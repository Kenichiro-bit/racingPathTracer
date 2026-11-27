"""Checks for the single-file build (racepath.html, scripts/build_offline_html.py).

The in-browser end-to-end check is scripts/check_offline_html.js (Playwright, run in CI).
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build_offline_html.py"


def test_committed_racepath_html_is_up_to_date():
    # racepath.html at the repo root is what users double-click; it must match the sources
    p = subprocess.run([sys.executable, str(BUILD), "--check"], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr + "\n-> run: python scripts/build_offline_html.py"


def test_build_is_self_contained(tmp_path):
    out = tmp_path / "racepath.html"
    subprocess.run([sys.executable, str(BUILD), "-o", str(out)], check=True)
    html = out.read_text(encoding="utf-8")
    assert out.stat().st_size < 500_000
    assert html == (ROOT / "racepath.html").read_text(encoding="utf-8")   # deterministic
    for marker in ("<!--LOCAL_BACKEND-->", "__TOKEN__", "__VERSION__"):
        assert marker not in html
    # no external resources: everything is inline
    assert not re.search(r'<(script|link|img|iframe)[^>]+(src|href)="(https?:)?//', html, re.I)
    # exactly the page's own three inline scripts are closed
    assert html.count("</script>") == 3
    assert "RacepathJobs.install(window)" in html and "racepathLocal" in html
