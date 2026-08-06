#!/bin/bash
set -eu

# Post-merge: instala deps Python. Idempotente.
if [ -f requirements.txt ]; then
    python -m pip install --quiet --disable-pip-version-check -r requirements.txt
fi
