#!/usr/bin/env bash
# Installs the toolchain for the "verilog" language in languages.yaml.
set -euo pipefail
apt-get install -y --no-install-recommends iverilog
