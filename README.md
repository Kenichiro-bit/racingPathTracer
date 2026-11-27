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

## いちばん簡単な使い方:`racepath.html` を開くだけ

リポジトリ直下の **`racepath.html`** をダブルクリックで開き、ネットリストを選んで(ドロップして)「解析する」を押すだけです。
**インストール・サーバー・インターネット接続はいりません。** ファイルは約110KB で、すぐに開きます。
clone していなければ、[Releases](https://github.com/Kenichiro-bit/racingPathTracer/releases) から `racepath.html` だけをダウンロードしても使えます。

- 解析はすべてブラウザの中(JavaScript)で行い、ネットリストを外部に送信しません。
- 結果は Python のコマンド版と同じです(同じ出力になることをテストで毎回確かめています)。約10万Tr のネットリストも1秒程度で解析します。
- 画面とできることは `racepath serve` と同じです(解析・レポート・ダウンロード・デッキ作成・集計)。PC 上のパスは指定できないので、ファイルを選んでください。
- デッキの zip にはネットリストと `races.meas` が入っていて、展開したフォルダで `run_all.sh` を実行すればそのまま動きます。
- 新しい Chrome / Edge / Firefox / Safari で動きます。結果はページを開いている間だけ保持されます(必要なファイルはダウンロードしてください)。
- 設定は JSON のみ対応です(YAML はコマンド版で使えます)。

`racepath.html` は `src/racepath/js/` と画面のソースから `python scripts/build_offline_html.py` で組み立てたものです。直接編集しないでください。

## ブラウザで使う(`racepath serve`)

```bash
racepath serve
```

ブラウザが開き、`http://127.0.0.1:8765/` に次の画面が出ます(このPCからしか接続できません)。

1. **解析**:ネットリストをドロップするか、PC上のパスを入力して「解析する」。トップセル・起点・形式・プローブの接頭辞・設定(JSON)・`--no-group` も指定できます。結果の要約、警告、`report.html`(ノードのタップで測定文へ移動)と、出力ファイルのダウンロードリンクが出ます。
2. **コーナーデッキ**:コーナー定義(JSON)を書き換えて「デッキを作る」。zip でダウンロードし、`run_all.sh` は手元で実行します(画面からシミュレーションは実行しません)。
   別のPCで実行するなら「ネットリストと races.meas を zip に入れる」にチェックを入れます。
3. **集計**:シミュレーション結果のフォルダを選ぶ(測定結果のファイルだけが送られます)か、パスを入力して「集計する」。判定が先頭に来る表と `summary.csv` が出ます。

過去の解析は「過去の解析」から開き直せます。データは作業フォルダ `racepath_web/` に残ります。

| オプション | 意味 |
|---|---|
| `--port 8765` | ポート番号 |
| `--workdir racepath_web` | アップロードしたネットリストと結果を置くフォルダ |
| `--no-browser` | ブラウザを自動で開かない |
| `--host 127.0.0.1` | 待ち受けるアドレス。ほかの端末から使う場合だけ変える(社外秘のネットリストを扱うときは変えない) |

大きなネットリスト(数百MB)はアップロードより「パスを指定」のほうが速く済みます。

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

### serve — ブラウザ画面を開く

上の「ブラウザで使う」を参照してください。

### analyze — 競合を検出する

```bash
racepath analyze examples/sram_demo.cdl -o sram_demo_racepath
```

| オプション | 意味 |
|---|---|
| `-f {auto,cdl,spice,hspice,spectre}` | 形式(既定は自動判定:拡張子 `.scs` や `simulator lang=spectre` で判断) |
| `-t TOP` | トップセル(既定:どこからもインスタンスされない subckt のうちデバイス数最大のもの) |
| `-s NET` | 起点ネット(複数可。既定:PININFO の `I` ピン、なければ駆動されていないトップポート) |
| `--static PIN` | 固定端子(テストモードなど。グロブ可、複数可)。起点にしないので、固定端子だけから生まれる偽の競合が出なくなる |
| `-c CONFIG` | 設定ファイル(JSON、または PyYAML があれば YAML) |
| `--prefix XDUT.` | テストベンチでの DUT の階層パス(`v(...)` の前に付く) |
| `--no-group` | 同型の競合をまとめずに全件出す |
| `-q` | コンソール出力を抑える |

出力(`-o DIR`):

| ファイル | 内容 |
|---|---|
| `races.meas` | HSPICE 構文の `.measure` 文(Spectre では `simulator lang=spice` の区間で読む)。競合ごとに入力端子・合流ブロック・分岐点からの段数・測定の意味(日本語)のコメント付き |
| `races_params.inc` | 測定で参照するパラメータの既定値(`vdd_m t_m th_mid th_pre sa_dv_req`) |
| `races.json` | 解析のメタ情報、入力端子ごとの競合の一覧(`pins`)、全競合(`meas` 行を含む) |
| `races.csv` | UTF-8(BOM 付き)。Excel でそのまま開ける。`pins` 列にその競合の入力端子 |
| `report.html` | 入力端子ごとの節に競合のカードを並べた表示(スマホ可)。ノード名をタップすると測定文へ移動してハイライト |

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

## 結果の見方:入力端子ごと

競合は**入力端子(起点)ごと**にまとめて出します。ある入力端子から出た信号が途中で2本に分かれ、別々の経路を通って
同じブロックで再び合流する組が、その端子の競合です。

```text
  races: 6  (入力端子 1本)
  ■ 入力端子 CLK  競合 6件
   rp001 分岐点 CLKBUF から PRE_N 2段 / WL0 2段(差0段)  @ CCC#0 [(top)] ...
   rp002 分岐点 CKI から CKBI 1段 / CKI 0段(分岐点そのもの)(差1段)  @ CCC#16 [XLAT] ...
```

- 一覧は**段数差の小さい順**(際どい順)で、番号 `rp001` … もこの順です。
- ビットごとに同じ形の競合は1件にまとめ(`同型×N`)、関係するすべての入力端子の下に出します。
  `report.html` ではカードを最初の端子の下に置き、ほかの端子からはリンクします。
- 競合が0件の入力端子も一覧に出します。
- `report.html` と画面の上部には入力端子のチップが並び、押すとその端子の節へ移動します。
- 測定文は、合流するブロックの構造に合わせて選びます(SAE と BL の組なら SA 入力の差電圧、ラッチならホールドマージン、
  それ以外は2本の到達時間差)。そのため `races.json` には内部の判定(`class`)が残りますが、一覧やレポートには出しません。

### 段数の読み方

段数は、**分岐点**(2本の経路が分かれる最後の共通ネット)から、合流するブロックの入力までに通るゲートの数です。
インバータや NAND など、チャネルでつながったトランジスタのまとまり1つを1段と数えます。

```text
rp001 sae_bl     分岐点 CLKBUF から SAE 6段 / WL0 2段(差4段)
```

これは「CLKBUF で分かれたあと、SAE まではインバータ6段、WL0 までは2段」という意味です。
`0段(分岐点そのもの)` は、その入力自体が分岐点であることを表します。
`races.meas` のコメントにある「CLK と同相/逆相」は、起点(CLK など)から見た信号の向き(段数の偶奇)です。

詳しい判定条件・出力形式は [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) を参照してください。

## 固定端子(テストモード端子など)を除外する

PININFO で入力になっている端子は、すべて起点(信号が変化する端子)として扱われます。テストモード端子やスキャンイネーブルのように
通常動作では固定の端子があると、それだけから作られる信号同士の合流が「競合」として出てしまいます。
そうした端子は固定端子として指定してください。

```json
{"static_inputs": ["TM*", "SCAN_EN", "TEST*"]}
```

または `racepath analyze macro.cdl --static "TM*" --static SCAN_EN`、画面ではオプションの「固定端子」欄に書きます。

- 指定した端子は起点になりません。CLK などからの経路が固定端子のゲートを通る競合は、これまでどおり検出します。
- 該当した端子はコンソール(`static inputs: ...`)とレポートの見出しに表示されます。名前が合わなければ `(none matched ...)` と出ます。
- デッキでは、固定端子は 1GΩ で接地(0V)されます。1 に固定する端子はコーナー定義の `static` に `{"TM": "vdd_m"}` のように書いてください。

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
| `static_inputs` | `[]` | 固定端子のグロブ(例 `["TM*", "SCAN_EN", "TEST*"]`)。通常動作で固定の入力ピンを起点から外す |
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
src/racepath/        CLI 本体(cli.py)、ジョブ管理(jobs.py)、ブラウザ画面(web.py)
tests/               受け入れテスト、Python/JS 一致テスト、合成アレイ・ランダム回路の生成、誤分類の再現ケース(cases/)
examples/            デモネットリスト(CDL / Spectre)とコーナー定義
skill/racepath/      Claude Code スキル
src/racepath/js/     JavaScript 版の解析エンジン(racepath.js)とブラウザ内のジョブ管理(jobs.js)
racepath.html        1ファイル版(組み立て済み。開くだけで使える)
scripts/             install_skill.sh、racepath.html の組み立て(build_offline_html.py)と確認(check_offline_html.js)
docs/BUILD_SPEC.md   仕様書
```

変更のルールは [CLAUDE.md](CLAUDE.md)、変更履歴は [CHANGELOG.md](CHANGELOG.md) にあります。誤分類は Issue の「誤分類の報告」テンプレートで記録してください。

## ライセンス

MIT
