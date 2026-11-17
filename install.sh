#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"

if [[ $(id -u) == 0 || $(uname -m) != aarch64 || ! -d /usr/share/APPLaunch/applications ]]; then
  echo '请在 Cardputer Zero 上，用运行 APPLaunch 的普通用户执行安装。' >&2
  exit 1
fi

sudo apt-get -o DPkg::Lock::Timeout=30 install -y --no-install-recommends \
  nodejs npm mpv python3-pil python3-qrcode fonts-noto-cjk
node -e 'if (+process.versions.node.split(".")[0] < 20) { console.error("ZeroMusic163 需要 Node.js 20 或更新版本。"); process.exit(1) }'

runtime="$HOME/.local/share/zero-music163/runtime"
install -d "$runtime"
install -m 0644 package.json package-lock.json "$runtime/"
npm --prefix "$runtime" ci --omit=dev --ignore-scripts --no-audit --no-fund

sudo install -d /usr/local/lib/zero-music163
sudo install -m 0644 app.py device.py mpv_ipc.py netease.py player.py setup_web.py zero_music163.py \
  ncm-isolate.cjs VERSION /usr/local/lib/zero-music163/
sudo install -D -m 0644 zero-music163.png /usr/share/APPLaunch/share/images/zero-music163.png
sudo install -m 0644 zero-music163.desktop /usr/share/APPLaunch/applications/
printf '已安装 ZeroMusic163 %s。请从应用启动器打开。\n' "$(cat VERSION)"
