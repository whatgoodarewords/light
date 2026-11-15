#!/bin/bash
# Builds what `light` needs, in this folder:
#   .venv       Python with bleak and cryptography (made if missing)
#   Light.app   the small signed helper app that owns the Bluetooth permission
#   Lamp.app    the menu bar panel (light gui)
# Then runs the offline tests.
#
#   bash build.sh
#
# LIGHT_PYTHON=/path/to/python3    Python 3.11 or newer for a new .venv
# LIGHT_SIGN_ID=<name or SHA-1>    signing identity. Default: your first
#     "Apple Development" certificate, so a rebuild does not ask for Bluetooth
#     permission again; without one, an ad-hoc signature (asks again per rebuild).
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${LIGHT_PYTHON:-/opt/homebrew/bin/python3.13}"
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
.venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "The .venv Python is older than 3.11. Set LIGHT_PYTHON and run again." >&2; exit 1; }
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

mkdir -p Light.app/Contents/MacOS
.venv/bin/python - <<'PY'
import plistlib
from pathlib import Path
Path('Light.app/Contents/Info.plist').write_bytes(plistlib.dumps({
    'CFBundleIdentifier': 'local.govee-light.helper',
    'CFBundleName': 'Light',
    'CFBundleDisplayName': 'Light',
    'CFBundleExecutable': 'Light',
    'CFBundlePackageType': 'APPL',
    'CFBundleShortVersionString': '2.0',
    'CFBundleVersion': '2',
    'LSMinimumSystemVersion': '12.0',
    'LSUIElement': True,
    'NSBluetoothAlwaysUsageDescription': 'Light uses Bluetooth to control your Govee lamp GVH6008DBA0 from the command line.',
}))
PY
clang -O2 -Wall -o Light.app/Contents/MacOS/Light helper-launcher.c
SIGN="${LIGHT_SIGN_ID:-}"
if [ -z "$SIGN" ]; then
  SIGN="$(security find-identity -v -p codesigning 2>/dev/null | awk '/Apple Development/ {print $2; exit}')"
fi
codesign --force --sign "${SIGN:--}" --identifier local.govee-light.helper Light.app
codesign --verify --strict Light.app

# Lamp.app: the menu bar panel (Swift). It only talks to the helper, so it needs no permissions.
mkdir -p Lamp.app/Contents/MacOS
.venv/bin/python - <<'PY'
import plistlib
from pathlib import Path
Path('Lamp.app/Contents/Info.plist').write_bytes(plistlib.dumps({
    'CFBundleIdentifier': 'local.govee-light.menubar',
    'CFBundleName': 'Lamp',
    'CFBundleDisplayName': 'Lamp',
    'CFBundleExecutable': 'Lamp',
    'CFBundlePackageType': 'APPL',
    'CFBundleShortVersionString': '1.0',
    'CFBundleVersion': '1',
    'LSMinimumSystemVersion': '13.0',
    'LSUIElement': True,
}))
PY
swiftc -O -swift-version 5 -o Lamp.app/Contents/MacOS/Lamp gui/*.swift
codesign --force --sign "${SIGN:--}" --identifier local.govee-light.menubar Lamp.app
codesign --verify --strict Lamp.app

chmod +x light
.venv/bin/python -m unittest discover -s tests -q
echo "Built Light.app (signed with: ${SIGN:-ad-hoc}). Try: ./light"
