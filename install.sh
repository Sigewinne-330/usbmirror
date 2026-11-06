#!/bin/sh
# 构建并安装到 ~/Applications, 同时装好「插盘自动备份」用的 LaunchAgent
#   ./install.sh            安装 / 更新
#   ./install.sh uninstall  卸载 app 和 LaunchAgent (保留 ~/.config/usbmirror 配置)
set -eu
cd "$(dirname "$0")"
NAME="U盘镜像备份"
LABEL="${USBMIRROR_LABEL:-com.usbmirror.onmount}"
DEST="$HOME/Applications/$NAME.app"
CFG_DIR="$HOME/.config/usbmirror"
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
  rm -f "$AGENT" "$CFG_DIR/on-mount.sh"
  rm -rf "$DEST"
  echo "已卸载 (配置保留在 $CFG_DIR)"
  exit 0
fi

./build.sh
mkdir -p "$HOME/Applications" "$CFG_DIR" "$HOME/Library/Logs/usbmirror" "$HOME/Library/LaunchAgents"
rm -rf "$DEST"
cp -R "dist/$NAME.app" "$DEST"

cp launchd/on-mount.sh "$CFG_DIR/on-mount.sh"
chmod +x "$CFG_DIR/on-mount.sh"
sed -e "s|__HOME__|$HOME|g" -e "s|__LABEL__|$LABEL|g" \
    launchd/usbmirror.plist.template > "$AGENT"
launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENT"

echo "✅ 已安装到 $DEST"
echo "   LaunchAgent: $AGENT"
command -v rsync >/dev/null && rsync --version | grep -q openrsync \
  && echo "⚠️  当前 rsync 是系统自带的 openrsync, 请先 brew install rsync"
true
