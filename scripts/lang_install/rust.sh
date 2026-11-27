#!/usr/bin/env bash
# Installs the toolchain for the "rust" language in languages.yaml.
set -euo pipefail
apt-get install -y --no-install-recommends rustc
