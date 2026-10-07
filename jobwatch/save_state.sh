#!/usr/bin/env bash
# Commit jobwatch's state files and push, retrying if another run pushed first.
set -e
git config user.name "jobwatch"
git config user.email "jobwatch@users.noreply.github.com"
git add -A jobwatch/
git diff --cached --quiet && exit 0
git commit -q -m "$1"
for i in 1 2 3 4; do
  git push -q && exit 0
  sleep $((i * 5))
  git pull --rebase -q -X theirs || { git rebase --abort; exit 1; }
done
exit 1
