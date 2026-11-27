---
name: racepath
description: トランジスタレベルのネットリスト(CDL/Spectre)からレーシングパス(競合パス)を検出し、オートプローブの .measure 文・PVTコーナーデッキ・マージン集計を作る。ネットリストを渡されて「レーシングパス」「競合」「race」「オートプローブ」「SAEとBL」「ホールド」などを調べてと頼まれたとき、または /racepath <netlist> で呼ばれたときに使う。
---

# racepath: ネットリストを入れるだけでレーシングパス解析

`racepath` コマンド(`pip install` で入る。Python標準ライブラリのみ)を使う。
`racepath` が見つからない(`command not found`)ときは、代わりに `python3 -m racepath` を使う。
それも失敗するなら未インストールなので、リポジトリで `scripts/install_skill.sh` を実行するようユーザーに伝える。
ユーザーにはネットリストのパスだけ受け取り、残りはここの手順で進める。報告は日本語で書く。

## 1. 解析
```
racepath analyze <netlist> -o <netlistのstem>_racepath
```
- 起点ネット(`-s`)とトップ(`--top`)は自動判定される。ユーザーが指定したときだけ付ける。
- プロジェクト直下に `racepath.json`(設定ファイル)があれば `-c racepath.json` を付ける。

## 2. 出力を確認(自動判定の誤りを先に潰す)
コンソール出力を読み、次に当てはまれば直してから再実行する。黙って先に進まない。
| 症状 | 対処 |
|---|---|
| `model '…' not recognised as MOS` | モデル名の判定漏れ。`racepath.json` の `pmos_models` / `nmos_models` に正規表現を追加 |
| `several top candidates` | ユーザーにトップセルを確認して `--top` を付ける |
| `sources:` が想定と違う(CLKが無い、出力ピンが入っている) | ユーザーに起点を確認して `-s CLK` などを付ける |
| `sources:` にテストモード・スキャン・ヒューズなど通常動作で固定の端子(TM, TEST*, SCAN_EN など)が入っている | ユーザーに確認して `racepath.json` の `static_inputs` に追加(または `--static`)。固定端子だけから生まれる偽の競合が消える |
| `transistors=0` や `races: 0` | フォーマット(`-f cdl` / `-f spectre`)、トップ、`supplies` の設定を疑う |
| 電源名が `VDD*`/`VSS*` 系以外(例: VDDM, VCS) | `racepath.json` の `supplies` に追加 |

`racepath.json` を新規に作るときは、必要なキーだけ書く(例: `{"supplies": ["VDD*","VSS*","VCS*"], "sources": ["CLK"]}`)。

## 3. 日本語で要約して報告
`<out>/races.json` を読み、次の形で短くまとめる。
- 1行目:トップ名、トランジスタ数、起点、競合の総数と種別ごとの件数
- 種別ごとに主要な競合を挙げる:ID、`explain`(NG条件の説明)をそのまま使い、分岐点と2本のパスの段数を添える
- 注目点:段数差が小さい組、`derived`(パルス生成系)、`group_size` が大きい同型の競合、`has_feedback` のホールド系
- `report.html` のパス(ブラウザで開くとノード名から測定文へ移動できる)と、`races.meas` のパスを伝える

解析結果から言えるのは構造だけ。「どちらが速いか」「NGかどうか」はシミュレーション前に断定しない。

## 4. ユーザーが望んだら:コーナーデッキ → 実行 → 集計
1. コーナー定義:プロジェクトの `racepath_corners.json` を使う。無ければこのスキルのフォルダの `templates/corners.json` をコピーし、モデルファイルのパスとセクション名、VDD、温度、クロック周期をユーザーに確認して埋める。
2. デッキを作る:
   ```
   racepath decks <out>/races.json racepath_corners.json -o <out>/decks [--sim hspice]
   ```
   自前のテストベンチを使うなら、`probe_prefix`(DUTの階層パス、既定は `XDUT.`)を合わせて `analyze --prefix` からやり直す。
3. シミュレーション(`<out>/decks/run_all.sh`)は、ライセンスを使い時間もかかるので、**実行前に必ずユーザーに確認する**。
4. 結果を集計する:
   ```
   racepath collect <out>/races.json <out>/decks -o <out>/summary.csv
   ```
   NG・要注意・順序反転・測定失敗を日本語で報告する。測定失敗は `.lis` / `.log` を開いて原因(構文、ノード名の解決、`cross=` で交差が見つからない など)を調べる。

## ブラウザで見たいと言われたら
インストールせずに使いたい、別のPCで使いたい場合は、リポジトリ直下(または Releases)の `racepath.html` を案内する(ダブルクリックで開いてネットリストを選ぶだけ。ネット接続不要で、結果はコマンド版と同じ)。
racepath が入っている環境なら `racepath serve` を案内する(ブラウザ画面で解析・デッキ作成・集計ができる。既定は `http://127.0.0.1:8765/`)。
サーバーは終了するまで動き続けるので、Claude Code から起動する場合はバックグラウンドで実行し、URL を伝える。

## 守ること
- racepath 本体は勝手に書き換えない。分類の誤りやパーサの問題を見つけたら、症状と修正案を示し、ユーザーの了承を得てから直す。直すのは racepath リポジトリ側(`src/racepath/cli.py`)で、そのリポジトリの CLAUDE.md に従う。誤分類はリポジトリの Issue テンプレート「誤分類の報告」で記録するとよい。直したら `templates/sram_demo.cdl` で再解析し、競合が次の6件のままであることを確かめる:sae_bl(SAE vs WL0)、wl_pre(PRE_N vs WL0)、hold×2(CKBI/CKI vs SAT)、clock_pair(CKBI vs CKI)、generic(PRE_N vs SAE)。
