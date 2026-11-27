# racepath

SRAM などのトランジスタレベルのネットリスト(CDL / SPICE / Spectre)から**レーシングパス(競合パス)**を自動で見つけ、
測るべき内部ノード(オートプローブ)を選んで `.measure` 文・PVT コーナーデッキ・マージン集計を作るツールです。
Python 標準ライブラリのみで動きます(YAML 設定を読むときだけ PyYAML を任意で使用)。

## インストール

pip で直接入れる場合(プライベートリポジトリなので GitHub の認証が必要です):

```bash
pip install "git+https://github.com/Kenichiro-bit/racingPathTracer"
```

Claude Code のスキルも使う場合は、clone してインストールスクリプトを実行します:

```bash
git clone https://github.com/Kenichiro-bit/racingPathTracer.git racepath
```

```bash
cd racepath && scripts/install_skill.sh
```

`install_skill.sh` は `pip install -e ".[yaml]"` を実行してから、`~/.claude/skills/racepath` を
このリポジトリの `skill/racepath` へのシンボリックリンクにします(既に実体のフォルダがある場合は上書きせずに止まります)。
venv に入れたい場合は `PYTHON=.venv/bin/python scripts/install_skill.sh` のように指定します。

## Claude Code での使い方

```text
/racepath path/to/macro.cdl
```

または「このネットのレーシングパス見て」と頼むだけで、スキルが次の流れで進めます。

1. `racepath analyze` で解析
2. 自動判定の誤り(MOS モデル名・トップセル・起点・電源名)を直して再実行
3. `races.json` を日本語で要約して報告
4. 望まれたら `decks` でコーナーデッキを作り、**実行前に確認したうえで**シミュレーション、`collect` で集計

## 更新方法

```bash
git pull
```

editable インストールとシンボリックリンクなので、`git pull` だけでツールとスキルの両方が更新されます。

---

## コマンド

### analyze — 競合を検出する

```bash
racepath analyze examples/sram_demo.cdl -o sram_demo_racepath
```

| オプション | 意味 |
|---|---|
| `-f {auto,cdl,spice,hspice,spectre}` | 形式(既定は自動判定:拡張子 `.scs` や `simulator lang=spectre` で判断) |
| `-t TOP` | トップセル(既定:どこからもインスタンスされない subckt のうちデバイス数最大のもの) |
| `-s NET` | 起点ネット(複数可。既定:PININFO の `I` ピン、なければ駆動されていないトップポート) |
| `-c CONFIG` | 設定ファイル(JSON、または PyYAML があれば YAML) |
| `--prefix XDUT.` | テストベンチでの DUT の階層パス(`v(...)` の前に付く) |
| `--no-group` | 同型の競合をまとめずに全件出す |
| `-q` | コンソール出力を抑える |

出力(`-o DIR`):

| ファイル | 内容 |
|---|---|
| `races.meas` | HSPICE 構文の `.measure` 文(Spectre では `simulator lang=spice` の区間で読む)。競合ごとに種別・段数・分岐点・NG 条件(日本語)のコメント付き |
| `races_params.inc` | 測定で参照するパラメータの既定値(`vdd_m t_m th_mid th_pre sa_dv_req`) |
| `races.json` | 解析のメタ情報と全競合(`explain`、`meas` 行を含む) |
| `races.csv` | UTF-8(BOM 付き)。Excel でそのまま開ける |
| `report.html` | スマホでも読めるカード表示。ノード名をタップすると測定文へ移動してハイライト |

### decks — PVT コーナーごとのデッキを作る

```bash
racepath decks sram_demo_racepath/races.json examples/corners.json -o sram_demo_racepath/decks --sim spectre
```

コーナー定義の例は `examples/corners.json`。モデルの `lib`、`vdd`、`temp`、コーナー別 `params`、
起点の `stimulus`、`static` ポート、`stimulus_file` を指定できます。`run_all.sh` も出力します。

### collect — 結果を集計する

```bash
racepath collect sram_demo_racepath/races.json sram_demo_racepath/decks -o summary.csv
```

Spectre の `.measure`、HSPICE の `.lis` / `.log` / `.mt0` を読み、指標ごとに全コーナーの値・最悪値・最悪コーナー・判定を CSV に出します。

- margin 系:0 未満で **NG**、警告閾値未満で **要注意**(時間は `--warn` 20ps、sae_bl は `--warn-v` 0.02V)
- skew 系:コーナー間で符号が変われば **順序反転**、絶対値が `--skew-warn` 10ps 未満なら **要確認**
- 欠測があれば **測定失敗あり**

## 検出する競合の種別

| 種別 | 内容 | プローブ |
|---|---|---|
| `sae_bl` | SAE vs BL 振幅(WL が開いて BL 差が広がる前に SAE が立つと誤読出し) | sae, wl, sa_t, sa_c, bl_t, bl_c |
| `hold` | ラッチのホールド(ラッチが閉じる前に次のデータが届く) | clk, data_in, data_node |
| `wl_pre` | WL vs プリチャージ(プリチャージが切れる前に WL が開く/終了側も同様) | pre, wl |
| `clock_pair` | クロック対の重なり(両方開く/両方閉じる瞬間) | a, b |
| `generic` | 汎用(到達順の入れ替わりでグリッチ。`derived` はパルス幅) | a, b |

詳しい判定条件・出力形式は [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) を参照してください。

## 設定ファイル(`racepath.json`)

必要なキーだけ書けば、残りは既定値が使われます。

```json
{"supplies": ["VDD*", "VSS*", "VCS*"], "sources": ["CLK"]}
```

| キー | 既定値 | 意味 |
|---|---|---|
| `supplies` | `VDD* VSS* GND* VCC* VPWR* VGND* VPP* VBB* AVDD* AVSS* DVDD* DVSS* 0` | 電源とみなすネット名のグロブ |
| `high_supplies` | `VDD* VCC* VPWR* VPP* AVDD* DVDD*` | デッキで `vdd_m` を与える電源 |
| `pmos_models` / `nmos_models` | `^p\|pch\|pmos\|pfet` / `^n\|nch\|nmos\|nfet` | MOS 判定の正規表現 |
| `sources` | `[]` | 起点ネット |
| `probe_prefix` | `XDUT.` | DUT の階層パス |
| `merge_resistors` | `true` | 抵抗をショートとみなす(寄生抽出後のネット対策) |
| `ignore_nets` | `[]` | この名前に触れる競合を除外 |
| `extra_checks` | `[]` | 手動の測定 `{"class", "a", "b", ...}` |
| `keep_bus_pairs` | `false` | `WL<0>` と `WL<1>` などバスの兄弟同士も組にする |
| `vdd`, `t_m`, `th_mid`, `th_pre`, `sa_dv_req` | `0.8`, `1n`, `0.5`, `0.9`, `0.05` | 測定パラメータの既定値 |

## 開発

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,yaml]"
```

```bash
.venv/bin/pytest -q
```

リポジトリ構成:

```text
src/racepath/        CLI 本体(cli.py)
tests/               受け入れテスト、合成アレイ生成(gen_array.py)、誤分類の再現ケース(cases/)
examples/            デモネットリスト(CDL / Spectre)とコーナー定義
skill/racepath/      Claude Code スキル
scripts/             install_skill.sh
docs/BUILD_SPEC.md   仕様書
```

変更のルールは [CLAUDE.md](CLAUDE.md)、変更履歴は [CHANGELOG.md](CHANGELOG.md) にあります。誤分類は Issue の「誤分類の報告」テンプレートで記録してください。

## ライセンス

MIT
