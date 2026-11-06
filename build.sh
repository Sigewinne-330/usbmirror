#!/bin/sh
# 把 src/ 里的源码打包成 dist/U盘镜像备份.app (ad-hoc 签名)
set -eu
cd "$(dirname "$0")"
NAME="U盘镜像备份"
APP="dist/$NAME.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp macos/Info.plist          "$APP/Contents/Info.plist"
cp macos/launcher.sh         "$APP/Contents/MacOS/usbmirror"
chmod +x                     "$APP/Contents/MacOS/usbmirror"
cp src/usbmirror.py          "$APP/Contents/Resources/usbmirror.py"
cp assets/AppIcon.icns       "$APP/Contents/Resources/AppIcon.icns"

# ad-hoc 签名: 「完全磁盘访问权限」按签名身份授予, 不签的话每次改动都要重新授权
codesign --force --deep --sign - "$APP"
echo "✅ 已生成 $APP"
