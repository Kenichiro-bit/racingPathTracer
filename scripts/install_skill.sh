#!/usr/bin/env bash
# Install racepath (editable) and link the Claude Code skill to this checkout,
# so that `git pull` updates both the tool and the skill.
#
#   scripts/install_skill.sh            # uses python3 on PATH
#   PYTHON=.venv/bin/python scripts/install_skill.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
TARGET="$SKILLS_DIR/racepath"
SOURCE="$REPO/skill/racepath"

echo "==> pip install -e \"$REPO[yaml]\"  ($PYTHON)"
"$PYTHON" -m pip install -e "$REPO[yaml]"

mkdir -p "$SKILLS_DIR"
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    echo "警告: $TARGET はシンボリックリンクではない実体のファイル/フォルダです。上書きせずに中止します。" >&2
    echo "       中身を確認して退避・削除してから、もう一度実行してください。" >&2
    exit 1
fi
ln -sfn "$SOURCE" "$TARGET"
echo "==> linked $TARGET -> $(readlink "$TARGET")"

if command -v racepath >/dev/null 2>&1; then
    echo "==> $(racepath --version 2>&1 | sed 's/^/racepath /')  ($(command -v racepath))"
else
    echo "注意: racepath コマンドが PATH にありません。venv に入れた場合は、Claude Code を起動する前に venv を有効にしてください。" >&2
fi
echo "完了。更新は git pull だけでツールとスキルの両方に反映されます。"
