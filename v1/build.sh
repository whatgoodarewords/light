#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
PYTHON="${LIGHT_PYTHON:-/opt/homebrew/bin/python3.13}"
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt
mkdir -p 'Govee Light.app/Contents/MacOS'
.venv/bin/python - <<'PY'
import plistlib
from pathlib import Path
p = Path('Govee Light.app/Contents/Info.plist')
p.write_bytes(plistlib.dumps({
    'CFBundleIdentifier': 'local.govee.light',
    'CFBundleName': 'Govee Light',
    'CFBundleExecutable': 'GoveeLight',
    'CFBundlePackageType': 'APPL',
    'CFBundleVersion': '1',
    'LSUIElement': True,
    'NSBluetoothAlwaysUsageDescription': 'Control the Govee light GVH6008DBA0 in your room.',
}))
PY
INCLUDE="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"
LIBRARY="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR") + "/libpython3.13.dylib")')"
clang -O2 -I"$INCLUDE" "-DLIGHT_ROOT=\"$ROOT\"" launcher.c "$LIBRARY" -o 'Govee Light.app/Contents/MacOS/GoveeLight'
codesign --force --sign - --identifier local.govee.light 'Govee Light.app'
codesign --verify --deep --strict 'Govee Light.app'
chmod +x light
./light --help
