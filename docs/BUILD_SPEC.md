# Claude Code への依頼文:racepath をゼロから作る

> **このリポジトリでの配置(v0.1.0 以降)**
> 本仕様の `racepath.py` は Python パッケージ `racepath` として `src/racepath/cli.py` に置かれ、
> `racepath` コマンドまたは `python -m racepath` で実行する。対応 Python は 3.9 以上。
> テストは `tests/test_racepath.py`、合成アレイ生成は `tests/gen_array.py`、スキルは `skill/racepath/SKILL.md`。
> 分類ルールや出力形式を変えたときは、この文書も同時に更新する。

Claude Code に自分で作らせたいときは、この文書をそのまま渡す。
(出来上がったものを使うだけなら、racepath_claude_code.zip のスキルを置けば足りる。)

---

## 依頼
トランジスタレベルのネットリストから**レーシングパス(競合パス)**を自動で見つけ、測るべき内部ノード(オートプローブ)を選んで `.measure` 文を生成するCLIツール `racepath.py` を作ってください。
- Python 3.8以降、標準ライブラリのみ(YAML設定を読むときだけPyYAMLを任意で使う)
- 1ファイル構成。サブコマンドは `analyze` / `decks` / `collect` の3つ(v0.2.0 で、ブラウザ画面の `serve` を `web.py` に追加。9章)
- 最後に `tests/test_racepath.py` を作り、下の受け入れ条件を満たすことを確認する

## 1. 入力の読み込み
- **CDL/SPICE**:`.SUBCKT` と `.ENDS`、`+` による継続行、`*` コメント、`$` 以降のインラインコメント、`*.PININFO`(ピン方向)、`.GLOBAL`。
  - X行は `X1 a b / SUB`、`X1 a b SUB`、`$PINS A=n1 B=n2`(名前による接続)の3形式。
  - M行はノード4つ + モデル名。`$[model]` の形も受ける。
  - R行は抵抗として扱う。
- **Spectre**:`subckt NAME (ports)`(括弧なしの形も)と `ends`、`//` コメント、行末 `\` による継続、`global`、`name (nodes) master params` の形式。`simulator lang=spice/spectre` で読み方を切り替える。`parameters` / `model` / 解析文は読み飛ばす。
- 形式の自動判定(拡張子 `.scs` か、`subckt` / `simulator lang=spectre` の有無で判定)。
- MOS判定はモデル名の正規表現で行う:PMOSは `^p|pch|pmos|pfet`、NMOSは `^n|nch|nmos|nfet`(設定で変更可)。X行でも、未定義マスタかつノード3〜4個でモデル名が一致すればMOSとみなす。
- 階層を展開する:区切りは `.`、グローバルネットと電源にはプレフィックスを付けない。
- トップは自動判定する(どこからもインスタンスされないsubcktのうちデバイス数が最大のもの)。`--top` で指定もできる。
- 電源はネット末尾の名前をグロブで判定する。既定は `VDD* VSS* GND* VCC* VPWR* VGND* VPP* VBB* AVDD* AVSS* DVDD* DVSS* 0`。
- 抵抗はショートとみなしてUnion-Findでネットを統合する(寄生抽出後のネットリスト対策)。代表名は電源を優先し、なければ名前が短いほうを使う。

## 2. グラフと競合検出
- **CCC(チャネル接続ブロック)**:非電源のS/DノードをUnion-Findでつなぐ。CCCの入力は、ゲートネットのうちCCC内部にも電源にもないもの。
- **BFS**:起点ネットから「ネット → そのネットをゲートに持つCCC → CCC内の全ネット」とたどり、各ネットの最短経路(CCC段数)と、どこから来たかを記録する。
  - 起点の既定値:PININFOで `I` のピン。なければ、どのCCCのメンバーでもないトップポート(=どのチャネルにも駆動されていないピン)。
- **合流点**:到達可能な入力を2本以上持つCCC。
  - 経路が自分自身のCCCを通る入力は帰還として除外し、そのCCCを「帰還あり」とする。
  - バスの兄弟(`WL<0>` と `WL<1>`、`\<` によるエスケープも考慮)は代表1本にまとめてから組を作る(デコードで排他なため)。
- 2本の経路が最後に共有するネットを**分岐点**とし、そこから先の各パス(ネット列)と段数を記録する。
  - 一方の入力がもう一方の経路上にある組には `derived` を付ける。
- **同型のまとめ**:(種別、数字とインデックスを除いた入力名、CCCの階層名、段数A・B)が同じ競合は1件にまとめ、`group_size` と `group_members` を持たせる。`--no-group` で全件出力する。

## 3. 分類(上から順に判定)
CCC内で入力gが駆動する素子について、次を判定しておく。
- `pass_only`:S/Dのどちらも電源につながっていない
- `drives`:S/Dのどちらかが電源につながっている
- `precharge`:全部がPMOSで、少なくとも1つが電源につながっている

| 種別 | 条件 | プローブ |
|---|---|---|
| `sae_bl` | CCC内に交差結合対があり(gate_i ∈ S/D_j かつ gate_j ∈ S/D_i)、共通ソースが非電源で、そのノードと電源の間にテール素子がある。テール素子のゲートが一方の入力で、もう一方が `pass_only` | sae, wl, sa_t, sa_c(交差結合ノード)、bl_t, bl_c(SAノードからパス素子を1つ越えた先のネット) |
| `hold` | CCCに帰還があり、一方が `pass_only`(クロック)、もう一方が `drives`(データ) | clk, data_in, data_node(データ入力が駆動するCCC内のノード) |
| `wl_pre` | 一方が `precharge`、もう一方が `pass_only` | pre, wl |
| `clock_pair` | 両方とも `pass_only` | a, b |
| `generic` | 上のどれにも当たらない | a, b |

設定 `extra_checks: [{"class","a","b",...}]` で、手動の測定を追加できるようにする。

## 4. 出力(`analyze -o DIR`)
- `races.meas`:HSPICE構文。Spectreでは `simulator lang=spice` の区間で読み込む。測定対象のノード名は `v(<probe_prefix><net>)` とし、既定プレフィックスは `XDUT.`。電源にはプレフィックスを付けず、`\<` は `<` に戻す。参照するパラメータは `vdd_m t_m th_mid th_pre sa_dv_req`。
  - **sae_bl**:`tsae`(SAEの交差)、`twl`、`find v(sa_t/sa_c) at=tsae`、`dv=abs差`、`margin=dv-sa_dv_req`、`dt_wl_sae`。BLが見つかれば `dvbl` も出す。
  - **wl_pre**:`margin = twl_on - tpre_off`(PREの閾値は `th_pre*vdd_m`、`cross=1`)と、`margin_end = tpre_on - twl_off`(`cross=2`)。
  - **hold**:`margin = tdata(data_node) - tclk`。
  - **clock_pair / generic**:`skew = ta - tb`。
  - 交差の判定はすべて `when v(x)='th_mid*vdd_m' cross=N td='t_m'`。
  - 各競合の前に、コメントで「種別、合流CCC、段数、分岐点、NG条件(日本語)、同型件数」を書く。
- `races_params.inc`:上記パラメータの既定値(vdd 0.8、t_m 1n、th_mid 0.5、th_pre 0.9、sa_dv_req 0.05)。
- `races.json`:meta(ネットリスト、形式、トップ、ポート、PININFO、起点、Tr数、CCC数、トップの電源、probe_prefix、cfg、警告)と races。races の各要素には `explain` と `meas` 行も含める。
- `races.csv`:UTF-8(BOM付き)。日本語の種別名と explain の列を入れる。
- `report.html`:単一ファイルのHTMLで、スマホでも読めるカード表示にする。
  - 各カード:ID、種別タグ(色分け)、フラグ(派生、手動、同型×N)、**日本語のNG条件説明**、パスA・B(ネット列と段数)、分岐点、合流CCC、プローブのチップ。
  - 下部に競合ごとの測定文ブロックを置く。
  - **ノード名をタップすると該当の測定文ブロックへスクロールし、そのノードを使っている `v(...)` をハイライトする**。測定対象でない中間ノードなら、到達時刻を測る `.measure` 文を1行表示してコピーボタンを付ける。
- **NG条件の日本語説明**(実際のネット名を埋め込む):
  - sae_bl:WLが開いてBL差が広がる前にSAEが立ち上がると誤読出し。SA入力差が `sa_dv_req` V未満でNG。
  - wl_pre:プリチャージが切れる前にWLが開くと電流が衝突。終了側も同様。
  - hold:ラッチが閉じる前に次のデータが届くとホールド違反。
  - clock_pair:ずれが大きいと両方が開く、または両方が閉じる瞬間ができる。
  - generic(derived):時間差がパルス幅になる。
  - generic:到達順がコーナーで入れ替わるとグリッチ。

## 5. `decks RACES_JSON CORNERS_JSON [--sim spectre|hspice]`
- コーナーごとにデッキを1つ作る:モデルの `lib`([path, section])、`.temp`、パラメータ(`vdd` とコーナー別の `params`)、ネットリストの include、measの include、`XDUT <ports> <top>`。
- 電源ポートは、名前が high 系なら `vdd_m`、それ以外は0の電圧源にする。
- 起点ポートには `stimulus` の設定でパルス源を付ける(既定:delay 1n、period 4n、width 2n、tr 20p)。`static` 指定のポートは直流源、残りのポートは1GΩで接地する。
- `stimulus_file` を指定した場合はそれをincludeし、自動の信号源は出さない。
- Spectreデッキでは、CDLのネットリストとmeasを `simulator lang=spice` の区間で読む。
- `run_all.sh` も出力する。

## 6. `collect RACES_JSON RESULTS...`
- 読む結果:`key = value` 形式(Spectreの `.measure`、`.lis`、`.log`)と、HSPICEの `.mt0` の表形式(`alter#` まで列名が並び、その後に値)。単位接尾辞(f p n u m k meg g)と `failed` に対応する。
- コーナー名はファイル名から取る。ファイル名が `tran1` などの場合は親ディレクトリ名(`.raw` は除く)を使う。
- 指標ごとに全コーナーの値、最悪値、最悪コーナー、判定をCSVに出す。
  - margin系の判定:<0でNG、警告閾値未満で要注意(時間は `--warn` 20ps、sae_blは `--warn-v` 0.02V)、それ以外はOK。
  - skew系の判定:コーナー間で符号が変われば「順序反転」、絶対値が10ps未満なら「要確認」。
  - 欠測値がある場合は「測定失敗あり」とする。

## 7. 受け入れ条件(テスト)
次のCDL(とSpectreで同じ内容のもの)を解析したとき、競合がちょうど次の6件になること。
- sae_bl:SAE vs WL0(分岐点CLKBUF、SAE側6段、WL0側2段)
- wl_pre:PRE_N vs WL0
- hold:CKBI vs SAT と CKI vs SAT(data_node = XLAT.DL)
- clock_pair:CKBI vs CKI
- generic:PRE_N vs SAE

追加の条件:
- 合成アレイ(32行×8列、WLに直列抵抗あり、`/` と `$PINS` 構文を使用)が6グループになり、各グループの `group_size` が8であること。
- 256行×64列(約10万Tr)を数秒で解析できること。
- `decks`(spectre/hspice)と `collect`(spectre形式、mt0形式、`failed`)が動くこと。

```
* racepath demo netlist (CDL)
.GLOBAL VDD VSS
.SUBCKT INV A Y VDD VSS
MP0 Y A VDD VDD pch W=0.4u L=0.04u
MN0 Y A VSS VSS nch W=0.2u L=0.04u
.ENDS
.SUBCKT SRAM6T BL BLB WL VDD VSS
MPU0 Q QB VDD VDD pch W=0.1u L=0.04u
MPD0 Q QB VSS VSS nch W=0.2u L=0.04u
MPU1 QB Q VDD VDD pch W=0.1u L=0.04u
MPD1 QB Q VSS VSS nch W=0.2u L=0.04u
MPG0 BL WL Q VSS nch W=0.15u L=0.04u
MPG1 BLB WL QB VSS nch W=0.15u L=0.04u
.ENDS
.SUBCKT SA BL BLB SAE SAT SAC VDD VSS
MCP0 BL SAE SAT VDD pch W=0.3u L=0.04u
MCP1 BLB SAE SAC VDD pch W=0.3u L=0.04u
MP1 SAT SAC VDD VDD pch W=0.3u L=0.04u
MP2 SAC SAT VDD VDD pch W=0.3u L=0.04u
MN1 SAT SAC TAIL VSS nch W=0.6u L=0.04u
MN2 SAC SAT TAIL VSS nch W=0.6u L=0.04u
MNT TAIL SAE VSS VSS nch W=1u L=0.04u
.ENDS
.SUBCKT LATCH DIN CK CKB Q VDD VSS
XDRV DIN DL VDD VSS INV
MTN DL CKB X VSS nch W=0.2u L=0.04u
MTP DL CK X VDD pch W=0.4u L=0.04u
XQ X Q VDD VSS INV
MFP1 F1 Q VDD VDD pch W=0.2u L=0.04u
MFP2 X CKB F1 VDD pch W=0.2u L=0.04u
MFN2 X CK F2 VSS nch W=0.1u L=0.04u
MFN1 F2 Q VSS VSS nch W=0.1u L=0.04u
.ENDS
.SUBCKT SRAM_DEMO CLK Q VDD VSS
*.PININFO CLK:I Q:O VDD:B VSS:B
XI0 CLK CLKB VDD VSS INV
XI1 CLKB CLKBUF VDD VSS INV
XW0 CLKBUF WLB VDD VSS INV
XW1 WLB WL0 VDD VSS INV
XP0 CLKBUF PREB VDD VSS INV
XP1 PREB PRE_N VDD VSS INV
XS0 CLKBUF S1 VDD VSS INV
XS1 S1 S2 VDD VSS INV
XS2 S2 S3 VDD VSS INV
XS3 S3 S4 VDD VSS INV
XS4 S4 S5 VDD VSS INV
XS5 S5 SAE VDD VSS INV
MPRE0 BL PRE_N VDD VDD pch W=0.5u L=0.04u
MPRE1 BLB PRE_N VDD VDD pch W=0.5u L=0.04u
MEQ BL PRE_N BLB VDD pch W=0.3u L=0.04u
XC0 BL BLB WL0 VDD VSS SRAM6T
XC1 BL BLB VSS VDD VSS SRAM6T
XSA BL BLB SAE SAT SAC VDD VSS SA
XL0 CLKBUF CKI VDD VSS INV
XL1 CKI CKBI VDD VSS INV
XLAT SAT CKBI CKI Q VDD VSS LATCH
.ENDS
```

## 8. 作り方の指示
1. まず受け入れテストを書き、それが通るように実装する。
2. 実装後、`report.html` をブラウザで開き(Playwrightがあれば自動で)、ノードのタップでハイライトされることを確認する。
3. 完成後、`.claude/skills/racepath/SKILL.md` を作る。内容は「ネットリストのパスを受け取る → analyze → 警告を潰す → races.json を日本語で要約する → 望まれたら decks/collect」の手順。

## 9. `serve`(ブラウザ画面、v0.2.0)
- `racepath serve [--host 127.0.0.1] [--port 8765] [--workdir racepath_web] [--no-browser]`。標準ライブラリの `http.server` だけで実装する(`src/racepath/web.py`)。
- 処理はすべて既存のサブコマンド(`analyze` / `decks` / `collect`)をプロセス内で呼び、検出ロジックと出力形式はCLIと同じにする。コンソール出力は画面に表示する。
- 画面(日本語、スマホ幅で横スクロールなし、ダークモード対応):
  1. 解析:ネットリストのアップロード(ドロップ可)か、PC上のパス指定。オプションは `-t`、`-s`、`-f`、`--prefix`、`--no-group`、設定JSON。結果は要約(トップ、形式、Tr数、CCC数、起点、競合数、種別ごとの件数)、警告、`report.html` の埋め込み表示、出力ファイルのダウンロード。
  2. デッキ作成:コーナー定義JSON(既定値は `examples/corners.json` と同じ)とシミュレータを指定し、`decks/` を作って zip で渡す。シミュレーションは実行しない。
  3. 集計:結果ファイルのアップロード(フォルダ選択可。測定結果の拡張子 `.mt* .ms* .measure .lis .log .out` だけを送り、フォルダ構成を保つので `tran1` の場合も親フォルダ名でコーナー名が決まる)か、パス指定。警告閾値を指定できる。表は判定の列を先頭側に並べる(`summary.csv` の列順はCLIのまま)。
  4. 過去の解析の一覧と再表示。
- 作業フォルダの1回の解析ごとにジョブフォルダ(`<日時>_<ネットリスト名>`)を作り、ネットリスト(アップロード時)、`out/`、`decks/`、`decks.zip`、`results/`、`summary.csv`、`analyze.log`、`job.json` を置く。
- 安全対策:既定で `127.0.0.1` のみで待ち受ける。起動ごとのランダムなトークンを全APIで必須にし(他サイトからの操作を防ぐ)、ループバック時は Host ヘッダーを確認する(DNSリバインディング対策)。ダウンロードできるのはジョブフォルダ内の出力ファイルだけ。アップロードされたファイル名は無害化し、ジョブフォルダの外に書かない。
