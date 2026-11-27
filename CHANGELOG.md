# 変更履歴

このプロジェクトは [Semantic Versioning](https://semver.org/lang/ja/) に従います。

## [0.3.0] - 2026-11-27

### 追加
- **オフライン版 `racepath.html`**:HTML ファイル1つ(約7MB)をダブルクリックで開き、ネットリストを選ぶだけで解析できる。インストール・サーバー・インターネット接続は不要。Pyodide(ブラウザで動く Python)と racepath 本体を埋め込み、解析はすべてブラウザの中で行う(ネットリストは外に出ない)。画面は `racepath serve` と同じで、解析・レポート・ダウンロード・デッキ作成・集計ができる。
- `scripts/build_offline_html.py`:オフライン版を組み立てる(Pyodide 0.29.5 を npm レジストリから取得し、チェックサムを確認)。
- `scripts/check_offline_html.js`:オフライン版を `file://` で開き、ネットワークを遮断した状態で全工程を確かめる(CI で実行)。
- CI:オフライン版を組み立てて成果物に保存し、`v*` タグの push でリリースに `racepath.html` を自動で添付する。
- デッキ作成に「ネットリストと races.meas を zip に入れてファイル名で参照する」オプション(`serve` の画面で選択可。オフライン版では常に有効)。

### 変更
- 解析・デッキ・集計のジョブ管理を `racepath/jobs.py` に分離し、`serve` とオフライン版で共通にした。検出ロジックと出力形式は変更なし。

## [0.2.0] - 2026-11-27

### 追加
- `racepath serve`:ローカルのブラウザ画面。ネットリストのアップロード(またはパス指定)とオプション指定で解析し、`report.html` の表示と出力ファイルのダウンロードができる。コーナーデッキの作成(zip でダウンロード)と、結果の集計(フォルダごとアップロード、またはパス指定)も画面から行える。過去の解析も一覧から開き直せる。
- 標準ライブラリの `http.server` だけで動き、依存は増えていない。既定では `127.0.0.1` のみで待ち受け、起動ごとのトークンと Host ヘッダーの確認で、ほかのサイトからの操作を防ぐ。
- 解析結果は作業フォルダ(既定は `racepath_web/`)に残る。

### 変更なし
- `analyze` / `decks` / `collect` の検出ロジックと出力形式は変えていない。

## [0.1.0] - 2026-11-27

### 追加
- 初版。動作確認済みの `racepath.py` を Python パッケージ `racepath` として取り込み(検出ロジック・出力形式は変更なし)。
- サブコマンド `analyze` / `decks` / `collect`。`racepath` コマンドと `python -m racepath` で実行できる。
- Claude Code スキル(`skill/racepath/`)と、それを `~/.claude/skills/racepath` にリンクする `scripts/install_skill.sh`。
- 受け入れテスト(デモ回路で6件、32×8 アレイで6グループ×8、256×64 アレイの解析速度、`decks`、`collect`)。
- Spectre 版のデモネットリスト `examples/sram_demo.scs`。
- GitHub Actions(Python 3.9〜3.13)と、誤分類報告用の Issue テンプレート。
