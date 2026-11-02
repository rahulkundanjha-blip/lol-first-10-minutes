#!/usr/bin/env bash
# Downloads the public source file into data/. It isn't redistributed here.
set -euo pipefail
cd "$(dirname "$0")/../data"
curl -fsSL -o lol_diamond_10min.csv "https://raw.githubusercontent.com/jppoter/Project-on-high_diamond_ranked_10min-dataset/8590dae3c3eec748a1f36a7009f65d3e32f9dbbe/high_diamond_ranked_10min.csv"
