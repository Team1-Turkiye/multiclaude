#!/usr/bin/env bash
# Installs the multiclaude CLI and links the skill into every Claude Code config dir.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

mkdir -p "$BIN_DIR"
chmod +x "$HERE/multiclaude.py"
ln -sf "$HERE/multiclaude.py" "$BIN_DIR/multiclaude"
echo "✓ CLI → $BIN_DIR/multiclaude"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "  ⚠ $BIN_DIR is not on your PATH; add it to your shell profile." ;; esac

if [[ -n "${MULTICLAUDE_ACCOUNTS:-}" ]]; then
  IFS=',' read -ra DIRS <<< "$MULTICLAUDE_ACCOUNTS"
else
  DIRS=("$HOME/.claude" "$HOME"/.claude_*)
fi
for d in "${DIRS[@]}"; do
  d="${d/#\~/$HOME}"
  [[ -d "$d" ]] || continue
  mkdir -p "$d/skills"
  ln -sfn "$HERE/skill" "$d/skills/multiclaude"
  echo "✓ skill → $d/skills/multiclaude"
done
echo
"$BIN_DIR/multiclaude" accounts || true
