# 変更履歴

このプロジェクトは [Semantic Versioning](https://semver.org/lang/ja/) に従います。

## [0.1.0] - 2026-11-27

### 追加
- 初版。動作確認済みの `racepath.py` を Python パッケージ `racepath` として取り込み(検出ロジック・出力形式は変更なし)。
- サブコマンド `analyze` / `decks` / `collect`。`racepath` コマンドと `python -m racepath` で実行できる。
- Claude Code スキル(`skill/racepath/`)と、それを `~/.claude/skills/racepath` にリンクする `scripts/install_skill.sh`。
- 受け入れテスト(デモ回路で6件、32×8 アレイで6グループ×8、256×64 アレイの解析速度、`decks`、`collect`)。
- Spectre 版のデモネットリスト `examples/sram_demo.scs`。
- GitHub Actions(Python 3.9〜3.13)と、誤分類報告用の Issue テンプレート。
