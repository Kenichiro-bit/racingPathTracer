# 変更履歴

このプロジェクトは [Semantic Versioning](https://semver.org/lang/ja/) に従います。

## [0.3.0] - 2026-11-27

### 追加
- **`racepath.html`**:開くだけで使える1ファイル版(約110KB)。ネットリストを選んで「解析する」を押すだけで、インストール・サーバー・インターネット接続なしに解析・レポート・ダウンロード・デッキ作成・集計ができる。画面は `racepath serve` と同じ。リポジトリ直下に置き、Releases にも添付する。
- **JavaScript 版の解析エンジン** `src/racepath/js/racepath.js`:Python 版(cli.py)の移植。出力が Python 版とバイト単位で一致することを一致テストで毎回確かめる。約10万Tr を1秒程度で解析する。
- `src/racepath/js/jobs.js`:ブラウザ内のジョブ管理(メモリのみ、デッキ zip の生成)。
- テスト:Python/JS 一致テスト(デモ・オプション・設定・アレイ・ランダム回路40種・decks・collect)、ブラウザ内ジョブ管理のテスト、`racepath.html` が最新か確かめるテスト。`tests/gen_random.py`(ランダム回路の生成)。
- `scripts/build_offline_html.py`(組み立て・`--check`)と `scripts/check_offline_html.js`(`file://` でネット遮断のまま全工程を確認)。
- CI:一致テストと `racepath.html` のブラウザ確認を毎回実行し、`v*` タグの push でリリースに `racepath.html` を自動添付する。
- デッキ作成に「ネットリストと races.meas を zip に入れてファイル名で参照する」オプション(`serve` の画面で選択可。`racepath.html` では常に有効)。

### 変更
- 解析・デッキ・集計のジョブ管理を `racepath/jobs.py` に分離し、HTTP サーバー(web.py)から独立させた。検出ロジックと出力形式は変更なし。

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
