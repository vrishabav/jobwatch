#!/usr/bin/env bash
# Commit jobwatch's state and push. Never auto-resolves conflicts in state files:
# if another run pushed first and the state conflicts, stop (nothing gets sent; the next run redoes the work).
set -e
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A jobwatch/
git diff --cached --quiet && exit 0
git commit -q -m "$1"
for i in 1 2 3; do
  git push -q && exit 0
  sleep $((i * 5))
  if ! git pull --rebase -q; then
    git rebase --abort || true
    echo "State conflict with another run; not pushing (no alerts will be sent this run)." >&2
    exit 1
  fi
done
exit 1
