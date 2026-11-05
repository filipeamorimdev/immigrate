#!/usr/bin/env bash
# Downloads the two sample Photos libraries the tests use into .samples/.
set -euo pipefail

target=".samples"
if [[ -d "$target/tests/Test-26.1.photoslibrary" ]]; then
  echo "Sample libraries are already in $target."
  exit 0
fi

git clone --quiet --depth 1 --filter=blob:none --sparse \
  https://github.com/RhetTbull/osxphotos.git "$target"
git -C "$target" sparse-checkout set --no-cone \
  /tests/Test-26.1.photoslibrary \
  /tests/Test-Live-15.7.2.photoslibrary
echo "Sample libraries are in $target/tests."
