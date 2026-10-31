#!/usr/bin/env bash
# Clones the bot-data branch into data/ if it exists yet. The first run has none,
# and save-data.sh creates it.
set -euo pipefail
if git ls-remote --exit-code --heads origin bot-data >/dev/null; then
  git clone --quiet --depth 1 --branch bot-data \
    "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" data
else
  echo "No bot-data branch yet; this run's save creates it."
fi
