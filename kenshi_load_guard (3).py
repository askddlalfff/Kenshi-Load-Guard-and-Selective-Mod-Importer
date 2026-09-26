"""Kenshi Load Guard - Windows launcher / on-demand loading aid.

The optional light-load preset temporarily changes settings.cfg, with a backup.
It never edits mods or saves. Requires Python 3.10+ on Windows.
"""

from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser


TITLE = "Kenshi 载入保护器"
KCF_URL = "https://www.nexusmods.com/kenshi/mods/506"
KERNEL = None
STILL_ACTIVE = 259
WAIT_TIMEOUT = 258
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_INFORMATION = 0x0400
SYNCHRONIZE = 0x00100000
CREATE_SUSPENDED = 0x00000004
CAPTURE_NAMES = ("kenshi.log", "kenshi_info.log", "kenshi.txt", "kenshi_info.txt", "_kenshi_fix1.log")
SKELETON_MISSING = re.compile(
    r"(?:Skeleton file\s+(.+?\.skeleton)\s+not found|"
    r"Failed to load skeleton file:\s*(.+?\.skeleton))", re.IGNORECASE)
MESH_MISSING = re.compile(r"Loaded mesh doesn't exist:\s*(.+?\.mesh)\b", re.IGNORECASE)
LIGHT_LIMITS = {
    "Global population multiplier": 1.0,
    "Squad size multiplier": 1.0,
    "raidSizeMult": 1.0,
    "npc range": 3000.0,
    "objects view range": 3000.0,
    "view distance": 4500.0,
}


def profile_backup(folder: Path) -> Path:
    normalized = str(folder.resolve()).rstrip("\\/").casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KenshiLoadGuard" / "backups" / (digest + ".settings.cfg")


def _setting_pattern(key: str) -> re.Pattern:
    return re.compile(r"^([ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*)([^\r\n]*)", re.I | re.M)


def _replace_settings(path: Path, content: str) -> None:
    temp = path.with_name(path.name + ".kenshi_guard.tmp")
    try:
        temp.write_bytes(content.encode("utf-8"))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def apply_light_profile(folder: Path) -> str:
    """Reduce only values above the limits; preserve an exact pre-change backup."""
    file = folder / "settings.cfg"
    if not file.is_file():
        raise ValueError("游戏根目录缺少 settings.cfg；未修改设置")
    backup = profile_backup(folder)
    if backup.exists():
        raise ValueError("发现待恢复的轻载配置；请先关闭游戏并点‘恢复原设置’")
    original = file.read_bytes()
    try:
        content = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("settings.cfg 不是 UTF-8 文本，未修改") from exc
    if "\x00" in content:
        raise ValueError("settings.cfg 格式不正确，未修改")
    changed = 0
    for key, target in LIGHT_LIMITS.items():
        def lower(match: re.Match) -> str:
            nonlocal changed
            try:
                value = float(match.group(2).strip())
            except ValueError:
                return match.group(0)
            if not math.isfinite(value) or value <= target:
                return match.group(0)
            changed += 1
            return match.group(1) + f"{target:g}"
        content = _setting_pattern(key).sub(lower, content, count=1)
    if not changed:
        return "当前设置不高于预设，或未找到可调整项；未修改 settings.cfg"
    backup.parent.mkdir(parents=True, exist_ok=True)
    temporary = backup.with_suffix(backup.suffix + ".tmp")
    try:
        temporary.write_bytes(original)
        os.replace(temporary, backup)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        _replace_settings(file, content)
    except OSError as exc:
        raise OSError("应用设置失败；原配置已备份，游戏退出后请点‘恢复原设置’") from exc
    return f"已临时调低 {changed} 项设置；原值已备份"


def restore_light_profile(folder: Path) -> str:
    """Restore our keys without overwriting later changes made by the user/game."""
    backup = profile_backup(folder)
    if not backup.is_file():
        return "没有待恢复的轻载配置"
    file = folder / "settings.cfg"
    original = backup.read_bytes()
    if not file.exists():
        file.write_bytes(original)
        backup.unlink()
        return "已从备份恢复 settings.cfg"
    try:
        current = file.read_bytes().decode("utf-8")
        previous = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("设置文件编码已变化；备份仍保留，请手动检查") from exc
    restored = conflicts = 0
    for key, target in LIGHT_LIMITS.items():
        pattern = _setting_pattern(key)
        old = pattern.search(previous)
        if not old:
            continue
        original_value = old.group(2)
        try:
            if float(original_value.strip()) <= target:
                continue  # This key was never changed by the profile.
        except ValueError:
            continue
        if not pattern.search(current):
            conflicts += 1
            continue
        def restore(match: re.Match) -> str:
            nonlocal restored, conflicts
            value = match.group(2).strip()
            if value == original_value.strip():
                return match.group(0)
            try:
                applied = math.isclose(float(value), target, rel_tol=0, abs_tol=1e-6)
            except ValueError:
                applied = False
            if applied:
                restored += 1
                return match.group(1) + original_value
            conflicts += 1
            return match.group(0)
        current = pattern.sub(restore, current, count=1)
    if restored:
        _replace_settings(file, current)
    if not conflicts:
        backup.unlink()
    return (f"已恢复 {restored} 项；" +
            ("原始备份已清理" if not conflicts else
             f"{conflicts} 项被再次修改，未覆盖；备份保留于 {backup}"))


def load_settings() -> dict:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KenshiLoadGuard"
    try:
        return json.loads((base / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KenshiLoadGuard"
    base.mkdir(parents=True, exist_ok=True)
    destination = base / "settings.json"
    temporary = base / "settings.json.tmp"
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)


def detect_game_folder() -> Path | None:
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        root = os.environ.get(variable)
        if not root:
            continue
        candidate = Path(root) / "Steam" / "steamapps" / "common" / "Kenshi"
        if (candidate / "kenshi_x64.exe").is_file():
            return candidate
    return None


def find_logs(folder: Path) -> list[Path]:
    paths = []
    for name in CAPTURE_NAMES:
        file = folder / name
        if file.is_file():
            paths.append(file)
    return paths


def file_state(file: Path) -> tuple[int, int] | None:
    try:
        stat = file.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def capture_baseline(folder: Path) -> dict[str, tuple[int, int] | None]:
    return {name: file_state(folder / name) for name in CAPTURE_NAMES}


def read_log_tail(file: Path, max_bytes: int = 2_000_000) -> tuple[list[str], bool]:
    with file.open("rb") as reader:
        reader.seek(0, 2)
        size = reader.tell()
        reader.seek(max(0, size - max_bytes))
        raw = reader.read()
    if size > max_bytes and b"\n" in raw:
        raw = raw.split(b"\n", 1)[1]  # Discard a partial first line.
    return raw.decode("utf-8-sig", errors="replace").splitlines(), size > max_bytes


def asset_search_roots(folder: Path) -> list[Path]:
    roots = [folder / "mods", folder / "data"]
    if folder.parent.name.lower() == "common" and folder.parent.parent.name.lower() == "steamapps":
        roots.append(folder.parent.parent / "workshop" / "content" / "233860")
    return [root for root in roots if root.is_dir()]


def locate_assets(folder: Path, names: list[str], budget_seconds: float = 15.0) -> list[str]:
    """Search only filenames; no mod file or save file is opened or changed."""
    wanted = {Path(name.replace("\\", "/")).name.casefold() for name in names[:12]}
    wanted.discard("")
    if not wanted:
        return ["日志没有提供可搜索的缺失骨骼或模型文件名。"]
    found: dict[str, list[str]] = {name: [] for name in wanted}
    roots = asset_search_roots(folder)
    deadline = time.monotonic() + budget_seconds
    timed_out = False
    for root in roots:
        for directory, subdirs, files in os.walk(root):
            subdirs.sort()
            if time.monotonic() >= deadline:
                timed_out = True
                break
            for name in files:
                key = name.casefold()
                if key in found and len(found[key]) < 8:
                    found[key].append(str(Path(directory) / name))
        if timed_out:
            break
    lines = ["搜索范围：" + ("；".join(str(root) for root in roots) if roots else "没有找到模组/数据目录")]
    for name in sorted(wanted):
        matches = found[name]
        lines.append(name + ("：" + "；".join(matches) if matches else "：搜索范围内未找到"))
    if timed_out:
        lines.append("搜索超过 15 秒已停止；‘未找到’不代表完整磁盘不存在该文件。")
    return lines


def build_capture_report(folder: Path, started_at: datetime, baseline: dict,
                         pid: int | None, exit_code: int | None, mode: str) -> str:
    """Make one pasteable, time-scoped, read-only report from a game session."""
    ended_at = datetime.now().astimezone()
    lines = ["Kenshi 载入与闪退捕获报告", "=" * 35,
             "记录开始：" + started_at.isoformat(timespec="seconds"),
             "报告生成：" + ended_at.isoformat(timespec="seconds"),
             "启动方式：" + mode,
             "进程 PID：" + (str(pid) if pid is not None else "未记录"),
             "进程退出码：" + ("未获取（手动生成的报告不代表游戏已退出）" if exit_code is None
                       else f"0x{exit_code & 0xffffffff:08X} ({exit_code})"),
             "游戏目录：" + str(folder),
             "说明：退出码和日志只是线索；报告不自动判定真正的闪退原因。", ""]

    lines.append("【启用模组，保持 mods.cfg 原有顺序】")
    cfg = folder / "data" / "mods.cfg"
    try:
        names = cfg.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        active = [name.strip() for name in names if name.strip().lower().endswith(".mod")]
        lines.append("数量：" + str(len(active)))
        lines.extend(f"{i}. {name}" for i, name in enumerate(active, 1))
        if not active:
            lines.append("未读取到启用的模组名称。")
    except OSError as exc:
        lines.append("无法读取：" + str(exc))
    lines.extend(["", "【动画崩溃补丁】",
                  "_kenshi_fix1.asi：" + ("发现" if (folder / "_kenshi_fix1.asi").is_file() else "未在游戏根目录发现"),
                  ""])

    missing_names: list[str] = []
    for name in CAPTURE_NAMES:
        file = folder / name
        lines.append("【" + name + "】")
        before, after = baseline.get(name), file_state(file)
        lines.append("本次文件状态：" + ("已新建或更新" if after and before != after else
                                 "手动报告：文件在捕获前已存在，请按时间判断" if after and mode.startswith("手动捕获") else
                                 "未更新（以下可能是旧会话内容）" if after else "未找到"))
        if not after:
            lines.append("")
            continue
        try:
            content, truncated = read_log_tail(file)
        except OSError as exc:
            lines.extend(["读取失败：" + str(exc), ""])
            continue
        if truncated:
            lines.append("文件较大，仅检查末尾约 2 MB。")
        matches = [(i, match.group(1) or match.group(2)) for i, line in enumerate(content)
                   for match in SKELETON_MISSING.finditer(line)]
        for line in content:
            for match in MESH_MISSING.finditer(line):
                missing_names.append(match.group(1).replace("\\", "/").split("/")[-1])
        for _, asset in matches:
            if asset.casefold() not in [item.casefold() for item in missing_names]:
                missing_names.append(asset)
        if matches:
            lines.append("缺失骨骼文件及次数：")
            for asset in missing_names[-12:]:
                count = sum(value.casefold() == asset.casefold() for _, value in matches)
                if count:
                    lines.append(f"  {asset}：{count} 次")
            lines.append("最近缺失记录的前后文：")
            context_indices = set()
            for index, _ in matches[-5:]:
                context_indices.update(range(max(0, index - 3), min(len(content), index + 4)))
            lines.extend(f"  [{i+1}] {content[i]}" for i in sorted(context_indices))
        lines.append("日志真正的末尾（最多 180 行）：")
        lines.extend("  " + entry for entry in content[-180:])
        lines.append("")

    lines.append("【缺失骨骼及模型文件位置搜索】")
    lines.extend(locate_assets(folder, missing_names))
    lines.extend(["", "【游戏目录中的崩溃压缩包】"])
    dumps = list(folder.glob("*rash*ump*.zip"))
    dumps.sort(key=lambda file: file_state(file)[1] if file_state(file) else -1, reverse=True)
    if not dumps:
        lines.append("未在游戏目录发现 crashDump ZIP。")
    for dump in dumps[:8]:
        try:
            stat = dump.stat()
            stamp = datetime.fromtimestamp(stat.st_mtime).astimezone()
            lines.append(f"{dump.name}｜修改于 {stamp.isoformat(timespec='seconds')}｜{stat.st_size} 字节"
                         + ("｜本次会话期间生成/更新" if stat.st_mtime >= started_at.timestamp() - 3 else
                            "｜手动捕获前已生成，请结合日志时间判断" if mode.startswith("手动捕获") else
                            "｜早于本次会话"))
        except OSError as exc:
            lines.append(f"{dump.name}：{exc}")
    lines.extend(["", "请把这份文本完整发送；缺失文件即使在日志末尾出现，也还需结合退出码和崩溃现场判断。"])
    return "\n".join(lines) + "\n"


def save_capture_report(folder: Path, started_at: datetime, baseline: dict,
                        pid: int | None, exit_code: int | None, mode: str) -> tuple[Path, str]:
    report = build_capture_report(folder, started_at, baseline, pid, exit_code, mode)
    target = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KenshiLoadGuard" / "reports"
    target.mkdir(parents=True, exist_ok=True)
    file = target / ("Kenshi_捕获报告_" + started_at.strftime("%Y%m%d_%H%M%S") + f"_{pid or 0}.txt")
    file.write_text(report, encoding="utf-8-sig")
    return file, report


def diagnostic(folder: Path) -> str:
    logs = find_logs(folder)
    lines = []
    patch = (folder / "_kenshi_fix1.asi").is_file()
    generated = list((folder / "mods").glob("*KCF*")) if (folder / "mods").is_dir() else []
    lines.append("KCF 动画崩溃补丁：" + ("发现 _kenshi_fix1.asi；请在游戏模组页确认自动补丁位于末尾" if patch else "未发现 _kenshi_fix1.asi"))
    if generated:
        lines.append("疑似自动补丁文件：" + ", ".join(p.name for p in generated[:4]))
    if not logs:
        lines.append("未找到日志。先运行游戏并复现问题，再点‘分析日志’。")
        return "\n".join(lines)
    for file in logs:
        try:
            with file.open("rb") as reader:
                reader.seek(0, 2)
                reader.seek(max(0, reader.tell() - 500_000))
                raw = reader.read().decode("utf-8", errors="replace")
        except OSError as exc:
            lines.append(f"{file.name}: 无法读取（{exc}）")
            continue
        hits = []
        for entry in raw.splitlines():
            lower = entry.lower()
            if any(word in lower for word in (
                "skeleton", "animation", "out of memory", "bad allocation", "std::bad_alloc",
                "resource not found", "cannot locate", "could not load", "ogre exception",
                "physx", "access violation", "device removed", "hair attachment",
                "loaded mesh doesn't exist", "modified by",
            )) and any(word in lower for word in (
                "error", "exception", "failed", "cannot", "could not", "not found", "missing",
                "crash", "memory", "violation", "device removed", "does not exist",
            )):
                hits.append(entry.strip()[:240])
        lines.append(f"{file.name}：" + ("发现可能相关的记录" if hits else "近期日志未发现明确的相关报错"))
        lines.extend("  · " + hit for hit in hits[-8:])
    lines.append("提示：日志关键词只是线索，不能凭出现次数证明‘同时播放动画’是根因。")
    return "\n".join(lines)


def enabled_mod_count(folder: Path) -> str:
    cfg = folder / "data" / "mods.cfg"
    try:
        names = [line.strip() for line in cfg.read_text(encoding="utf-8-sig", errors="replace").splitlines()]
    except OSError:
        return "模组列表不可读"
    count = sum(name.lower().endswith(".mod") for name in names)
    return f"当前启用 {count} 个模组（不会修改它们）"


def kernel32():
    global KERNEL
    if KERNEL is None:
        if os.name != "nt":
            raise OSError("此功能仅支持 Windows")
        KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
        KERNEL.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        KERNEL.OpenProcess.restype = wintypes.HANDLE
        KERNEL.CloseHandle.argtypes = [wintypes.HANDLE]
        KERNEL.CloseHandle.restype = wintypes.BOOL
        KERNEL.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        KERNEL.GetProcessAffinityMask.restype = wintypes.BOOL
        KERNEL.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        KERNEL.SetProcessAffinityMask.restype = wintypes.BOOL
        KERNEL.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        KERNEL.WaitForSingleObject.restype = wintypes.DWORD
        KERNEL.ResumeThread.argtypes = [wintypes.HANDLE]
        KERNEL.ResumeThread.restype = wintypes.DWORD
        KERNEL.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        KERNEL.TerminateProcess.restype = wintypes.BOOL
        KERNEL.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        KERNEL.GetExitCodeProcess.restype = wintypes.BOOL
        KERNEL.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                          ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.LPCWSTR,
                                          ctypes.c_void_p, ctypes.c_void_p]
        KERNEL.CreateProcessW.restype = wintypes.BOOL
    return KERNEL


def win_error(action: str) -> OSError:
    return OSError(f"{action}失败：{ctypes.WinError(ctypes.get_last_error())}")


class StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def available_cpu_bits(mask: int) -> list[int]:
    return [1 << index for index in range(ctypes.sizeof(ctypes.c_size_t) * 8) if mask & (1 << index)]


def limited_mask(original: int, count: int) -> int:
    bits = available_cpu_bits(original)
    if not bits:
        raise ValueError("进程没有可用 CPU 核心")
    return sum(bits[:max(1, min(count, len(bits)))])


class GameProcess:
    def __init__(self, handle, pid: int, original_mask: int, started_here: bool):
        self.handle = handle
        self.pid = pid
        self.original_mask = original_mask
        self.started_here = started_here
        self.limited = False

    @classmethod
    def attach(cls, pid: int) -> "GameProcess":
        api = kernel32()
        handle = api.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:
            raise win_error("连接游戏进程")
        try:
            original = cls._read_mask(handle)
            return cls(handle, pid, original, False)
        except Exception:
            api.CloseHandle(handle)
            raise

    @staticmethod
    def _read_mask(handle) -> int:
        process, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not kernel32().GetProcessAffinityMask(handle, ctypes.byref(process), ctypes.byref(system)):
            raise win_error("读取 CPU 亲和性")
        return process.value

    @classmethod
    def start_limited(cls, exe: Path, cores: int | None) -> "GameProcess":
        api = kernel32()
        startup, info = StartupInfo(), ProcessInfo()
        startup.cb = ctypes.sizeof(startup)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(exe)]))
        if not api.CreateProcessW(str(exe), command, None, None, False,
                                  CREATE_SUSPENDED, None, str(exe.parent),
                                  ctypes.byref(startup), ctypes.byref(info)):
            raise win_error("启动游戏")
        process = cls(info.hProcess, info.dwProcessId, 0, True)
        try:
            process.original_mask = cls._read_mask(info.hProcess)
            if cores is not None:
                process.limit(cores)
            if api.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise win_error("恢复游戏主线程")
            return process
        except Exception:
            # Never leave a newly created game running if its protection failed.
            try:
                api.TerminateProcess(info.hProcess, 1)
                api.WaitForSingleObject(info.hProcess, 5000)
            finally:
                process.close(restore=False)
            raise
        finally:
            api.CloseHandle(info.hThread)

    def running(self) -> bool:
        return kernel32().WaitForSingleObject(self.handle, 0) == WAIT_TIMEOUT

    def exit_code(self) -> int | None:
        code = wintypes.DWORD()
        if kernel32().GetExitCodeProcess(self.handle, ctypes.byref(code)):
            return code.value
        return None

    def limit(self, cores: int) -> None:
        if not self.running():
            raise RuntimeError("游戏进程已经退出")
        mask = limited_mask(self.original_mask, cores)
        if not kernel32().SetProcessAffinityMask(self.handle, mask):
            raise win_error("限制 CPU 核心")
        self.limited = mask != self.original_mask

    def restore(self) -> None:
        if self.running() and self.limited:
            if not kernel32().SetProcessAffinityMask(self.handle, self.original_mask):
                raise win_error("恢复 CPU 核心")
        self.limited = False

    def close(self, restore: bool = True) -> None:
        try:
            if restore:
                self.restore()
        finally:
            kernel32().CloseHandle(self.handle)


def find_running_kenshi() -> list[tuple[int, str]]:
    result = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                            text=True, errors="replace", timeout=12,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("无法读取进程列表")
    found = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 2 and row[0].lower() in {"kenshi_x64.exe", "kenshi_x32.exe"}:
            try:
                found.append((int(row[1].replace(",", "")), row[0]))
            except ValueError:
                continue
    return found


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.game: GameProcess | None = None
        self.capture: tuple[Path, datetime, dict, str] | None = None
        self.capture_queue: queue.Queue = queue.Queue()
        self.report_jobs = 0
        self.last_report: str | None = None
        self.restore_profile_when_exit = False
        self.profile_notice = ""
        self.settings = load_settings()
        root.title(TITLE + " 1.2（轻载与自动捕获版）")
        root.geometry("950x750")
        root.minsize(750, 640)
        self.folder = tk.StringVar(value=self.settings.get("folder", str(detect_game_folder() or "")))
        self.cores = tk.IntVar(value=self.settings.get("cores", 2))
        self.status = tk.StringVar(value="请选择 Kenshi 目录，然后启动游戏。")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(1200, self.poll)
        if self.folder.get():
            self.scan()

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Kenshi 载入保护器", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        ttk.Label(outer, text="轻载设置减少载入人物和可见范围；限核可降低资源处理的瞬时并行度。保留所有模组。",
                  wraplength=720).pack(anchor="w", pady=(5, 12))
        row = ttk.Frame(outer)
        row.pack(fill="x")
        ttk.Label(row, text="游戏目录：").pack(side="left")
        ttk.Entry(row, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(row, text="浏览", command=self.browse).pack(side="left")
        ttk.Button(row, text="分析日志", command=self.scan).pack(side="left", padx=(6, 0))
        config = ttk.Frame(outer)
        config.pack(fill="x", pady=14)
        ttk.Label(config, text="载入期间可用逻辑核心：").pack(side="left")
        ttk.Spinbox(config, from_=1, to=8, textvariable=self.cores, width=5).pack(side="left", padx=(5, 12))
        ttk.Label(config, text="建议先试 2；若没改善，再试 1 或 4（实验性）。").pack(side="left")
        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        ttk.Button(actions, text="一键捕获并启动", command=self.launch_capture).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="保护启动游戏", command=self.launch).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="连接已运行游戏", command=self.attach).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="开启载入保护", command=self.protect).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="恢复全部核心", command=self.restore).pack(side="left")
        profile = ttk.Frame(outer)
        profile.pack(fill="x", pady=(7, 0))
        ttk.Button(profile, text="一键轻载启动", command=self.launch_light).pack(side="left", padx=(0, 6))
        ttk.Button(profile, text="只应用轻载设置", command=self.apply_only).pack(side="left", padx=(0, 6))
        ttk.Button(profile, text="恢复原设置", command=self.restore_settings).pack(side="left")
        ttk.Label(outer, text="使用 Steam / RE_Kenshi 时：只应用轻载设置 → 按原方式启动 → 连接已运行游戏。",
                  wraplength=750).pack(anchor="w", pady=(4, 0))
        extra = ttk.Frame(outer)
        extra.pack(fill="x", pady=(5, 0))
        ttk.Button(extra, text="捕获当前状态", command=self.capture_now).pack(side="left", padx=(0, 6))
        ttk.Button(extra, text="复制上次报告", command=self.copy_report).pack(side="left")
        ttk.Label(outer, textvariable=self.status, foreground="#175585", wraplength=730).pack(anchor="w", pady=(16, 9))
        ttk.Label(outer, text="诊断用‘一键捕获并启动’（不限制核心）；游戏退出后自动保存报告并复制，直接回聊天框粘贴。",
                  wraplength=730).pack(anchor="w")
        ttk.Label(outer, text="诊断信息", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(16, 4))
        frame = ttk.Frame(outer)
        frame.pack(fill="both", expand=True)
        self.report = tk.Text(frame, wrap="word", height=15, state="disabled", font=("Consolas", 10))
        self.report.pack(side="left", fill="both", expand=True)
        ttk.Scrollbar(frame, orient="vertical", command=self.report.yview).pack(side="right", fill="y")
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Button(footer, text="查看动画崩溃补丁说明", command=lambda: webbrowser.open(KCF_URL)).pack(side="left")
        ttk.Label(outer, text="轻载会临时修改 settings.cfg 并备份；退出后尝试恢复。不能修复空指针或缺失模组引用；不修改模组和存档。",
                  wraplength=730, foreground="#923b20").pack(anchor="w", pady=(12, 0))

    def path(self) -> Path:
        path = Path(self.folder.get().strip().strip('"'))
        if not path.is_dir() or not (path / "kenshi_x64.exe").is_file():
            raise ValueError("请选择包含 kenshi_x64.exe 的 Kenshi 安装目录")
        return path

    def count(self) -> int:
        try:
            value = self.cores.get()
        except tk.TclError:
            raise ValueError("逻辑核心数必须是 1 到 8 的整数") from None
        if not 1 <= value <= 8:
            raise ValueError("逻辑核心数必须是 1 到 8 的整数")
        return value

    def browse(self) -> None:
        value = filedialog.askdirectory(title="选择 Kenshi 游戏目录")
        if value:
            self.folder.set(value)
            self.scan()

    def scan(self) -> None:
        try:
            path = self.path()
            content = (("发现待恢复的轻载配置；游戏退出后可点‘恢复原设置’。\n\n"
                        if profile_backup(path).exists() else "") +
                       enabled_mod_count(path) + "\n\n" + diagnostic(path))
            save_settings({"folder": str(path), "cores": self.count()})
        except (OSError, ValueError) as exc:
            content = str(exc)
        self.report.configure(state="normal")
        self.report.delete("1.0", "end")
        self.report.insert("1.0", content)
        self.report.configure(state="disabled")

    def _begin_capture(self, folder: Path, mode: str) -> tuple[Path, datetime, dict, str]:
        return folder, datetime.now().astimezone(), capture_baseline(folder), mode

    def _report_worker(self, session, pid: int | None, code: int | None) -> None:
        folder, started, baseline, mode = session
        try:
            file, report = save_capture_report(folder, started, baseline, pid, code, mode)
            self.capture_queue.put((file, report, None))
        except Exception as exc:
            self.capture_queue.put((None, None, str(exc)))

    def _capture_async(self, session, pid: int | None, code: int | None) -> None:
        self.report_jobs += 1
        threading.Thread(target=self._report_worker, args=(session, pid, code), daemon=True).start()

    def launch_light(self) -> None:
        """Apply a reversible settings profile before starting the root executable."""
        try:
            if self.game and self.game.running() or find_running_kenshi():
                raise RuntimeError("请先关闭游戏，再使用轻载启动")
            folder, count = self.path(), self.count()
            summary = apply_light_profile(folder)
            active = profile_backup(folder).exists()
            try:
                session = self._begin_capture(folder, f"一键轻载启动：{summary}；逻辑核心 {count}")
                game = GameProcess.start_limited(folder / "kenshi_x64.exe", count)
            except Exception:
                if active:
                    try:
                        restore_light_profile(folder)
                    except (OSError, ValueError):
                        pass  # Original settings remain backed up for manual recovery.
                raise
            self.game, self.capture = game, session
            self.restore_profile_when_exit = active
            self.status.set(summary + "。游戏退出后尝试自动恢复；读档和切到人群时保持本窗口打开。")
            save_settings({"folder": str(folder), "cores": count})
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(TITLE, str(exc))

    def apply_only(self) -> None:
        """Keep Steam / RE_Kenshi startup intact, changing settings only before it runs."""
        try:
            if find_running_kenshi():
                raise RuntimeError("请先退出游戏，再应用轻载设置")
            folder = self.path()
            summary = apply_light_profile(folder)
            self.status.set(summary + "。按平时方式启动游戏，再点‘连接已运行游戏’。")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(TITLE, str(exc))

    def restore_settings(self) -> None:
        try:
            if find_running_kenshi():
                raise RuntimeError("请先完全退出游戏，再恢复原设置")
            self.status.set(restore_light_profile(self.path()))
            self.restore_profile_when_exit = False
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(TITLE, str(exc))

    def launch_capture(self) -> None:
        """Start without affinity modification so the diagnostic run has one variable less."""
        try:
            if self.game and self.game.running():
                raise RuntimeError("已有连接的游戏进程。请先关闭游戏，或点‘捕获当前状态’")
            folder = self.path()
            if find_running_kenshi():
                raise RuntimeError("发现已运行的 Kenshi。请点‘连接已运行游戏’")
            session = self._begin_capture(folder, "正常启动：未限制 CPU 核心")
            self.game = GameProcess.start_limited(folder / "kenshi_x64.exe", None)
            self.capture = session
            self.status.set("正在自动捕获 PID " + str(self.game.pid) + "；保持本窗口打开，正常进入存档并复现问题。")
            save_settings({"folder": str(folder), "cores": self.count()})
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(TITLE, str(exc))

    def launch(self) -> None:
        try:
            if self.game and self.game.running():
                raise RuntimeError("已有连接的游戏进程。请在原窗口继续，或先关闭游戏")
            path, cores = self.path(), self.count()
            running = find_running_kenshi()
            if running:
                raise RuntimeError("发现已运行的 Kenshi。请点‘连接已运行游戏’")
            session = self._begin_capture(path, f"保护启动：{cores} 个逻辑核心")
            self.game = GameProcess.start_limited(path / "kenshi_x64.exe", cores)
            self.capture = session
            self.status.set(f"已启动 PID {self.game.pid}，限制为 {cores} 个逻辑核心；读档稳定后点‘恢复全部核心’。")
            save_settings({"folder": str(path), "cores": cores})
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(TITLE, str(exc))

    def attach(self) -> None:
        try:
            if self.game and self.game.running():
                raise RuntimeError("已经连接了一个 Kenshi 进程")
            processes = find_running_kenshi()
            if not processes:
                raise RuntimeError("找不到正在运行的 Kenshi 进程")
            if len(processes) != 1:
                raise RuntimeError("检测到多个 Kenshi 进程。请只运行一个游戏实例")
            folder = self.path()
            self.game = GameProcess.attach(processes[0][0])
            self.capture = self._begin_capture(folder, "连接已运行游戏（此前的日志可能属于当前会话）")
            self.restore_profile_when_exit = profile_backup(folder).exists()
            self.status.set(f"已连接 PID {self.game.pid}；读档或切换场景前点‘开启载入保护’。")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(TITLE, str(exc))

    def protect(self) -> None:
        try:
            if not self.game or not self.game.running():
                raise RuntimeError("先启动或连接游戏")
            self.game.limit(self.count())
            self.status.set(f"载入保护生效：PID {self.game.pid}，{self.count()} 个逻辑核心。场景稳定后手动恢复。")
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror(TITLE, str(exc))

    def restore(self) -> None:
        try:
            if not self.game:
                raise RuntimeError("没有已连接的游戏")
            self.game.restore()
            self.status.set(f"PID {self.game.pid} 已恢复原有 CPU 核心范围。")
        except (OSError, RuntimeError) as exc:
            messagebox.showerror(TITLE, str(exc))

    def capture_now(self) -> None:
        try:
            if self.capture is None:
                self.capture = self._begin_capture(self.path(), "手动捕获，未通过本程序启动")
            pid = self.game.pid if self.game else None
            code = self.game.exit_code() if self.game and not self.game.running() else None
            self._capture_async(self.capture, pid, code)
            self.status.set("正在收集日志、模组列表和缺失文件位置；稍后自动复制报告。")
        except (OSError, ValueError) as exc:
            messagebox.showerror(TITLE, str(exc))

    def copy_report(self) -> None:
        if not self.last_report:
            messagebox.showinfo(TITLE, "还没有生成报告。请先点‘一键捕获并启动’或‘捕获当前状态’。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_report)
        self.status.set("报告已复制。返回聊天窗口直接粘贴即可。")

    def poll(self) -> None:
        if self.game and not self.game.running():
            pid = self.game.pid
            code = self.game.exit_code()
            self.game.close(restore=False)
            self.game = None
            session, self.capture = self.capture, None
            notice = ""
            if getattr(self, "restore_profile_when_exit", False):
                self.restore_profile_when_exit = False
                try:
                    if find_running_kenshi():
                        notice = "检测到其他游戏实例；轻载配置备份保留，请退出后点‘恢复原设置’。"
                    else:
                        notice = restore_light_profile(session[0] if session else self.path())
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    notice = "恢复轻载设置失败：" + str(exc) + "；原备份保留。"
            self.profile_notice = notice
            if session:
                self._capture_async(session, pid, code)
                self.status.set("游戏已退出（退出码 " + (f"0x{code:08X}" if code is not None else "未知")
                                + "）；正在生成报告，请勿关闭本窗口。" + notice)
            else:
                self.status.set("游戏进程已退出。")
        while True:
            try:
                file, report, error = self.capture_queue.get_nowait()
            except queue.Empty:
                break
            self.report_jobs = max(0, self.report_jobs - 1)
            if error:
                self.status.set("生成报告失败：" + error)
                continue
            self.last_report = report
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(report)
                self.status.set(f"报告已保存并复制：{file}。返回聊天窗口直接粘贴。" +
                                getattr(self, "profile_notice", ""))
            except tk.TclError:
                self.status.set(f"报告已保存：{file}。打开 TXT 并复制全部内容。" +
                                getattr(self, "profile_notice", ""))
        self.root.after(1200, self.poll)

    def quit(self) -> None:
        if self.report_jobs:
            messagebox.showinfo(TITLE, "报告正在保存，请等待界面显示‘报告已保存并复制’后再关闭。")
            return
        if self.game and self.game.running() and self.capture:
            if not messagebox.askyesno(TITLE, "游戏仍在运行。关闭本窗口会停止自动捕获和轻载设置的自动恢复；"
                                              "备份会保留，游戏退出后可重新打开本程序点‘恢复原设置’。确定关闭吗？"):
                return
        if self.game:
            try:
                self.game.close(restore=True)
            except OSError as exc:
                messagebox.showerror(TITLE, f"无法恢复游戏 CPU 核心：{exc}\n请先在任务管理器恢复进程亲和性。")
                return
            self.game = None
        self.root.destroy()


def main() -> None:
    if sys.platform != "win32":
        print("此工具仅能在 Windows 上运行。")
        raise SystemExit(1)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
