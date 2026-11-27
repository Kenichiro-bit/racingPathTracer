# CLAUDE.md — racepath リポジトリで守るルール

racepath はトランジスタレベルのネットリスト(CDL/Spectre)からレーシングパスを検出し、
オートプローブの `.measure` 文・コーナーデッキ・マージン集計を作る CLI。本体は `src/racepath/cli.py`。
仕様は `docs/BUILD_SPEC.md`。Claude Code スキルは `skill/racepath/SKILL.md`。

## 必ず守ること

- **変更したら必ず `pytest` を回す。** 受け入れテストの6件
  (sae_bl: SAE vs WL0 / wl_pre: PRE_N vs WL0 / hold×2: CKBI・CKI vs SAT /
  clock_pair: CKBI vs CKI / generic: PRE_N vs SAE)が維持されていることを確認する。
  ```bash
  .venv/bin/pytest -q
  ```
- **分類ルールや出力形式を変えたら、`docs/BUILD_SPEC.md` と `README.md` も同じコミットで更新する。**
  スキルの手順に影響するなら `skill/racepath/SKILL.md` も更新する。
- **言語**:レポート(`report.html`、`races.meas` のコメント、CSV)・NG条件の説明・コンソールに出す要約は日本語。
  コードとコメントは英語でよい。
- **`report.html` を変えたら**、ブラウザ(使えれば Playwright)で開いて確認する。
  - ノード名をタップすると該当の測定文ブロックへスクロールし、そのノードの `v(...)` がハイライトされること
  - 測定対象でない中間ノードでは、到達時刻を測る `.measure` 1行とコピーボタンが出ること
  - スマホ幅(390px 程度)で横スクロールが出ず、カードが読めること
- **ブラウザ画面(`src/racepath/web.py`)を変えたら**、`racepath serve --no-browser` で起動して Playwright で操作し、
  解析 → レポート内のタップ → デッキ作成 → 集計 → 過去の解析の再表示が通ること、
  PC幅とスマホ幅(ダークモードも)で横スクロールが出ないことを確認する。
  画面の処理は既存のサブコマンドを呼ぶだけにして、検出ロジックを web.py に書かない。
  `INDEX_HTML` はオフライン版(`dist/racepath.html`)と共通なので、オフライン版も組み立てて確かめる:
  ```bash
  .venv/bin/python scripts/build_offline_html.py && node scripts/check_offline_html.js dist/racepath.html
  ```
- **オフライン版はネットワークに一切アクセスしない。** 外部の CDN やフォントを足さない。Pyodide の版を上げるときは
  `scripts/build_offline_html.py` の版とチェックサムを両方更新する。
- **依存ライブラリは増やさない。** 標準ライブラリのみ(オフライン版に埋め込む Pyodide は実行時の依存ではない)。PyYAML は任意(`[yaml]` extra、YAML 設定を読むときだけ import)。
  対応 Python は 3.9 以上。
- **誤分類の報告を受けたら、テストを先に書いてから直す。**
  1. 再現する最小ネットリストを `tests/cases/` に追加する
  2. 期待する分類・プローブを確かめるテストを `tests/` に書き、落ちることを確認する
  3. 実装を直し、新しいテストと受け入れテストの両方が通ることを確認する
- **バージョンは SemVer で上げる。** `src/racepath/__init__.py` の `__version__` を更新し、
  `CHANGELOG.md` に日本語で記録する。検出結果や出力形式が変わる変更は、少なくともマイナーバージョンを上げる。

## 開発環境

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,yaml]"
.venv/bin/racepath analyze examples/sram_demo.cdl -o /tmp/rp
```

- `examples/output/` や `*_racepath/` などの生成物はコミットしない(`.gitignore` 済み)。
- `skill/racepath/templates/sram_demo.cdl` と `examples/sram_demo.cdl` は同じ内容に保つ(テストで確認している)。
