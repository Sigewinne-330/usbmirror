#!/bin/sh
# 自动探测一个带 tkinter 的 Python 解释器。
# 不写死版本路径 —— 用户换 Python 环境(uv / homebrew / python.org)时不会再失效。
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"

candidates() {
  # 用户当前默认 (uv 等工具维护的软链)
  echo "$HOME/.local/bin/python3"
  # Homebrew
  for v in 3.14 3.13 3.12 3.11; do
    echo "/opt/homebrew/bin/python$v"
    echo "/opt/homebrew/opt/python@$v/bin/python$v"
  done
  echo /opt/homebrew/bin/python3
  # python.org framework (新版本优先)
  ls -d /Library/Frameworks/Python.framework/Versions/*/bin/python3 2>/dev/null | sort -rV
  # uv 自管理的版本 (新版本优先)
  ls -d "$HOME/.local/share/uv/python"/*/bin/python3 2>/dev/null | sort -rV
  # pyenv
  ls -d "$HOME/.pyenv/versions"/*/bin/python3 2>/dev/null | sort -rV
  echo /usr/local/bin/python3
  # PATH 上的
  command -v python3 2>/dev/null
  # 系统自带, 最后兜底
  echo /usr/bin/python3
}

FOUND=""
for p in $(candidates); do
  [ -x "$p" ] || continue
  "$p" -c 'import tkinter' >/dev/null 2>&1 || continue
  FOUND="$p"; break
done

if [ -z "$FOUND" ]; then
  osascript -e 'display alert "U盘镜像备份 无法启动" message "系统里找不到带 tkinter 的 Python 解释器。

请安装其中之一后重试：
  • brew install python-tk
  • uv python install 3.13
  • 从 python.org 下载安装包" as critical' >/dev/null 2>&1
  exit 1
fi

exec "$FOUND" "$DIR/usbmirror.py" "$@"
