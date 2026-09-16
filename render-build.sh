#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" python -m playwright install chromium --only-shell
