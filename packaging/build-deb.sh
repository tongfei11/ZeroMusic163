#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
modules=
if [ "${1:-}" = --node-modules ] && [ "$#" -eq 2 ]; then modules=$2; shift 2; fi
[ "$#" -eq 0 ] || { echo 'Usage: build-deb.sh [--node-modules PATH]' >&2; exit 2; }
[ "$(dpkg --print-architecture)" = arm64 ] || { echo 'Build on Debian ARM64.' >&2; exit 1; }
node -e 'if (Number(process.versions.node.split(".")[0]) < 20) process.exit(1)'
version=$(node -p 'require(process.argv[1]).version' "$root/package.json")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
trap 'exit 1' HUP INT TERM
app=$stage/usr/lib/zero-music163
launcher=$stage/usr/share/APPLaunch
docs=$stage/usr/share/doc/zero-music163
mkdir -p "$app" "$launcher/applications" "$launcher/share/images" "$docs" "$stage/DEBIAN" "$root/dist"
chmod 755 "$stage"

for file in app.py artwork.py device.py mpv_ipc.py ncm_resident.py netease.py playback_resume.py player.py \
    setup_web.py uninstall.py zero_music163.py ncm-isolate.cjs ncm-resident.cjs VERSION package.json package-lock.json; do
    install -m 644 "$root/$file" "$app/"
done
if [ -n "$modules" ]; then
    python3 - "$modules" "$app" <<'PY'
import json
from pathlib import Path
import shutil
import sys

source, app = (Path(value).resolve() for value in sys.argv[1:])
packages = json.loads((app / "package-lock.json").read_text())["packages"]
for name, details in packages.items():
    if not name:
        continue
    manifest = source.parent / name / "package.json"
    if json.loads(manifest.read_text())["version"] != details["version"]:
        raise SystemExit(f"Dependency version mismatch: {name}")

def omit(directory, names):
    if Path(directory) == source / "ffprobe-static" and "bin" in names:
        return ["bin"]
    return []

shutil.copytree(source, app / "node_modules", symlinks=True, ignore=omit)
PY
else
    npm --prefix "$app" ci --omit=dev --ignore-scripts --no-audit --no-fund --prefer-offline
    # The app plays through system mpv. ffprobe-static ships no Linux ARM64 binary.
    rm -rf "$app/node_modules/ffprobe-static/bin"
fi
# The app plays through system mpv. ffprobe-static ships no Linux ARM64 binary.
node "$app/node_modules/@music163/ncm-cli/dist/index.js" --version

sed 's@/usr/local/lib/zero-music163/@/usr/lib/zero-music163/@' "$root/zero-music163.desktop" \
    > "$launcher/applications/zero-music163.desktop"
python3 - "$root/zero-music163.png" "$launcher/share/images/zero-music163.png" <<'PY'
from PIL import Image
import sys

with Image.open(sys.argv[1]) as source:
    source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS).save(sys.argv[2], optimize=True)
PY

{
    printf 'ZeroMusic163\n\n'
    cat "$root/LICENSE"
    printf '\nBundled npm dependencies retain their published license files.\n'
    printf 'The @music163/ncm-cli package declares the MIT license in its package metadata.\n'
} > "$docs/copyright"

size=$(du -sk "$stage/usr" | cut -f1)
cat > "$stage/DEBIAN/control" <<CONTROL
Package: zero-music163
Version: $version
Architecture: arm64
Maintainer: tongfei11 <tongfei11@users.noreply.github.com>
Section: sound
Priority: optional
Installed-Size: $size
Depends: nodejs (>= 20), mpv, python3 (>= 3.10), python3-pil, python3-qrcode, fonts-noto-cjk
Homepage: https://github.com/tongfei11/ZeroMusic163
Description: NetEase Cloud Music player for Cardputer Zero
 Browse playlists, search and play music from the built-in keyboard.
CONTROL
dpkg-deb --root-owner-group -Zxz -z3 --build "$stage" "$root/dist/zero-music163_${version}_arm64.deb"
