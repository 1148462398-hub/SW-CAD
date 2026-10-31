# -*- coding: utf-8 -*-
"""
featureworks.py —— 用 FeatureWorks 把导入实体「识别」成真实特征

背景（用户需求）：
    用户问「STEP 能在 SolidWorks 里先自动识别特征再出图吗」。
    STEP/IGES 导入后只是一坨「导入实体」，没有设计尺寸，
    所以出图时自动插入尺寸 = 0。FeatureWorks 能把它反推成
    拉伸/旋转/切除/孔等参数化特征，之后就能正常标注。

⚠ 关键坑（都是实测踩出来的）：

1. FeatureWorks 的 COM 类注册成了**相对路径** `.\fworks\fworks.dll`，
   只有在 SolidWorks 进程内才能解析。所以：
   - 直接 `Dispatch('FeatureWorks.FeatureWorksApp')` → 找不到指定的模块 ✘
   - 必须在 SW 里 `sw.LoadAddIn(<SW目录>\fworks\fworks.dll)` 再 GetAddInObject ✔

2. 拿到的对象是 **vtable 接口**（IFeatureWorksApp），不是纯 IDispatch。
   pywin32 看不到方法（`<COMObject <unknown>>`），makepy 也没用。
   必须用 **comtypes**：`GetAddInObject(...).QueryInterface(mod.IFeatureWorksApp)` ✔

3. comtypes 的 `GetActiveObject` 报 MK_E_UNAVAILABLE（ROT 里找不到），
   但 SW 是单实例服务，`CreateObject('SldWorks.Application')` 会拿到运行中的实例 ✔

4. 方法签名（以 FWorks.tlb 生成的类型提示为准）：
       RecognizeFeatureAutomatic(options)   ← 只有 options，作用于**活动文档**
       CreateFeatures(options)
   options 是位标志，见下面的常量。

5. 识别过程中 SolidWorks 会弹模态框（例如
   「SOLIDWORKS 数据库 swbrowser.sldedb 遗失」），**会把调用卡住十几分钟**。
   必须开一个后台线程自动按回车把它点掉。
"""

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

# ---- fwAutomaticRecognitionOptions_e（识别哪些特征）----
FW_EXTRUDE = 1
FW_VOLUME = 2
FW_REVOLVE = 4
FW_HOLES = 8
FW_CHAMFILS = 16
FW_RIBS = 32
FW_BASE_FLANGE = 64
FW_SKETCHED_BEND = 128
FW_AUTO_EDGE_FLANGE = 256
FW_AUTO_HEM_FLANGE = 512

# 常用组合：拉伸/体积/旋转/孔/倒角/筋
FW_RECOGNIZE_COMMON = (FW_EXTRUDE | FW_VOLUME | FW_REVOLVE |
                       FW_HOLES | FW_CHAMFILS | FW_RIBS)

# ---- fwFeatureCreationOptions_e（怎么建特征）----
FW_ADD_CONSTRAINTS = 1
FW_ALLOW_FAIL = 2

# 大多数导入件没有 Toolbox 的孔数据，倒角/筋识别容易失败，
# 这里给一组「稳一点」的默认值
DEFAULT_RECOGNIZE = FW_EXTRUDE | FW_VOLUME | FW_REVOLVE
DEFAULT_CREATE = FW_ADD_CONSTRAINTS | FW_ALLOW_FAIL


# ---------------------------------------------------------------- 路径

def find_fworks_dll(solidworks_exe=None):
    """找 FeatureWorks 的 fworks.dll（它跟 SLDWORKS.exe 同级的 fworks 目录）。"""
    cands = []
    if solidworks_exe:
        cands.append(os.path.join(os.path.dirname(solidworks_exe), 'fworks', 'fworks.dll'))
    try:
        import apps
        exe, _ = apps.resolve_exe(apps.SOLIDWORKS, solidworks_exe)
        if exe:
            cands.append(os.path.join(os.path.dirname(exe), 'fworks', 'fworks.dll'))
    except Exception:
        pass
    for c in cands:
        if c and os.path.isfile(c):
            return c
    # 最后兜底：常见位置
    for c in (r'F:\sw\SOLIDWORKS\fworks\fworks.dll',
              r'C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\fworks\fworks.dll'):
        if os.path.isfile(c):
            return c
    return None


def find_fworks_tlb(solidworks_exe=None):
    dll = find_fworks_dll(solidworks_exe)
    if not dll:
        return None
    tlb = os.path.join(os.path.dirname(dll), 'FWorks.tlb')
    return tlb if os.path.isfile(tlb) else None


# ---------------------------------------------------------------- 弹窗自动确认

class _DialogWatcher(threading.Thread):
    """后台盯着 SolidWorks 的模态框，自动按回车。

    FeatureWorks 识别时会弹「swbrowser.sldedb 遗失」之类的一次性提示，
    不点掉 COM 调用会一直挂着（实测卡了 874 秒）。
    """

    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.clicked = 0
        if not hasattr(ctypes, 'windll'):
            self._enabled = False
            return
        self._enabled = True
        self._user32 = ctypes.windll.user32
        self._enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _class_of(self, h):
        b = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(h, b, 256)
        return b.value

    def run(self):
        if not self._enabled:
            return
        while not self._stop.is_set():
            found = []

            def cb(h, lp):
                try:
                    if self._user32.IsWindowVisible(h) and self._class_of(h) == '#32770':
                        found.append(h)
                except Exception:
                    pass
                return True

            try:
                self._user32.EnumWindows(self._enum_proc(cb), 0)
                for d in found:
                    self._user32.SetForegroundWindow(d)
                    time.sleep(0.2)
                    self._user32.keybd_event(0x0D, 0, 0, 0)   # Enter down
                    self._user32.keybd_event(0x0D, 0, 2, 0)   # Enter up
                    self.clicked += 1
                    time.sleep(0.5)
            except Exception:
                pass
            self._stop.wait(1.0)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------- 连接

def _connect_fw(sw, solidworks_exe=None, log=None):
    """加载 FeatureWorks 并返回 (fw 接口, 说明)。"""
    dll = find_fworks_dll(solidworks_exe)
    tlb = find_fworks_tlb(solidworks_exe)
    if not dll:
        return None, '没找到 fworks.dll（FeatureWorks 未安装）'
    if not tlb:
        return None, '没找到 FWorks.tlb'

    try:
        rc = sw.LoadAddIn(dll)
    except Exception as exc:
        return None, f'LoadAddIn 失败：{exc}'
    # rc: 0=成功，其他值通常表示「已加载」也算可用
    time.sleep(1.0)

    try:
        import comtypes.client as cc
    except Exception as exc:
        return None, f'缺少 comtypes（pip install comtypes）：{exc}'

    try:
        mod = cc.GetModule(tlb)
        fw_sw = cc.CreateObject('SldWorks.Application')
        raw = fw_sw.GetAddInObject('FeatureWorks.FeatureWorksApp')
        if raw is None:
            return None, 'GetAddInObject 返回空（加载项未就绪）'
        ifw = raw.QueryInterface(mod.IFeatureWorksApp)
    except Exception as exc:
        return None, f'取 FeatureWorks 接口失败：{type(exc).__name__}: {exc}'

    if log:
        try:
            log(f'FeatureWorks 就绪（LoadAddIn={rc}，dll={dll}）')
        except Exception:
            pass
    return ifw, 'ok'


# ---------------------------------------------------------------- 主流程

def recognize(sw, part_doc, recognize_options=None, create_options=None,
              solidworks_exe=None, log=None):
    """在**已打开并激活**的零件上跑特征识别。

    sw        : pywin32 的 SolidWorks 对象
    part_doc  : 已打开的零件文档（FeatureWorks 作用于活动文档，务必先 Activate）
    返回 {'ok', 'recognized', 'created', 'featuresBefore', 'featuresAfter',
          'added', 'note', 'seconds'}
    """
    recognize_options = DEFAULT_RECOGNIZE if recognize_options is None else recognize_options
    create_options = DEFAULT_CREATE if create_options is None else create_options

    def feat_names(doc, limit=200):
        out = []
        try:
            f = doc.FirstFeature
            while f is not None and len(out) < limit:
                try:
                    out.append(str(f.Name))
                except Exception:
                    pass
                f = f.GetNextFeature
        except Exception:
            pass
        return out

    result = {
        'ok': False, 'recognized': None, 'created': None,
        'featuresBefore': 0, 'featuresAfter': 0,
        'added': [], 'note': '', 'seconds': 0.0
    }

    before = feat_names(part_doc)
    result['featuresBefore'] = len(before)

    ifw, note = _connect_fw(sw, solidworks_exe, log=log)
    if ifw is None:
        result['note'] = note
        return result

    # 让目标零件成为活动文档（FeatureWorks 不接收 doc 参数）
    try:
        sw.ActivateDoc3(part_doc.GetTitle, True, 0, 0)
    except Exception:
        try:
            part_doc.Activate()
        except Exception:
            pass
    time.sleep(0.8)

    watcher = _DialogWatcher()
    watcher.start()
    t0 = time.time()
    try:
        try:
            result['recognized'] = ifw.RecognizeFeatureAutomatic(recognize_options)
        except Exception as exc:
            result['note'] = f'识别失败：{type(exc).__name__}: {exc}'
            return result

        try:
            result['created'] = ifw.CreateFeatures(create_options)
        except Exception as exc:
            result['note'] = f'建特征失败：{type(exc).__name__}: {exc}'
            return result
    finally:
        watcher.stop()
        result['seconds'] = round(time.time() - t0, 1)

    after = feat_names(part_doc)
    result['featuresAfter'] = len(after)
    result['added'] = [f for f in after if f not in before]
    result['ok'] = bool(result['added'])
    if not result['note']:
        result['note'] = (
            f'新增 {len(result["added"])} 个特征' if result['added']
            else '识别完成但没有生成特征（该实体可能不含可识别的规则特征）'
        )
    if watcher.clicked and log:
        try:
            log(f'识别过程中自动确认了 {watcher.clicked} 个弹窗')
        except Exception:
            pass
    return result
