#!/usr/bin/env bash
# Installs the toolchain for the "javascript" language in languages.yaml.
set -euo pipefail
apt-get install -y --no-install-recommends nodejs
