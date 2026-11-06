#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""U盘镜像备份 — 插上指定 U 盘后自动把选定文件夹镜像过去 (exFAT 友好)."""
from __future__ import annotations

import fnmatch, json, os, plistlib, queue, re, shutil, signal, subprocess, sys, threading, time, unicodedata
from datetime import datetime
from pathlib import Path

APP_NAME   = "U盘镜像备份"
VERSION    = "1.3"          # 1.3: 每个文件夹可关掉改名 + 还原改名工具
CONFIG_DIR = Path.home() / ".config" / "usbmirror"
CONFIG     = CONFIG_DIR / "config.json"
RULES_DIR  = CONFIG_DIR / "rules"
LOG_DIR    = Path.home() / "Library" / "Logs" / "usbmirror"

# exFAT/Windows 非法字符 -> 全角等价字符 (确定性映射, 不依赖任何保存的状态)
ILLEGAL = {':': '：', '|': '｜', '\\': '＼', '?': '？', '*': '＊',
           '"': '＂', '<': '＜', '>': '＞'}
LONG_PATH_WARN = 230          # Windows MAX_PATH 260, 扣掉 "E:\\Mac备份\\" 前缀约 10 字符
MTIME_TOL      = 2.0          # exFAT 把 mtime 截断到 10ms, 用 2 秒容差

MODES = {"mirror": "镜像", "mirror_trash": "镜像+回收站", "add_only": "只增不删"}
MODE_ORDER = ["mirror", "mirror_trash", "add_only"]

# 首次运行时的示例文件夹, 之后在界面里用「+ 添加 / − 移除」改
DEFAULT_FOLDERS = [str(Path.home() / "Documents"), str(Path.home() / "Desktop")]

_WX = Path.home() / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data"
# 微信 4.x 把东西摊在两处: 聊天库在 Application Support, 收到的文件/图片/视频
# 和 db_storage 在 Documents/xwechat_files。两边靠硬链接共享一部分文件, 但
# exFAT 没有硬链接, 所以 U 盘上会各存一份 —— 这是没办法的事。
WECHAT_APPSUP = _WX / "Library" / "Application Support" / "com.tencent.xinWeChat"
WECHAT_FILES  = _WX / "Documents" / "xwechat_files"

DEFAULT_CONFIG = {
    "volume_uuid": "",            # 目标 U 盘的卷 UUID, 见 README
    "volume_name": "未设置",
    "dest_base": "Mac备份",
    "auto_on_mount": True,
    "auto_countdown": 5,
    "max_delete": 500,
    "eject_when_done": False,
    "folders": [{"path": p, "enabled": True, "mode": "mirror"} for p in DEFAULT_FOLDERS],
    "excludes": [".DS_Store", "._*", ".Spotlight-V100", ".fseventsd", ".Trashes",
                 ".TemporaryItems", ".localized", "__pycache__/",
                 "**/.claude/worktrees/"],
    # 微信单独一条: 不参与插盘自动备份, 只能手动点「备份微信」
    "wechat": {
        # sanitize=False: 微信数据迟早是要整个拷回去给微信用的, U 盘上的名字
        # 必须跟本地一模一样, 否则拷回去微信按数据库里的原名找不到文件。
        "items": [
            {"path": str(WECHAT_APPSUP), "dest": "微信聊天记录", "sanitize": False},
            {"path": str(WECHAT_FILES),  "dest": "微信文件",     "sanitize": False},
        ],
        "mode": "mirror",
        "app": "/Applications/WeChat.app",
        "relaunch": True,        # 备份完自动重新打开微信
        "quit_timeout": 45,      # 等微信退出的秒数
        "max_delete": 5000,      # 微信缓存换得勤, 上限放宽
    },
}


# ───────────────────────── 配置 ─────────────────────────

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG.exists():
        try:
            saved = json.loads(CONFIG.read_text("utf-8"))
        except Exception:
            saved = {}
        wc = saved.pop("wechat", None)
        cfg.update(saved)
        if isinstance(wc, dict):     # 按键合并, 老配置缺字段也不至于塌掉
            cfg["wechat"].update(wc)
    w = cfg["wechat"]
    if not w.get("items"):           # 兼容 1.1 那版只有单个 path 的配置
        w["items"] = [{"path": w.get("path") or str(WECHAT_APPSUP),
                       "dest": w.get("dest") or "微信聊天记录"}]
    w.pop("path", None)
    w.pop("dest", None)
    for it in w["items"]:            # 1.2 存下来的 items 里没有这个字段
        it.setdefault("sanitize", False)   # 微信数据默认不改名, 为的是能原样拷回去
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# ───────────────────── 系统探测 ─────────────────────

def find_rsync() -> str | None:
    """必须是 GNU rsync >= 3.1 (系统自带的 openrsync 没有 --info=progress2)."""
    for cand in ("/opt/homebrew/bin/rsync", "/usr/local/bin/rsync", shutil.which("rsync")):
        if not cand or not Path(cand).exists():
            continue
        try:
            out = subprocess.run([cand, "--version"], capture_output=True,
                                 text=True, timeout=5).stdout
        except Exception:
            continue
        if "openrsync" in out.lower():
            continue
        m = re.search(r"version\s+(\d+)\.(\d+)", out)
        if m and (int(m.group(1)), int(m.group(2))) >= (3, 1):
            return cand
    return None


def target_mount(uuid: str) -> Path | None:
    """按卷 UUID 找挂载点 — 换个同名 U 盘不会误触发."""
    try:
        r = subprocess.run(["diskutil", "info", "-plist", uuid],
                           capture_output=True, timeout=15)
        if r.returncode != 0:
            return None
        mp = plistlib.loads(r.stdout).get("MountPoint") or ""
    except Exception:
        return None
    p = Path(mp)
    return p if mp and p.is_dir() else None


def free_bytes(path: Path) -> int:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except Exception:
        return 0


def human(n: float) -> str:
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


# ─────────────────── 文件名映射 ───────────────────

def needs_map(name: str) -> bool:
    return any(c in ILLEGAL for c in name)


def sanitize(name: str) -> str:
    return "".join(ILLEGAL.get(c, c) for c in name)


def sanitize_rel(rel: str) -> str:
    return "/".join(sanitize(p) for p in rel.split("/"))


def rsync_pat(rel: str) -> str:
    """把相对路径转成锚定的 rsync 模式。

    rsync 有个坑: 反斜杠只有当模式里**同时含通配符**时才被当作转义符, 否则按字面比较。
    所以不能无脑转义 —— 对不含通配符的路径转义反斜杠会导致规则完全失配
    (文件被原样复制一份, 或者 P 保护规则失效导致每轮删了重抄)。
    """
    if any(c in rel for c in "*?["):      # rsync 靠这三个字符判定是否启用通配/转义
        out = "".join("\\" + c if c in "*?[]\\" else c for c in rel)
    else:                                  # 纯字面比较, 反斜杠必须原样保留
        out = rel
    return "/" + out


def _match(pat: str, rel: str, name: str, is_dir: bool) -> bool:
    dir_only = pat.endswith("/")
    p = pat[:-1] if dir_only else pat
    if dir_only and not is_dir:
        return False
    if "/" in p:
        if p.startswith("**/"):
            tail, parts = p[3:], rel.split("/")
            return any(fnmatch.fnmatch("/".join(parts[i:]), tail) for i in range(len(parts)))
        return fnmatch.fnmatch(rel, p.lstrip("/"))
    return fnmatch.fnmatch(name, p)


def is_excluded(rel: str, name: str, is_dir: bool, excludes: list[str]) -> bool:
    return any(_match(e, rel, name, is_dir) for e in excludes)


def scan_mappings(src_root: Path, excludes: list[str], sanitize_on: bool = True):
    """找出名字需要映射的最顶层条目 (映射根), 以及超长路径警告.

    sanitize_on=False 时不做任何改名, 只统计超长路径警告 —— 这样 U 盘上的名字
    和本地一模一样, 将来整个拷回去就能直接用, 代价是那几个文件在 Windows 上
    不好打开。给"迟早要原样还原"的文件夹(比如微信数据)用。
    """
    roots, warns = [], []
    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src_root)
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if is_excluded(rel, d, True, excludes):
                continue
            if sanitize_on and needs_map(d):
                roots.append((rel, True))      # 整个子树交给特殊通道, 不再往下走
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if is_excluded(rel, f, False, excludes):
                continue
            if sanitize_on and needs_map(f):
                roots.append((rel, False))
            if len(f"{src_root.name}/{rel}") > LONG_PATH_WARN:
                warns.append(rel)
    return roots, warns


def expand_mappings(src_root: Path, roots, excludes: list[str]):
    """映射根 -> [(源绝对路径, 目标相对路径)]"""
    pairs = []
    for rel, is_dir in roots:
        if not is_dir:
            pairs.append((src_root / rel, sanitize_rel(rel)))
            continue
        for dp, dns, fns in os.walk(src_root / rel, followlinks=False):
            rd = os.path.relpath(dp, src_root)
            dns[:] = [d for d in dns if not is_excluded(f"{rd}/{d}", d, True, excludes)]
            for f in fns:
                r = f"{rd}/{f}"
                if is_excluded(r, f, False, excludes):
                    continue
                pairs.append((Path(dp) / f, sanitize_rel(r)))
    return pairs


def write_rules(key: str, roots, excludes: list[str]) -> Path:
    """一个 filter 文件同时承担: 保护目标端映射名不被 --delete 删掉 + 排除源端原名 + 通用排除."""
    lines = []
    for rel, is_dir in roots:
        san = rsync_pat(sanitize_rel(rel))
        lines.append(f"P {san}")
        if is_dir:
            lines.append(f"P {san}/***")
        lines.append(f"- {rsync_pat(rel)}")
    lines += [f"- {e}" for e in excludes]
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    path = RULES_DIR / f"{key}.rules"
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def name_variants(name: str) -> list[str]:
    """同一个名字在 exFAT 上可能有几种 Unicode 写法, 挨个试。"""
    out, seen = [], set()
    for n in (name, unicodedata.normalize("NFC", name),
              unicodedata.normalize("NFD", name)):
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def find_on_disk(path: Path) -> Path | None:
    """按几种规范化写法找这个路径, 找到哪个返回哪个。"""
    for n in name_variants(path.name):
        q = path.with_name(n)
        if q.exists() or q.is_symlink():
            return q
    return None


def restore_names(folder: Path, entries: list[dict], log=lambda s: None) -> dict:
    """把备份里被改过名的条目改回原名 —— 严格按映射表, 绝不按规则反推。

    为什么不能反推: 本机本来就有二十多个文件名里自带全角冒号之类的字符,
    无脑把全角换回半角会把这些**本来就对**的名字改错。映射表里记的才作数。
    """
    st = {"done": 0, "already": 0, "missing": 0, "conflict": 0, "errors": []}
    for e in sorted(entries, key=lambda x: x["mapped"].count("/"), reverse=True):
        mapped, orig = e.get("mapped", ""), e.get("orig", "")
        if not mapped or not orig or mapped == orig:
            continue
        src, dst = folder / mapped, folder / orig
        real = find_on_disk(src)
        if real is None:
            if find_on_disk(dst) is not None:
                st["already"] += 1          # 已经还原过了
            else:
                st["missing"] += 1
                log(f"  找不到: {mapped}")
            continue
        if find_on_disk(dst) is not None:
            st["conflict"] += 1
            log(f"  ⚠️  目标已存在, 跳过: {orig}")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(real, dst)
            st["done"] += 1
            log(f"  {Path(mapped).name}  ->  {Path(orig).name}")
        except OSError as ex:
            st["errors"].append(f"{mapped}: {ex}")
            log(f"  ❌ {mapped}: {ex}")
    return st


def unlink_exfat(path: Path) -> bool:
    """在 exFAT 上删文件, 挨个 Unicode 规范化形式试一遍。

    macOS 的 exFAT 驱动把目录项存成 NFC, 但 readdir 交回来的是 NFD(HFS+ 的老
    习惯)。名字里带组合重音符号(é、á、ě …)时两者不一样, 于是出现 os.path.exists()
    说文件在、os.unlink() 却 ENOENT 的怪事 —— 结果就是 ._ 垃圾文件永远删不掉。
    纯 ASCII 和纯中日韩的名字不受影响(它们没有分解形式)。
    """
    for name in name_variants(path.name):
        try:
            os.unlink(path.with_name(name))
            return True
        except FileNotFoundError:
            continue
        except OSError:
            return False
    return False


def differs(src: Path, dst: Path) -> bool:
    """大小 + 修改时间比对 — 一致就跳过, 所以不会每次插盘都重复复制."""
    try:
        a, b = src.stat(), dst.stat()
    except OSError:
        return True
    return a.st_size != b.st_size or abs(a.st_mtime - b.st_mtime) > MTIME_TOL


# ───────────────────────── 引擎 ─────────────────────────

PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\S+)")


# ───────────────────────── 微信 ─────────────────────────

def bundle_id(app: str) -> str:
    try:
        with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier", "")
    except Exception:
        return ""


def wechat_pids(app: str) -> list[int]:
    """靠可执行文件路径认人。

    不能用命令行匹配 (pgrep -f): rsync 的参数里带着 com.tencent.xinWeChat
    这个容器路径, 那样会把自己认成微信, 永远等不到"已退出"。
    ps 的 comm 字段只给命令行的头一个词(argv[0]), 后面的参数一概不算。
    微信由 LaunchServices 拉起, argv[0] 一定是 app 包里的绝对路径。
    """
    prefix = str(Path(app)).rstrip("/") + "/"
    try:
        out = subprocess.run(["ps", "-axo", "pid=,comm="],
                             capture_output=True, text=True).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        pid, _, comm = line.strip().partition(" ")
        if pid.isdigit() and comm.startswith(prefix):
            pids.append(int(pid))
    return pids


def quit_wechat(app: str, log, check=None, timeout: int = 45) -> bool:
    """先请微信自己优雅退出(好让它把 SQLite 落盘), 不理会再发 TERM 信号。"""
    # 必须从 app 包自己的 Info.plist 读 bundle id: 写死一个默认值的话,
    # 万一 app 路径配错了, AppleScript 就会跑去关另一个毫不相干的程序。
    bid = bundle_id(app)
    graceful = False
    if not bid:
        log(f"  读不到 {app} 的 bundle id，跳过优雅退出，直接发信号")
    else:
        try:
            r = subprocess.run(["osascript", "-e", f'tell application id "{bid}" to quit'],
                               capture_output=True, text=True, timeout=20)
            graceful = r.returncode == 0
            if not graceful:
                tail = (r.stderr.strip().splitlines() or ["无输出"])[-1][:90]
                log(f"  优雅退出没成功（{tail}），改用退出信号")
        except subprocess.TimeoutExpired:
            log("  优雅退出超时，改用退出信号")
        except Exception as e:
            log(f"  优雅退出失败（{e}），改用退出信号")

    deadline = time.time() + timeout
    # 微信答应退出了就多给它点时间收尾; 压根没答应就别干等着
    grace = timeout / 2 if graceful else 3.0
    escalated = False
    while True:
        if check:
            check()
        pids = wechat_pids(app)
        if not pids:
            return True
        left = deadline - time.time()
        if left <= 0:
            return False
        if not escalated and left < timeout - grace:
            log(f"  还有 {len(pids)} 个微信进程没退，发送 TERM 信号")
            for q in pids:
                try:
                    os.kill(q, signal.SIGTERM)
                except OSError:
                    pass
            escalated = True
        time.sleep(0.5)


def launch_wechat(app: str) -> None:
    subprocess.run(["open", "-a", app], capture_output=True)


class Cancelled(Exception):
    pass


class FolderPlan:
    def __init__(self, cfg):
        self.cfg = cfg
        self.src = Path(cfg["path"])
        self.name = cfg.get("dest") or self.src.name
        self.adds = self.updates = 0
        self.deletes: list[str] = []
        self.rsync_bytes = 0
        self.roots: list = []
        self.warns: list[str] = []
        self.special: list = []          # 需要复制的 (src, dst_rel)
        self.special_bytes = 0
        self.rules: Path | None = None
        self.error: str | None = None

    @property
    def total_bytes(self):
        return self.rsync_bytes + self.special_bytes

    @property
    def total_files(self):
        return self.adds + self.updates + len(self.special)


class Plan:
    def __init__(self):
        self.folders: list[FolderPlan] = []

    @property
    def total_bytes(self):
        return sum(f.total_bytes for f in self.folders)

    @property
    def total_files(self):
        return sum(f.total_files for f in self.folders)

    @property
    def total_deletes(self):
        return sum(len(f.deletes) for f in self.folders)

    @property
    def mapped(self):
        return sum(len(f.roots) for f in self.folders)

    @property
    def warns(self):
        return [(f.name, w) for f in self.folders for w in f.warns]


class Engine:
    def __init__(self, cfg: dict, rsync: str, emit):
        self.cfg, self.rsync, self.emit = cfg, rsync, emit
        self._cancel = threading.Event()
        self._proc: subprocess.Popen | None = None
        self.env = dict(os.environ, COPYFILE_DISABLE="1")

    # ---- 控制 ----
    def cancel(self):
        self._cancel.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    # ---- 路径 ----
    def dest_root(self, mount: Path) -> Path:
        return mount / self.cfg["dest_base"]

    def base_args(self, mode: str, backup_dir: Path | None = None,
                  max_delete: int | None = None) -> list[str]:
        # --iconv: macOS 的 exFAT 驱动 readdir 交回来的是 NFD 名字, 而源盘上
        # 存的可能是 NFC(比如微信下载的 Thomée/Fernández)。不转换的话 rsync
        # 认为目标多了个文件 -> --delete 把它删掉, 而查找又能命中所以不重传,
        # 结果这些文件在备份里一轮存在一轮消失。UTF-8-MAC 就是 NFD 形式。
        a = [self.rsync, "-rLt", "--no-perms", "--no-owner", "--no-group",
             "--modify-window=1", "--no-inc-recursive", "--iconv=UTF-8,UTF-8-MAC"]
        if mode in ("mirror", "mirror_trash"):
            a.append("--delete")
            a.append(f"--max-delete={max_delete or self.cfg.get('max_delete', 500)}")
        if mode == "mirror_trash" and backup_dir:
            a += ["--backup", f"--backup-dir={backup_dir}"]
        return a

    # ---- 预演 ----
    def plan(self, folders: list[dict], mount: Path) -> Plan:
        plan = Plan()
        droot = self.dest_root(mount)
        for i, fc in enumerate(folders):
            self._check()
            fp = FolderPlan(fc)
            plan.folders.append(fp)
            self.emit("phase", text=f"正在分析 {fp.name} ({i+1}/{len(folders)}) …")
            if not fp.src.is_dir():
                fp.error = "源文件夹不存在"
                continue
            try:
                empty_src = not any(fp.src.iterdir())
            except PermissionError:
                fp.error = "没有读取权限（需在 系统设置→隐私与安全性→完全磁盘访问权限 中授权本 app）"
                continue
            dst_dir = droot / fp.name
            if empty_src and dst_dir.is_dir() and any(dst_dir.iterdir()):
                fp.error = "源文件夹读到的是空的，但 U 盘上有内容 —— 已中止，以免镜像模式把备份删光"
                continue

            fp.roots, fp.warns = scan_mappings(fp.src, self.cfg["excludes"],
                                               fc.get("sanitize", True))
            fp.rules = write_rules(re.sub(r"\W+", "_", str(fp.src)), fp.roots,
                                   self.cfg["excludes"])

            cmd = self.base_args(fc["mode"], None, fc.get("max_delete")) + [
                "--dry-run", "--itemize-changes", "--stats",
                f"--filter=. {fp.rules}", f"{fp.src}/", f"{droot / fp.name}/"]
            r = subprocess.run(cmd, capture_output=True, text=True, env=self.env)
            if r.returncode not in (0, 24):
                fp.error = (r.stderr.strip().splitlines() or ["rsync 预演失败"])[-1]
                continue

            for line in r.stdout.splitlines():
                if line.startswith("*deleting"):
                    fp.deletes.append(line.split(None, 1)[-1])
                elif len(line) > 11 and line[0] in "<>ch" and line[1] == "f":
                    if "+++++++++" in line:
                        fp.adds += 1
                    else:
                        fp.updates += 1
            m = re.search(r"Total transferred file size:\s*([\d,]+)", r.stdout)
            fp.rsync_bytes = int(m.group(1).replace(",", "")) if m else 0

            # 特殊通道: 名字含非法字符的文件, 逐个比对大小+时间
            for src, drel in expand_mappings(fp.src, fp.roots, self.cfg["excludes"]):
                self._check()
                dst = droot / fp.name / drel
                if differs(src, dst):
                    try:
                        fp.special_bytes += src.stat().st_size
                    except OSError:
                        continue
                    fp.special.append((src, drel))
        return plan

    # ---- 执行 ----
    def run(self, plan: Plan, mount: Path, log) -> dict:
        droot = self.dest_root(mount)
        droot.mkdir(parents=True, exist_ok=True)
        total = max(plan.total_bytes, 1)
        done = 0
        stats = {"files": 0, "deleted": 0, "mapped": 0, "junk": 0, "errors": []}
        stamp = datetime.now().strftime("%Y-%m-%d")

        for fp in plan.folders:
            self._check()
            if fp.error:
                stats["errors"].append(f"{fp.name}: {fp.error}")
                continue
            self.emit("phase", text=f"正在同步 {fp.name} …")
            dst = droot / fp.name
            dst.mkdir(parents=True, exist_ok=True)
            bdir = droot / "_已删除" / stamp / fp.name if fp.cfg["mode"] == "mirror_trash" else None

            cmd = self.base_args(fp.cfg["mode"], bdir, fp.cfg.get("max_delete")) + [
                "--info=progress2,del", "--out-format=%n",
                f"--filter=. {fp.rules}", f"{fp.src}/", f"{dst}/"]
            log(f"$ {' '.join(cmd)}")
            done0 = done
            done, rc, errs = self._run_rsync(cmd, done, total, fp, stats, log)
            # 23 = 有文件没传成。实测碰到过 open() 被信号打断(EINTR)的偶发情况,
            # rsync 自己不重试, 结果就是备份里静悄悄少了几个文件。重跑一遍只会
            # 补那几个, 很快; 真正打不开的文件重跑照样报错, 那时才算数。
            if rc == 23 and not self._cancel.is_set():
                log(f"  有 {len(errs)} 个文件这轮没拷成，自动重跑一遍补齐 …")
                before = stats["files"]
                done, rc, errs = self._run_rsync(cmd, done0, total, fp, stats, log)
                log(f"  重试补上 {stats['files'] - before} 个文件"
                    + ("" if rc in (0, 24) else f"，仍有问题 (退出码 {rc})"))
            for e in errs:
                stats["errors"].append(f"{fp.name}: {e}")
            if rc == 25:
                lim = fp.cfg.get("max_delete") or self.cfg.get("max_delete", 500)
                stats["errors"].append(
                    f"{fp.name}: 要删除的文件超过 {lim} 个上限，已中止删除。"
                    "源文件夹是不是没读到？")
            elif rc not in (0, 24):
                stats["errors"].append(f"{fp.name}: rsync 退出码 {rc}")

            # 特殊通道
            for src, drel in fp.special:
                self._check()
                target = dst / drel
                self.emit("progress", done=done, total=total,
                          name=f"{fp.name}/{drel}", note="改名复制")
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    done = self._copy(src, target, done, total, fp.name, drel)
                    stats["mapped"] += 1
                    stats["files"] += 1
                    log(f"  改名 {src.name}  ->  {Path(drel).name}")
                except Cancelled:
                    raise
                except Exception as e:
                    stats["errors"].append(f"{fp.name}/{drel}: {e}")
                    log(f"  ❌ {drel}: {e}")

            if fp.cfg["mode"] in ("mirror", "mirror_trash"):
                stats["deleted"] += self._prune_mapped(fp, dst, log)

        self._write_manifest(droot, plan, log)          # 先写映射表, 再清理, 否则它自己会留个 ._
        stats["junk"] = self._clean_appledouble(droot, log, mount)
        self.emit("progress", done=total, total=total, name="", note="")
        return stats

    def _run_rsync(self, cmd, done, total, fp, stats, log) -> tuple[int, int, list[str]]:
        """跑一轮 rsync。错误不直接记进 stats —— 交给调用方决定要不要重试。"""
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=self.env)
        errs: list[str] = []
        t = threading.Thread(target=lambda: errs.extend(
            self._proc.stderr.read().decode("utf-8", "replace").splitlines()), daemon=True)
        t.start()

        base, cur, buf = done, 0, b""
        fd = self._proc.stdout.fileno()
        while True:
            if self._cancel.is_set():
                self._proc.terminate()
                raise Cancelled()
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                m = PROGRESS_RE.match(line)
                if m:
                    cur = int(m.group(1).replace(",", ""))
                    self.emit("progress", done=base + cur, total=total,
                              name=None, note=m.group(3))
                elif line.startswith("deleting "):
                    stats["deleted"] += 1
                elif not line.endswith("/"):      # %n 给目录加结尾斜杠, 目录不算文件
                    stats["files"] += 1
                    self.emit("progress", done=base + cur, total=total,
                              name=f"{fp.name}/{line}", note=None)
        rc = self._proc.wait()
        t.join(timeout=2)
        self._proc = None
        errs = [e for e in errs if e.strip()]
        for e in errs:
            log(f"  ⚠️  {e}")
        return base + max(cur, fp.rsync_bytes), rc, errs

    def _copy(self, src: Path, dst: Path, done: int, total: int,
              fname: str, drel: str) -> int:
        """分块复制, 不带 xattr — 避免 macOS 在 exFAT 上生成 ._ 伴随文件."""
        tmp = dst.with_name(dst.name + ".usbmirror-tmp")
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    self._check()
                    b = fi.read(1 << 20)
                    if not b:
                        break
                    fo.write(b)
                    done += len(b)
                    self.emit("progress", done=done, total=total,
                              name=f"{fname}/{drel}", note="改名复制")
            st = src.stat()
            os.utime(tmp, (st.st_atime, st.st_mtime))
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return done

    def _prune_mapped(self, fp: FolderPlan, dst: Path, log) -> int:
        """rsync 的 --delete 被 P 规则挡在映射子树外, 这里自己清理孤儿。

        两边都先归一到 NFC 再比: os.walk 在 exFAT 上交回来的是 NFD, 而 want
        里是源盘的写法(可能是 NFC)。不归一的话, 名字带重音符号的文件每轮都会
        被当成孤儿删掉 —— 而它刚刚才被特殊通道拷过去。
        """
        n = 0
        norm = lambda x: unicodedata.normalize("NFC", x)
        for rel, is_dir in fp.roots:
            if not is_dir:
                continue
            want = {norm(sanitize_rel(r)) for _, r in
                    expand_mappings(fp.src, [(rel, True)], self.cfg["excludes"])}
            base = dst / sanitize_rel(rel)
            if not base.is_dir():
                continue
            for dp, _, fns in os.walk(base, topdown=False):
                for f in fns:
                    p = Path(dp) / f
                    if norm(os.path.relpath(p, dst)) not in want:
                        if not unlink_exfat(p):
                            log(f"  ⚠️  删不掉 {os.path.relpath(p, dst)}")
                            continue
                        log(f"  删除 {os.path.relpath(p, dst)}")
                        n += 1
        return n

    def _clean_appledouble(self, droot: Path, log, mount: Path | None = None) -> int:
        """macOS 内核会给 exFAT 上每个带扩展属性的文件自动生成 4KB 的 ._ 伴随文件。
        源目录里并不存在它们, rsync 管不着, 只能同步完自己扫掉 —— 否则插到
        Windows 上每个文件旁边都跟着一个垃圾文件。第一遍会漏掉一些(目录的 ._
        在遍历过程中被重建), 所以反复跑到归零为止。跑满上限还没归零只会
        剩几个隐藏文件, 不值得再纠缠 —— n==0 时会提前跳出, 多留几轮不花钱。"""
        self.emit("phase", text="正在清理 macOS 残留文件 …")
        total = 0
        for _ in range(6):
            n = 0
            for dp, dns, fns in os.walk(droot, topdown=False):
                self._check()
                junk = [f for f in fns if f.startswith("._") or f == ".DS_Store"]
                if not junk:
                    continue
                try:                       # 记下目录时间戳
                    st = os.stat(dp)
                except OSError:
                    st = None
                for f in junk:
                    if unlink_exfat(Path(dp) / f):
                        n += 1
                if st:                     # 删完恢复, 否则 rsync 下一轮会去回修目录 mtime
                    try:
                        os.utime(dp, (st.st_atime, st.st_mtime))
                    except OSError:
                        pass
            total += n
            if n == 0:
                break
        if mount:                                  # 备份根目录自己的 ._ 在 droot 之外
            if unlink_exfat(mount / f"._{droot.name}"):
                total += 1
        if total:
            log(f"  清理 {total} 个 macOS 残留文件 (._ / .DS_Store)")
        return total

    def _write_manifest(self, droot: Path, plan: Plan, log) -> None:
        """每个备份文件夹写自己的一份映射表。

        以前是所有文件夹合写一个 名称映射.txt —— 「备份微信」只备份微信那两个
        目录, 写出来的表会把另外 7 个文件夹的映射覆盖掉, 反过来也一样。
        """
        info = droot / "_备份信息"
        try:
            info.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return log(f"  ⚠️  映射表目录创建失败: {e}")
        for fp in plan.folders:
            if fp.error:
                continue
            keeps = not fp.cfg.get("sanitize", True)
            out = [f"# {APP_NAME} v{VERSION} 名称映射表 — {fp.name}",
                   f"# 生成于 {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
            if keeps:
                out += ["# 这个文件夹关闭了改名 —— U 盘上的文件名和本机完全一致，",
                        "# 整个拷回原位就能直接用，不需要任何还原步骤。"]
            else:
                out += ["# 这些文件的名字含 exFAT/Windows 非法字符, 在 U 盘上已替换为全角等价字符。",
                        "# 拷回本机后请用本程序的「还原改名」把名字改回去, 否则引用",
                        "# 这些文件的程序(比如 QQ音乐)会找不到它们。", ""]
                for rel, _is_dir in fp.roots:
                    out += [f"  原名  : {rel}", f"  U盘名: {sanitize_rel(rel)}", ""]
                if not fp.roots:
                    out.append("(这个文件夹这次没有需要改名的文件)")
            name = re.sub(r'[/\\:|?*"<>]', "_", fp.name)
            try:
                (info / f"名称映射-{name}.txt").write_text("\n".join(out), "utf-8")
                # 人看的是 txt, 「还原改名」读的是这份 json —— 路径里本来就有冒号,
                # 拿 txt 反向解析纯属自找麻烦。
                (info / f"名称映射-{name}.json").write_text(json.dumps(
                    {"folder": fp.name, "version": VERSION, "sanitize": not keeps,
                     "generated": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                     "entries": [{"orig": rel, "mapped": sanitize_rel(rel),
                                  "is_dir": bool(is_dir)} for rel, is_dir in fp.roots]},
                    ensure_ascii=False, indent=1), "utf-8")
            except Exception as e:
                log(f"  ⚠️  {fp.name} 映射表写入失败: {e}")
        legacy = info / "名称映射.txt"          # 1.1 及以前的合并版, 已被上面拆开
        if legacy.exists() and unlink_exfat(legacy):
            log("  映射表已按文件夹拆分，旧的合并版 名称映射.txt 已删除")


# ───────────────────────── 界面 ─────────────────────────

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText


class App:
    def __init__(self, root: tk.Tk, auto: bool):
        self.root, self.auto = root, auto
        self.cfg = load_config()
        self.rsync = find_rsync()
        self.q: queue.Queue = queue.Queue()
        self.engine: Engine | None = None
        self.worker: threading.Thread | None = None
        self.mount: Path | None = None
        self.busy = False
        self.countdown_job = None
        self.sizes: dict[str, str] = {}
        self.logfile = None

        root.title(f"{APP_NAME} v{VERSION}")
        root.minsize(780, 560)
        self._build()
        self._refresh_target()
        self._reload_tree()
        threading.Thread(target=self._measure_sizes, daemon=True).start()
        self.root.after(80, self._pump)

        if not self.rsync:
            self.status.config(text="❌ 找不到 GNU rsync ≥ 3.1，请先运行  brew install rsync",
                               foreground="#c0392b")
            self.btn_run.state(["disabled"])
            self.btn_plan.state(["disabled"])
            self.btn_wx.state(["disabled"])
        elif auto and self.mount:
            self._start_countdown()

    # ---- 构建界面 ----
    def _build(self):
        pad = dict(padx=12, pady=(0, 8))
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=12, pady=(12, 8))
        self.status = ttk.Label(top, text="", font=("-size", 13))
        self.status.pack(side="left")
        ttk.Button(top, text="刷新", width=6, command=self._refresh_target).pack(side="right")

        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=True, **pad)
        cols = ("en", "name", "size", "mode", "san")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8,
                                 selectmode="browse")
        for c, t, w, anc in (("en", "", 36, "center"), ("name", "文件夹", 320, "w"),
                             ("size", "大小", 88, "e"), ("mode", "模式", 120, "center"),
                             ("san", "改名", 52, "center")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=anc,
                             stretch=(c == "name"))
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)

        side = ttk.Frame(mid)
        side.pack(side="left", fill="y", padx=(8, 0))
        for txt, cmd in (("+ 添加", self._add), ("− 移除", self._remove),
                         ("排除规则", self._edit_excludes)):
            ttk.Button(side, text=txt, width=10, command=cmd).pack(pady=2)
        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)
        ttk.Button(side, text="还原改名", width=10,
                   command=self._restore_names).pack(pady=2)

        prog = ttk.LabelFrame(self.root, text="进度")
        prog.pack(fill="x", **pad)
        self.bar = ttk.Progressbar(prog, maximum=1000)
        self.bar.pack(fill="x", padx=10, pady=(10, 2))
        self.lbl_total = ttk.Label(prog, text="就绪")
        self.lbl_total.pack(anchor="w", padx=10)
        self.lbl_file = ttk.Label(prog, text="", foreground="#666")
        self.lbl_file.pack(anchor="w", padx=10, pady=(0, 10))

        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log = ScrolledText(logf, height=8, wrap="none", font=("Menlo", 11))
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        self.log.configure(state="disabled")

        bot = ttk.Frame(self.root)
        bot.pack(fill="x", padx=12, pady=(0, 12))
        self.v_auto = tk.BooleanVar(value=self.cfg["auto_on_mount"])
        self.v_eject = tk.BooleanVar(value=self.cfg["eject_when_done"])
        ttk.Checkbutton(bot, text="插盘自动备份", variable=self.v_auto,
                        command=self._save_opts).pack(side="left")
        ttk.Checkbutton(bot, text="完成后推出 U 盘", variable=self.v_eject,
                        command=self._save_opts).pack(side="left", padx=(12, 0))
        self.btn_cancel = ttk.Button(bot, text="取消", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="right")
        self.btn_run = ttk.Button(bot, text="开始备份", command=lambda: self._go(False))
        self.btn_run.pack(side="right", padx=6)
        self.btn_plan = ttk.Button(bot, text="预览改动", command=lambda: self._go(True))
        self.btn_plan.pack(side="right")
        self.btn_wx = ttk.Button(bot, text="备份微信", command=self._go_wechat)
        self.btn_wx.pack(side="right", padx=(0, 8))

    # ---- 目标盘 ----
    def _refresh_target(self):
        self.mount = target_mount(self.cfg["volume_uuid"])
        if self.mount:
            self.status.config(
                text=f"目标：{self.cfg['volume_name']} (exFAT)   ● 已连接   "
                     f"剩余 {human(free_bytes(self.mount))}", foreground="#1e8449")
        else:
            self.status.config(text=f"目标：{self.cfg['volume_name']}   ○ 未连接",
                               foreground="#c0392b")
        if self.rsync and not self.busy:
            state = "!disabled" if self.mount else "disabled"
            for b in (self.btn_run, self.btn_plan, self.btn_wx):
                b.state([state])

    def _measure_sizes(self):
        for f in self.cfg["folders"]:
            p = Path(f["path"])
            n = 0
            if p.is_dir():
                for dp, _, fns in os.walk(p):
                    for x in fns:
                        try:
                            n += (Path(dp) / x).stat().st_size
                        except OSError:
                            pass
            self.q.put(("size", {"path": f["path"], "text": human(n) if p.is_dir() else "不存在"}))

    # ---- 列表 ----
    def _reload_tree(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for f in self.cfg["folders"]:
            self.tree.insert("", "end", iid=f["path"], values=(
                "☑" if f["enabled"] else "☐", f["path"],
                self.sizes.get(f["path"], "…"), MODES[f["mode"]],
                "☑" if f.get("sanitize", True) else "☐"))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def _on_click(self, ev):
        if self.busy:
            return
        row = self.tree.identify_row(ev.y)
        col = self.tree.identify_column(ev.x)
        if not row:
            return
        f = next((x for x in self.cfg["folders"] if x["path"] == row), None)
        if not f:
            return
        if col == "#1":
            f["enabled"] = not f["enabled"]
            save_config(self.cfg)
            self._reload_tree()
        elif col == "#4":
            m = tk.Menu(self.root, tearoff=0)
            for key in MODE_ORDER:
                m.add_radiobutton(label=MODES[key], value=key,
                                  variable=tk.StringVar(value=f["mode"]),
                                  command=lambda k=key, ff=f: self._set_mode(ff, k))
            m.tk_popup(ev.x_root, ev.y_root)
        elif col == "#5":
            f["sanitize"] = not f.get("sanitize", True)
            save_config(self.cfg)
            self._reload_tree()
            if not f["sanitize"]:
                messagebox.showinfo(
                    APP_NAME,
                    f"「{Path(f['path']).name}」关闭了改名。\n\n"
                    "U 盘上的文件名会和本机完全一致，将来整个拷回原位就能直接用。\n"
                    "代价是名字里带 : | \\ ? * \" < > 的那些文件，在 Windows 上打不开。\n\n"
                    "下次备份时 U 盘上旧的全角文件名会被自动清掉换成原名。")

    def _set_mode(self, f, key):
        f["mode"] = key
        save_config(self.cfg)
        self._reload_tree()

    def _add(self):
        d = filedialog.askdirectory(title="选择要备份的文件夹")
        if not d:
            return
        d = os.path.normpath(d)
        if any(f["path"] == d for f in self.cfg["folders"]):
            return messagebox.showinfo(APP_NAME, "这个文件夹已经在列表里了。")
        if any(Path(d).name == Path(f["path"]).name for f in self.cfg["folders"]):
            return messagebox.showerror(
                APP_NAME, f"U 盘上已有同名目标文件夹「{Path(d).name}」，请先重命名以免覆盖。")
        self.cfg["folders"].append({"path": d, "enabled": True, "mode": "mirror"})
        save_config(self.cfg)
        self._reload_tree()
        threading.Thread(target=self._measure_sizes, daemon=True).start()

    def _remove(self):
        sel = self.tree.selection()
        if not sel:
            return
        if not messagebox.askyesno(APP_NAME, f"从备份清单移除：\n{sel[0]}\n\n"
                                             "（只是不再备份，U 盘上已有的副本不会动）"):
            return
        self.cfg["folders"] = [f for f in self.cfg["folders"] if f["path"] != sel[0]]
        save_config(self.cfg)
        self._reload_tree()

    def _edit_excludes(self):
        win = tk.Toplevel(self.root)
        win.title("排除规则")
        win.geometry("460x340")
        ttk.Label(win, text="每行一条 rsync 排除模式：", padding=8).pack(anchor="w")
        txt = tk.Text(win, font=("Menlo", 11))
        txt.pack(fill="both", expand=True, padx=10)
        txt.insert("1.0", "\n".join(self.cfg["excludes"]))

        def ok():
            self.cfg["excludes"] = [l.strip() for l in txt.get("1.0", "end").splitlines()
                                    if l.strip()]
            save_config(self.cfg)
            win.destroy()
        bar = ttk.Frame(win)
        bar.pack(fill="x", pady=10, padx=10)
        ttk.Button(bar, text="保存", command=ok).pack(side="right")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=6)

    def _save_opts(self):
        self.cfg["auto_on_mount"] = self.v_auto.get()
        self.cfg["eject_when_done"] = self.v_eject.get()
        save_config(self.cfg)

    # ---- 倒计时 ----
    def _start_countdown(self, n=None):
        n = self.cfg["auto_countdown"] if n is None else n
        if not self.v_auto.get():
            return
        if n <= 0:
            self.countdown_job = None
            return self._go(False)
        self.lbl_total.config(text=f"插盘自动备份将在 {n} 秒后开始 —— 点「取消」可中止")
        self.btn_cancel.state(["!disabled"])
        self.countdown_job = self.root.after(1000, lambda: self._start_countdown(n - 1))

    def _stop_countdown(self):
        if self.countdown_job:
            self.root.after_cancel(self.countdown_job)
            self.countdown_job = None
            self.lbl_total.config(text="已取消自动备份")
            self.btn_cancel.state(["disabled"])
            return True
        return False

    # ---- 运行 ----
    def _go(self, dry_only: bool):
        self._stop_countdown()
        if self.busy:
            return
        self._refresh_target()
        if not self.mount:
            return messagebox.showerror(APP_NAME, "U 盘没有连接。")
        folders = [f for f in self.cfg["folders"] if f["enabled"]]
        if not folders:
            return messagebox.showinfo(APP_NAME, "没有勾选任何文件夹。")
        self.busy = True
        for b in (self.btn_run, self.btn_plan, self.btn_wx):
            b.state(["disabled"])
        self.btn_cancel.state(["!disabled"])
        self._clear_log()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.logfile = open(LOG_DIR / f"backup-{datetime.now():%Y%m%d-%H%M%S}.log",
                            "w", encoding="utf-8")
        self.engine = Engine(self.cfg, self.rsync, lambda k, **kw: self.q.put((k, kw)))
        self.worker = threading.Thread(target=self._work, args=(folders, dry_only),
                                       daemon=True)
        self.worker.start()

    def _work(self, folders, dry_only):
        put = lambda k, **kw: self.q.put((k, kw))
        log = lambda s: put("log", text=s)
        t0 = time.time()
        try:
            plan = self.engine.plan(folders, self.mount)
            put("plan", plan=plan, dry_only=dry_only)
            if dry_only:
                return
            need = free_bytes(self.mount)
            if plan.total_bytes > need:
                raise RuntimeError(f"U 盘空间不足：需要 {human(plan.total_bytes)}，"
                                   f"只剩 {human(need)}")
            stats = self.engine.run(plan, self.mount, log)
            put("done", stats=stats, plan=plan, secs=time.time() - t0)
        except Cancelled:
            put("cancelled")
        except Exception as e:
            put("fail", text=str(e))

    # ---- 微信 ----
    def _go_wechat(self):
        self._stop_countdown()
        if self.busy:
            return
        self._refresh_target()
        if not self.mount:
            return messagebox.showerror(APP_NAME, "U 盘没有连接。")

        wc = dict(self.cfg["wechat"])
        app = wc.get("app") or "/Applications/WeChat.app"
        items = [it for it in wc.get("items", []) if it.get("path")]
        if not items:
            return messagebox.showerror(APP_NAME, "配置里 wechat.items 是空的。")
        missing = [it["path"] for it in items if not Path(it["path"]).is_dir()]
        if missing:
            return messagebox.showerror(
                APP_NAME, "找不到这些微信数据目录：\n" + "\n".join(missing) +
                          "\n\n微信换过大版本的话，在配置文件里改 wechat.items。")
        running = bool(wechat_pids(app))
        if running and not messagebox.askyesno(
                APP_NAME,
                "微信正在运行。\n\n聊天记录数据库必须在微信完全退出之后才能备份，"
                "否则备份出来的可能是半截的。\n\n现在关闭微信吗？"
                "（备份完成后会自动重新打开）",
                icon="question"):
            self._clear_log()
            self._append("已取消 —— 微信没有退出，没有做任何备份")
            self.lbl_total.config(text="已取消：微信仍在运行")
            return

        self.busy = True
        for b in (self.btn_run, self.btn_plan, self.btn_wx):
            b.state(["disabled"])
        self.btn_cancel.state(["!disabled"])
        self._clear_log()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.logfile = open(LOG_DIR / f"wechat-{datetime.now():%Y%m%d-%H%M%S}.log",
                            "w", encoding="utf-8")
        self.engine = Engine(self.cfg, self.rsync, lambda k, **kw: self.q.put((k, kw)))
        self.worker = threading.Thread(target=self._work_wechat, args=(wc, items),
                                       daemon=True)
        self.worker.start()

    def _work_wechat(self, wc, items):
        put = lambda k, **kw: self.q.put((k, kw))
        log = lambda t: put("log", text=t)
        t0 = time.time()
        app = wc.get("app") or "/Applications/WeChat.app"
        relaunch = False
        result = ("fail", {"text": "未知错误"})
        try:
            put("phase", text="正在检查微信数据目录 …")
            for it in items:        # 沙盒容器第一次访问可能要卡十几秒, 所以放在这个线程里
                try:
                    next(Path(it["path"]).iterdir(), None)
                except PermissionError:
                    raise PermissionError(
                        "读不到微信的数据目录：\n" + it["path"] +
                        "\n\n微信的数据放在沙盒容器里，macOS 要求「完全磁盘访问权限」才能读取。"
                        f"\n\n请在\n系统设置 → 隐私与安全性 → 完全磁盘访问权限\n"
                        f"里把「{APP_NAME}」加进去并打开，然后退出本程序重新打开再试。")
            if wechat_pids(app):
                put("phase", text="正在退出微信 …")
                log("正在退出微信 …")
                relaunch = bool(wc.get("relaunch", True))   # 先立 flag: 中途失败也要给人开回来
                if not quit_wechat(app, log, self.engine._check,
                                   int(wc.get("quit_timeout", 45))):
                    raise RuntimeError("微信没能在超时前退出（可能有对话框挡着），已中止备份。"
                                       "请手动退出微信后再点一次。")
                log("微信已完全退出")
                put("phase", text="等微信把数据落盘 …")
                time.sleep(2)
            else:
                log("微信没有在运行，直接开始")

            folders = [{"path": it["path"],
                        "dest": it.get("dest") or Path(it["path"]).name,
                        "sanitize": it.get("sanitize", False),
                        "mode": it.get("mode") or wc.get("mode") or "mirror",
                        "enabled": True,
                        "max_delete": it.get("max_delete") or wc.get("max_delete")}
                       for it in items]
            plan = self.engine.plan(folders, self.mount)
            put("plan", plan=plan, dry_only=False)
            need = free_bytes(self.mount)
            if plan.total_bytes > need:
                raise RuntimeError(f"U 盘空间不足：需要 {human(plan.total_bytes)}，"
                                   f"只剩 {human(need)}")
            stats = self.engine.run(plan, self.mount, log)
            self._wechat_readme(folders, log)
            result = ("done", {"stats": stats, "plan": plan, "secs": time.time() - t0})
        except Cancelled:
            result = ("cancelled", {})
        except PermissionError as e:
            result = ("fail", {"text": str(e), "open_privacy": True})
        except Exception as e:
            result = ("fail", {"text": str(e)})
        finally:
            if relaunch:
                put("phase", text="正在重新打开微信 …")
                launch_wechat(app)
                log("已重新打开微信")
        put(result[0], **result[1])

    # ---- 还原改名 ----
    def _find_manifest(self, folder: Path) -> Path | None:
        """映射表可能在拷回来的文件夹旁边, 也可能还在 U 盘上。"""
        stem = f"名称映射-{folder.name}.json"
        cands = [folder.parent / "_备份信息" / stem, folder / "_备份信息" / stem]
        if self.mount:
            cands.append(self.mount / self.cfg["dest_base"] / "_备份信息" / stem)
        for c in cands:
            if c.is_file():
                return c
        return None

    def _restore_names(self):
        if self.busy:
            return
        d = filedialog.askdirectory(title="选择要还原名字的文件夹（拷回本机的那份）")
        if not d:
            return
        folder = Path(d)
        man = self._find_manifest(folder)
        if man is None:
            self._refresh_target()
            man = self._find_manifest(folder)
        if man is None:
            f = filedialog.askopenfilename(
                title=f"没找到「{folder.name}」的映射表，请手动指定 名称映射-*.json",
                filetypes=[("映射表", "*.json"), ("所有文件", "*")])
            if not f:
                return
            man = Path(f)
        try:
            data = json.loads(man.read_text("utf-8"))
        except Exception as e:
            return messagebox.showerror(APP_NAME, f"映射表读不了：{e}")
        if not data.get("sanitize", True):
            return messagebox.showinfo(
                APP_NAME, f"「{data.get('folder', folder.name)}」备份时就没有改名，"
                          "\n U 盘上的名字和本机一致，直接用就行，不需要还原。")
        entries = [e for e in data.get("entries", []) if e.get("mapped") != e.get("orig")]
        if not entries:
            return messagebox.showinfo(APP_NAME, "这份映射表里没有需要改回的条目。")
        if not messagebox.askyesno(
                APP_NAME,
                f"把这个文件夹里 {len(entries)} 个条目的名字改回原样？\n\n"
                f"文件夹：{folder}\n映射表：{man.name}\n\n"
                "只改名，不动内容；目标名已存在的会跳过。",
                icon="question"):
            return
        self._clear_log()
        self._append(f"── 还原改名：{folder} ──")
        st = restore_names(folder, entries, self._append)
        msg = (f"改回 {st['done']} 个\n"
               f"本来就是原名 {st['already']} 个\n"
               f"找不到 {st['missing']} 个\n"
               f"目标已存在跳过 {st['conflict']} 个")
        if st["errors"]:
            msg += f"\n失败 {len(st['errors'])} 个（详见日志）"
        self._append("── " + msg.replace(chr(10), " · ") + " ──")
        self.lbl_total.config(text=f"还原改名完成：改回 {st['done']} 个")
        (messagebox.showwarning if st["errors"] else messagebox.showinfo)(APP_NAME, msg)

    def _wechat_readme(self, folders, log):
        """在 U 盘上留一张条子, 免得以后忘了这两个文件夹该放回哪儿。"""
        try:
            d = self.mount / self.cfg["dest_base"] / "_备份信息"
            d.mkdir(parents=True, exist_ok=True)
            lines = [f"微信数据备份  (由 {APP_NAME} v{VERSION} 生成)",
                     f"备份时间: {datetime.now():%Y-%m-%d %H:%M:%S}", "",
                     "还原时把下面左边的文件夹整个拷回右边的位置（先退出微信）:", ""]
            for f in folders:
                lines.append(f"  {self.cfg['dest_base']}/{f['dest']}")
                lines.append(f"      ->  {f['path']}")
            lines += ["", "注意: exFAT 不支持硬链接，微信原本共享的文件在 U 盘上",
                      "存了两份，拷回去之后占的空间会比原来大一些。"]
            (d / "微信还原说明.txt").write_text("\n".join(lines), "utf-8")
        except Exception as e:
            log(f"  ⚠️  写还原说明失败: {e}")

    def _cancel(self):
        if self._stop_countdown():
            return
        if self.engine:
            self.engine.cancel()
        self.btn_cancel.state(["disabled"])
        self.lbl_total.config(text="正在取消 …")

    def _finish(self):
        self.busy = False
        self.btn_cancel.state(["disabled"])
        for b in (self.btn_run, self.btn_plan, self.btn_wx):
            b.state(["!disabled"])
        if self.logfile:
            self.logfile.close()
            self.logfile = None
        self._refresh_target()

    # ---- 日志 ----
    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        if self.logfile:
            self.logfile.write(text + "\n")
            self.logfile.flush()

    # ---- 消息泵 ----
    def _pump(self):
        try:
            while True:
                kind, d = self.q.get_nowait()
                getattr(self, f"_ev_{kind}", lambda **_: None)(**d)
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _ev_size(self, path, text):
        self.sizes[path] = text
        if self.tree.exists(path):
            self.tree.set(path, "size", text)

    def _ev_phase(self, text):
        self.lbl_total.config(text=text)

    def _ev_log(self, text):
        self._append(text)

    def _ev_progress(self, done, total, name, note):
        self.bar["value"] = min(1000, done * 1000 / max(total, 1))
        pct = done * 100 / max(total, 1)
        self.lbl_total.config(text=f"{pct:.0f}%   {human(done)} / {human(total)}"
                                   + (f"   {note}" if note else ""))
        if name:
            self.lbl_file.config(text=name[:110])

    def _ev_plan(self, plan, dry_only):
        self._append(f"── 预演结果 ──")
        for fp in plan.folders:
            if fp.error:
                self._append(f"  {fp.name}: ❌ {fp.error}")
                continue
            self._append(f"  {fp.name}: 新增 {fp.adds} · 更新 {fp.updates} · "
                         f"删除 {len(fp.deletes)} · 改名 {len(fp.special)} · "
                         f"共 {human(fp.total_bytes)}")
        for folder, w in plan.warns:
            self._append(f"  ⚠️  路径过长(Windows 可能打不开): {folder}/{w}")
        if dry_only:
            self._show_plan_dialog(plan)
            self._finish()

    def _show_plan_dialog(self, plan):
        dels = [(fp.name, d) for fp in plan.folders for d in fp.deletes]
        msg = (f"将传输 {plan.total_files} 个文件，共 {human(plan.total_bytes)}\n"
               f"将删除 {plan.total_deletes} 个文件\n"
               f"需改名 {plan.mapped} 个文件\n"
               f"路径过长警告 {len(plan.warns)} 个")
        if dels:
            msg += "\n\n将被删除：\n" + "\n".join(f"  {a}/{b}" for a, b in dels[:15])
            if len(dels) > 15:
                msg += f"\n  … 其余 {len(dels)-15} 个（完整清单见日志）"
            for a, b in dels:
                self._append(f"  删除 {a}/{b}")
        messagebox.showinfo(f"{APP_NAME} — 预览改动", msg)

    def _ev_done(self, stats, plan, secs):
        self.bar["value"] = 1000
        errs = stats["errors"]
        self.lbl_total.config(
            text=f"✅ 完成：{stats['files']} 个文件 · 删除 {stats['deleted']} · "
                 f"改名 {stats['mapped']} · 清理 {stats['junk']} · 用时 {secs:.0f} 秒"
                 + (f" · {len(errs)} 个错误" if errs else ""))
        self.lbl_file.config(text="")
        self._append(f"── 完成，用时 {secs:.0f} 秒 ──")
        self._finish()
        if errs:
            messagebox.showwarning(APP_NAME, "备份完成，但有 %d 处问题：\n\n%s"
                                   % (len(errs), "\n".join(errs[:10])))
        elif self.v_eject.get():
            r = subprocess.run(["diskutil", "eject", self.cfg["volume_uuid"]],
                               capture_output=True, text=True)
            self._append("已推出 U 盘" if r.returncode == 0 else f"推出失败: {r.stderr.strip()}")
            self._refresh_target()
        if self.auto and not errs:          # 插盘自动跑完就别赖在屏幕上了
            self.lbl_file.config(text="3 秒后自动关闭")
            self.root.after(3000, self.root.destroy)

    def _ev_cancelled(self):
        self.lbl_total.config(text="已取消")
        self._append("── 已取消 ──")
        self._finish()

    def _ev_fail(self, text, open_privacy=False):
        self.lbl_total.config(text="失败")
        self._append(f"❌ {text}")
        self._finish()
        messagebox.showerror(APP_NAME, text)
        if open_privacy:
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference."
                            "security?Privacy_AllFiles"], capture_output=True)


def probe_wechat() -> int:
    """诊断用: 以 app 自己的身份试读微信容器, 结果写到 ~/Library/Logs/usbmirror/probe.txt。
    (完全磁盘访问权限是按可执行文件的签名身份授予的, 从终端里试出来的结果不算数。)"""
    cfg = load_config()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for it in cfg["wechat"].get("items", []):
        src = Path(it["path"])
        try:
            names = sorted(x.name for x in src.iterdir())
            rows.append(f"OK   {src}\n     读到 {len(names)} 项: {names[:6]}")
        except Exception as e:
            rows.append(f"FAIL {src}\n     {type(e).__name__}: {e}")
    (LOG_DIR / "probe.txt").write_text("\n".join(rows) + "\n", "utf-8")
    return 0


def main():
    if "--probe-wechat" in sys.argv:
        return probe_wechat()
    auto = "--auto" in sys.argv
    cfg = load_config()
    if auto:
        # launchd 的 StartOnMount 对任何卷挂载都会触发, 不是目标盘就安静退出
        if not cfg.get("auto_on_mount") or not target_mount(cfg["volume_uuid"]):
            return
    root = tk.Tk()
    App(root, auto)
    root.mainloop()


if __name__ == "__main__":
    main()
