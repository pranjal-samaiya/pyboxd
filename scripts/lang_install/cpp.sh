#!/usr/bin/env bash
# Installs the toolchain for the "cpp" language in languages.yaml.
set -euo pipefail
apt-get install -y --no-install-recommends g++
