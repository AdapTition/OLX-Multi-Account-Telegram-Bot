#!/usr/bin/env bash
# Запуск на Linux/macOS без Docker.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
exec .venv/bin/python main.py
