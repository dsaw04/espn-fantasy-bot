#!/usr/bin/env bash
# Commits whatever the bot wrote to data/ onto the bot-data branch, creating the
# branch on the first run. Usage: save-data.sh <label>
set -euo pipefail
mkdir -p data
cd data
if ! git rev-parse --verify -q HEAD >/dev/null 2>&1; then
  rm -rf .git
  git init -q -b bot-data
  git remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
fi
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
if git diff --cached --quiet; then
  echo "no new data"
  exit 0
fi
git commit -q -m "data: $1 $(date -u +%Y-%m-%dT%H:%MZ)"
git push -q origin bot-data
