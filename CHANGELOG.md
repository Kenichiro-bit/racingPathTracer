# 変更履歴

このプロジェクトは [Semantic Versioning](https://semver.org/lang/ja/) に従います。

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
