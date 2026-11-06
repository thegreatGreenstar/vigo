# The Linux half of the CI matrix, in one image: Python 3.12, 3.13 and 3.14.
#
# Codespaces runs Linux only, so this image is what `tests/ci-codespace.sh`
# gets and the whole of the Linux question.  Ubuntu 24.04 ships 3.12; 3.13 and
# 3.14 come from the deadsnakes PPA, which builds them for noble.
#
# The ruff version is the same pin as `.github/workflows/tests.yml` and
# `tests/ci-local.sh`.  `tests/test_ci_pins.py` fails when they drift apart.
FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

ARG RUFF_VERSION=0.17.4

RUN apt-get update \
 && apt-get install -y --no-install-recommends software-properties-common \
 && add-apt-repository -y ppa:deadsnakes/ppa \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.13 python3.14 \
 && rm -rf /var/lib/apt/lists/* \
 && python3.12 -m venv /opt/lint \
 && /opt/lint/bin/pip install --disable-pip-version-check --no-cache-dir \
        "ruff==${RUFF_VERSION}" \
 && ln -s /opt/lint/bin/ruff /usr/local/bin/ruff \
 && ruff --version
