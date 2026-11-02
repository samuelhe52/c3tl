#!/bin/sh
# Symlink c3tl onto PATH.
set -eu

repo=$(cd "$(dirname "$0")" && pwd)
bin_dir=${C3TL_BIN_DIR:-"$HOME/.local/bin"}

mkdir -p "$bin_dir"
chmod +x "$repo/c3tl.py"
ln -sfn "$repo/c3tl.py" "$bin_dir/c3tl"

echo "linked $bin_dir/c3tl"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "warning: $bin_dir is not on PATH" >&2 ;;
esac
