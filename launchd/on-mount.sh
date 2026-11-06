#!/bin/sh
# launchd 的 StartOnMount 会在系统**任何一个卷**挂载时触发 (dmg、App Translocation
# 的 nullfs、快照……)。以前直接 open 这个 app、由 app 自己判断目标盘在不在，
# 结果就是每次都先在 Dock 上闪一下再退出。改成在这里先判断, 不该跑就安静退出。
set -u
CFG="$HOME/.config/usbmirror/config.json"
APP="${USBMIRROR_APP:-$HOME/Applications/U盘镜像备份.app}"
STAMP="$HOME/.config/usbmirror/.last-auto-run"
DEBOUNCE=20

[ -f "$CFG" ] || exit 0
/usr/bin/grep -q '"auto_on_mount"[[:space:]]*:[[:space:]]*true' "$CFG" || exit 0

UUID=$(/usr/bin/sed -n 's/.*"volume_uuid"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG")
[ -n "$UUID" ] || exit 0

# ── 关键: 目标盘没挂上就到此为止, 绝不启动 app ──
MP=$(/usr/sbin/diskutil info "$UUID" 2>/dev/null \
     | /usr/bin/awk -F':[[:space:]]+' '/^ *Mount Point/ {print $2; exit}')
[ -n "${MP:-}" ] && [ -d "$MP" ] || { echo "skip: 目标盘未挂载"; exit 0; }

# app 已经在跑就别再开一个
if /usr/bin/pgrep -f "Resources/usbmirror.py" >/dev/null 2>&1; then
  echo "skip: 已有实例在运行"; exit 0
fi

# 防抖: 一次插盘可能连带多个挂载事件
NOW=$(/bin/date +%s)
if [ -f "$STAMP" ]; then
  LAST=$(/bin/cat "$STAMP" 2>/dev/null || echo 0)
  case "$LAST" in ''|*[!0-9]*) LAST=0 ;; esac
  if [ $((NOW - LAST)) -lt $DEBOUNCE ]; then echo "skip: ${DEBOUNCE}秒内刚跑过"; exit 0; fi
fi
echo "$NOW" > "$STAMP"

echo "launch: 目标盘在 $MP, 启动备份"
[ "${USBMIRROR_DRYRUN:-0}" = "1" ] && exit 0
exec /usr/bin/open -a "$APP" --args --auto
