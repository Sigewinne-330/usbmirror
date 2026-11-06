# U盘镜像备份 (usbmirror)

插上指定的 U 盘，就自动把选定的文件夹镜像过去的 macOS 小工具。专门针对 **exFAT** 格式的 U 盘做了处理，备份出来的盘在 Windows 上也能正常读。

基于 Python + tkinter 写的图形界面，底层用 GNU rsync 同步。

## 功能

- **插盘自动备份**：通过 launchd 的 `StartOnMount` 监听挂载事件，按**卷 UUID** 识别目标盘（换一个同名 U 盘不会误触发），倒计时几秒后开始备份
- **三种模式**，每个文件夹可以单独设置：
  - `镜像`：U 盘和本机保持一致，本机删掉的文件 U 盘上也删
  - `镜像+回收站`：同上，但被删或被覆盖的文件先按时间戳移到 U 盘上的 `_已删除/` 目录
  - `只增不删`：只复制新文件和改过的文件，从不删除
- **先预览再执行**：「预览改动」用 rsync dry-run 列出要新增、更新、删除的文件
- **exFAT 兼容**
  - `: | \ ? * " < >` 这些 exFAT/Windows 不允许的字符替换成对应的全角字符，并在 `_备份信息/` 下写一份名称映射表；「还原改名」可以把拷回来的文件改回原名。每个文件夹都可以关掉改名
  - 处理 NFC/NFD 文件名差异（`--iconv=UTF-8,UTF-8-MAC`），避免文件在两次备份之间反复删掉又拷回
  - 修改时间允许 2 秒误差，自动清理 `._*` AppleDouble 文件
  - 路径过长（接近 Windows 260 字符上限）时给出警告
- **防误删保护**：删除数超过 `max_delete`（默认 500）就停下来；源文件夹读出来是空的而 U 盘上有内容时直接中止
- **备份微信**（可选，需手动点击）：先退出微信，把聊天数据库和收到的文件原样镜像到 U 盘，完成后再打开微信。文件名保持原样，方便以后整个拷回去用
- 完成后可以自动推出 U 盘；日志写在 `~/Library/Logs/usbmirror/`

## 依赖

- macOS（Apple Silicon 或 Intel 都可以）
- 带 tkinter 的 Python 3.11 及以上版本，下面任选一种安装方式：
  ```bash
  brew install python-tk
  ```
  或者 `uv python install 3.13`，或者从 python.org 下载安装包。启动脚本会自动找到可用的解释器。
- GNU rsync 3.1 或更高版本（系统自带的 openrsync 不行）：
  ```bash
  brew install rsync
  ```

## 安装

```bash
git clone https://github.com/Sigewinne-330/usbmirror.git
```

```bash
cd usbmirror && ./install.sh
```

`install.sh` 会：

1. 用 `build.sh` 打包出 `dist/U盘镜像备份.app`（ad-hoc 签名）
2. 复制到 `~/Applications/`
3. 把 `launchd/on-mount.sh` 装到 `~/.config/usbmirror/`，并注册一个 LaunchAgent，用来实现插盘自动备份

如果只想打包、不安装，运行 `./build.sh` 就可以。卸载请运行 `./install.sh uninstall`，配置文件会保留下来。

### 授予完全磁盘访问权限

要备份「桌面」「文稿」、微信数据这些受保护的目录，需要到 **系统设置 → 隐私与安全性 → 完全磁盘访问权限** 里添加 `~/Applications/U盘镜像备份.app`。

## 配置

第一次运行后会生成 `~/.config/usbmirror/config.json`。文件夹列表、模式、排除规则都可以在界面里改；**目标 U 盘目前需要手动填写**：

1. 插上 U 盘，查出它的卷 UUID：
   ```bash
   diskutil info /Volumes/你的U盘名 | grep "Volume UUID"
   ```
2. 编辑 `config.json`：
   ```json
   {
     "volume_uuid": "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
     "volume_name": "你的U盘名",
     "dest_base": "Mac备份"
   }
   ```
   备份会写到 U 盘上的 `dest_base` 目录里。

其他配置项：

| 键 | 说明 | 默认值 |
|---|---|---|
| `auto_on_mount` | 插盘后自动备份 | `true` |
| `auto_countdown` | 自动备份前的倒计时（秒） | `5` |
| `max_delete` | 单个文件夹一次最多删除多少个文件 | `500` |
| `eject_when_done` | 完成后推出 U 盘 | `false` |
| `excludes` | 排除规则（类似 gitignore 的写法） | `.DS_Store`、`._*` 等 |
| `wechat` | 微信备份的目录、模式和退出等待时间 | 见源码中的 `DEFAULT_CONFIG` |

## 目录结构

```
src/usbmirror.py              主程序（界面和同步引擎）
macos/Info.plist              app 包信息
macos/launcher.sh             app 的启动脚本，负责寻找带 tkinter 的 Python
assets/AppIcon.icns           图标
launchd/on-mount.sh           挂载事件钩子：只有目标盘挂载时才启动 app
launchd/usbmirror.plist.template  LaunchAgent 模板
build.sh / install.sh         打包 / 安装脚本
```

## 命令行参数

- `--auto`：自动模式。目标盘不在的话直接退出；在的话倒计时后开始备份，完成后自动关闭窗口
- `--probe-wechat`：诊断用，以 app 自己的身份试着读取微信目录，结果写到 `~/Library/Logs/usbmirror/probe.txt`

## 许可证

[MIT](LICENSE)
