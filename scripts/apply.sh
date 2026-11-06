#!/usr/bin/env bash
# Checks out meta/buck2 at the commit patches/buck2/version.txt pins into $1 and applies patches/buck2/*.patch in order, stopping at the first one that does not apply.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
dest="${1:?usage: scripts/apply.sh <directory to check buck2 out into>}"
pin="$(tr -d '[:space:]' < "${root}/patches/buck2/version.txt")"

if [ ! -d "${dest}/.git" ]; then
  git init -q "${dest}"
  git -C "${dest}" remote add origin https://github.com/meta/buck2.git
fi
git -C "${dest}" fetch -q --depth=1 origin "${pin}"
git -C "${dest}" checkout -q --detach "${pin}"
git -C "${dest}" clean -qfdx

# git am records a committer; the patches keep their authors.
export GIT_COMMITTER_NAME="buck2-patches" GIT_COMMITTER_EMAIL="buck2-patches@users.noreply.github.com"
for patch in "${root}"/patches/buck2/*.patch; do
  if ! git -C "${dest}" am --quiet --no-3way "${patch}"; then
    echo "::error title=Patch does not apply::$(basename "${patch}") does not apply to meta/buck2 ${pin}" >&2
    git -C "${dest}" am --show-current-patch=diff | head -n 40 >&2 || true
    git -C "${dest}" am --abort || true
    exit 1
  fi
  echo "applied $(basename "${patch}")"
done
git -C "${dest}" log --oneline "${pin}..HEAD"
