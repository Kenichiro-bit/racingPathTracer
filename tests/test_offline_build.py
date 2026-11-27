"""Build check for the offline single-file HTML (scripts/build_offline_html.py).

Needs the pinned Pyodide tarball in build/ (it is downloaded by the build script);
skipped otherwise. The in-browser end-to-end check is scripts/check_offline_html.js (run in CI).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_offline_html as b  # noqa: E402

TARBALL = ROOT / "build" / f"pyodide-{b.PYODIDE_VERSION}.tgz"


@pytest.mark.skipif(not TARBALL.is_file(), reason="Pyodide tarball not downloaded")
def test_build_offline_html(tmp_path):
    out = tmp_path / "racepath.html"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_offline_html.py"),
                    "--tarball", str(TARBALL), "-o", str(out)], check=True)
    html = out.read_text(encoding="utf-8")
    assert out.stat().st_size < 12_000_000
    assert "<!--LOCAL_BACKEND-->" not in html and "__TOKEN__" not in html and "__VERSION__" not in html
    assert "racepath オフライン版" in html and "Mozilla Public License" in html
    m = re.search(r'<script type="application/json" id="rp-assets">(.*?)</script>', html, re.S)
    assets = json.loads(m.group(1))
    assert set(assets["pyodide"]) == set(b.PYODIDE_FILES)
    assert set(assets["racepath"]) == set(b.RACEPATH_FILES)
    assert "def rp_call" in assets["bridge"]
    # every embedded racepath source matches the working tree
    import base64
    import gzip
    for name, data in assets["racepath"].items():
        assert gzip.decompress(base64.b64decode(data)) == (ROOT / "src" / "racepath" / name).read_bytes()


def test_bad_tarball_is_rejected(tmp_path):
    bad = tmp_path / "pyodide.tgz"
    bad.write_bytes(b"not a tarball")
    with pytest.raises(SystemExit, match="checksum"):
        b.read_pyodide(str(bad))
