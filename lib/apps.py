# -*- coding: utf-8 -*-
"""
apps.py —— SolidWorks / AutoCAD 的启动路径解析与启动

背景（用户需求）：
    侧边栏要能填 SolidWorks 和 AutoCAD 的可执行文件路径。
    COM 的 Dispatch('SldWorks.Application') 依赖系统注册表里的 COM 服务器，
    多版本共存、绿色版、装在非默认盘时经常连不上或连到别的版本。
    显式给路径后：先按路径把进程拉起来，再 Dispatch 连上运行中的实例，
    这样最稳。

用法：
    from apps import ensure_solidworks, ensure_autocad
    sw = ensure_solidworks(exe_path_or_empty, visible=True)
    acad = ensure_autocad(exe_path_or_empty, visible=False)
"""

import os
import subprocess
import time

SOLIDWORKS = 'solidworks'
AUTOCAD = 'autocad'

# 常见安装位置（自动探测时按顺序试）
_SW_CANDIDATES = [
    r'F:\sw\SOLIDWORKS\SLDWORKS.exe',
    r'C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe',
    r'C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS (2)\SLDWORKS.exe',
    r'D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe',
    r'E:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe',
]

_ACAD_CANDIDATES = [
    r'C:\Program Files\Autodesk\AutoCAD 2018\acad.exe',
    r'C:\Program Files\Autodesk\AutoCAD 2019\acad.exe',
    r'C:\Program Files\Autodesk\AutoCAD 2020\acad.exe',
    r'C:\Program Files\Autodesk\AutoCAD 2021\acad.exe',
    r'C:\Program Files\Autodesk\AutoCAD 2022\acad.exe',
    r'C:\Program Files\Autodesk\AutoCAD 2023\acad.exe',
    r'C:\Program Files\Autodesk\AutoCAD 2024\acad.exe',
    r'C:\Program Files\Autodesk\AutoCAD 2025\acad.exe',
]

_PROC_NAMES = {
    SOLIDWORKS: 'SLDWORKS.exe',
    AUTOCAD: 'acad.exe',
}

_REG_PATHS = {
    SOLIDWORKS: [
        (r'SOFTWARE\SolidWorks', 'SolidWorks Folder'),
        (r'SOFTWARE\WOW6432Node\SolidWorks', 'SolidWorks Folder'),
    ],
    AUTOCAD: [
        (r'SOFTWARE\Autodesk\AutoCAD\R22.0\ACAD-8001:804', 'AcadLocation'),
        (r'SOFTWARE\Autodesk\AutoCAD\R23.0\ACAD-1001:804', 'AcadLocation'),
        (r'SOFTWARE\Autodesk\AutoCAD\R24.0\ACAD-1101:804', 'AcadLocation'),
        (r'SOFTWARE\Autodesk\AutoCAD\R24.1\ACAD-1201:804', 'AcadLocation'),
        (r'SOFTWARE\Autodesk\AutoCAD\R24.2\ACAD-1301:804', 'AcadLocation'),
        (r'SOFTWARE\Autodesk\AutoCAD\R24.3\ACAD-1401:804', 'AcadLocation'),
    ],
}


def _reg_lookup(kind):
    """从注册表猜安装目录（失败返回 None）。"""
    try:
        import winreg
    except Exception:
        return None
    exe = _PROC_NAMES[kind]
    for subkey, value_name in _REG_PATHS.get(kind, []):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    val, _ = winreg.QueryValueEx(k, value_name)
                if not val:
                    continue
                cand = os.path.join(str(val), exe)
                if os.path.isfile(cand):
                    return cand
            except Exception:
                continue
    return None


def resolve_exe(kind, explicit=None):
    """解析可执行文件路径。

    优先级：显式指定 > 注册表 > 常见安装位置。
    返回 (路径或 None, 来源说明)
    """
    if explicit:
        p = os.path.abspath(os.path.expandvars(str(explicit).strip().strip('"')))
        if os.path.isfile(p):
            return p, '手动指定'
        # 允许用户只给目录
        if os.path.isdir(p):
            cand = os.path.join(p, _PROC_NAMES[kind])
            if os.path.isfile(cand):
                return cand, '手动指定（目录）'
        return None, f'手动指定的路径不存在: {explicit}'

    reg = _reg_lookup(kind)
    if reg:
        return reg, '注册表'

    for cand in (_SW_CANDIDATES if kind == SOLIDWORKS else _ACAD_CANDIDATES):
        if os.path.isfile(cand):
            return cand, '常见位置'

    return None, '未找到（请在面板里填路径）'


def is_running(kind):
    """进程是否在跑。"""
    try:
        out = subprocess.run(
            ['tasklist', '/FI', f'IMAGENAME eq {_PROC_NAMES[kind]}', '/NH'],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return _PROC_NAMES[kind].lower() in (out.stdout or '').lower()
    except Exception:
        return False


def launch(kind, exe_path, extra_args=None):
    """启动进程（已在跑则跳过）。返回 (是否启动了, 说明)。"""
    if is_running(kind):
        return False, '已在运行'

    if not exe_path or not os.path.isfile(exe_path):
        return False, '没有可用的可执行文件路径'

    args = [exe_path, '/nologo']
    if kind == AUTOCAD:
        # 不加载图形界面插件，减少启动时间；保留界面便于用户看
        args = [exe_path, '/nologo']
    if extra_args:
        args.extend(extra_args)

    try:
        subprocess.Popen(
            args,
            cwd=os.path.dirname(exe_path) or None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) |
                          getattr(subprocess, 'DETACHED_PROCESS', 0),
        )
        return True, f'已启动 {os.path.basename(exe_path)}'
    except Exception as exc:
        return False, f'启动失败: {type(exc).__name__}: {exc}'


def ensure_running(kind, exe_hint=None, wait_s=45, log=None):
    """确保目标程序在运行。返回 (exe路径或None, 来源, 启动说明)。"""
    exe, source = resolve_exe(kind, exe_hint)

    if is_running(kind):
        return exe, source, '进程已在运行，直接连接'

    if not exe:
        return None, source, '未找到可执行文件，尝试直接 Dispatch'

    started, msg = launch(kind, exe)
    if started:
        if log:
            try:
                log(f'{kind}: {msg}（{exe}）')
            except Exception:
                pass
        # 等进程窗口起来
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if is_running(kind):
                time.sleep(3.0)   # 再给 COM 一点时间注册
                break
            time.sleep(1.0)
    return exe, source, msg


def probe_info(sw_hint=None, acad_hint=None):
    """给 probe 用的路径信息。"""
    out = {}
    for kind, hint, label in (
            (SOLIDWORKS, sw_hint, 'solidworks'),
            (AUTOCAD, acad_hint, 'autocad')):
        exe, source = resolve_exe(kind, hint)
        out[label] = {
            'exe': exe,
            'source': source,
            'exists': bool(exe and os.path.isfile(exe)),
            'running': is_running(kind),
        }
    return out
