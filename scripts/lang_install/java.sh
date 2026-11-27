#!/usr/bin/env bash
# Installs the toolchain for the "java" language in languages.yaml.
set -euo pipefail
apt-get install -y --no-install-recommends default-jdk-headless
