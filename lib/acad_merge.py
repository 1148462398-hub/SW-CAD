# -*- coding: utf-8 -*-
"""
acad_merge.py —— 把多张 DWG 并成「一个 DXF 里放多个图框」

背景（用户需求）：
    「多个 CAD 图在一个 DXF 文件里就可以了，只是多放几个工程图图框那种」

为什么不走 SolidWorks 多图纸：
    IDrawingDoc::NewSheet2 / NewSheet3 / NewSheet4 的 `TemplateIn` 参数
    在这台机器上一直报 `类型不匹配。 (None, 3)`（试过空串、模板路径、
    pythoncom.Empty 都不行），所以改成「每张图单独出 DWG → AutoCAD 合并」。

做法：
    1. 新建一个空 DWG
    2. 依次打开每张源 DWG，用 CopyObjects 把模型空间实体复制进目标
    3. 复制后整体平移，让每张图横向排开（间隔 gap_mm）
    4. SaveAs 成 DXF

⚠ 关键点：
    · CopyObjects 需要「对象数组」和「目标容器」，返回新对象数组，
      再对新对象逐个 Move 即可。
    · 每张源图记得先 Close(False)，否则文件越开越多。
    · 目标图的界线由来源图决定（A3=420×297），所以排布按 420+gap 递增。
"""

import os
import time

import pythoncom
import win32com.client


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _all_modelspace_entities(doc):
    """用 SelectionSet 收集模型空间全部实体。

    ⚠ 不能用 `ModelSpace.Item(i)` 攒成数组去 CopyObjects：
    报「对象不在数据库中」（那些 IDispatch 代理不适合跨文档复制）。
    官方推荐用 SelectionSet 选择集，它返回的数组才是合法输入。
    """
    from win32com.client import VARIANT
    name = f'DSH_MERGE_ALL_{os.getpid()}'
    ss = None
    try:
        for old in doc.SelectionSets:
            if str(old.Name) == name:
                old.Delete()
                break
    except Exception:
        pass
    try:
        ss = doc.SelectionSets.Add(name)
    except Exception:
        return []
    try:
        # acSelectionSetAll = 5，全选模式**只接受一个参数**；
        # 多传几个 0 会变成「选择了 0 个对象」
        ss.Select(5)
    except Exception:
        try:
            ss.Select(5, pythoncom.Empty, pythoncom.Empty, 0, pythoncom.Empty)
        except Exception:
            pass
    out = []
    try:
        arr = ss
        n = int(ss.Count)
        for i in range(n):
            try:
                out.append(arr.Item(i))
            except Exception:
                continue
    except Exception:
        pass
    return out, ss


def merge_dwgs(src_dwgs, out_path, gap_mm=20.0, sheet_w_mm=420.0,
               exe_hint=None, log=None):
    """把多张 DWG 横向并成一个 DXF/DWG。

    做法：在目标图里对每个源 DWG 调 `InsertBlock(点, <dwg路径>, 1,1,1, 0)`——
    AutoCAD 允许块名给一个**外部 DWG 路径**，等价于把整张图当块插进来。
    比跨文档 CopyObjects 稳得多（后者老是报「对象不在数据库中」）。

    src_dwgs : 源 DWG 路径列表（顺序即从左到右）
    out_path : 输出路径；扩展名 .dxf 或 .dwg 决定格式
    返回 {'ok', 'out', 'sheets', 'detail', 'error'}
    """
    src_dwgs = [os.path.abspath(p) for p in (src_dwgs or []) if p and os.path.isfile(p)]
    if not src_dwgs:
        return {'ok': False, 'error': '没有可合并的 DWG', 'sheets': 0}

    try:
        import apps
        apps.ensure_running(apps.AUTOCAD, exe_hint, log=log)
    except Exception:
        pass

    pythoncom.CoInitialize()
    acad = win32com.client.Dispatch('AutoCAD.Application')
    try:
        while acad.Documents.Count > 0:
            acad.Documents.Item(0).Close(False)
    except Exception:
        pass
    time.sleep(1.0)

    acad.Documents.Add()
    time.sleep(1.0)
    dst = acad.ActiveDocument

    detail = []
    offset_x = 0.0
    placed = 0

    for idx, src_path in enumerate(src_dwgs):
        try:
            from win32com.client import VARIANT
            pt = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                         (offset_x, 0.0, 0.0))
            ref = dst.ModelSpace.InsertBlock(pt, src_path, 1.0, 1.0, 1.0, 0.0)
            detail.append({'file': os.path.basename(src_path), 'ok': True,
                           'offsetMm': round(offset_x, 1),
                           'block': bool(ref is not None)})
            placed += 1
            if log:
                try:
                    log(f'并入 {os.path.basename(src_path)} @ x={offset_x:.0f}mm')
                except Exception:
                    pass
        except Exception as exc:
            detail.append({'file': os.path.basename(src_path), 'ok': False,
                           'error': f'InsertBlock: {exc}'})
        offset_x += sheet_w_mm + gap_mm

    if placed == 0:
        return {'ok': False, 'error': '一张图都没插进去',
                'sheets': 0, 'detail': detail}

    try:
        # ⚠ SaveAs 只给一个路径时，AutoCAD 一律按 DWG 存，
        # 于是 "merged.dxf" 会变成 "merged.dxf.dwg"。
        # 必须显式给 acSaveAsType：61=ac2013_dxf，63=ac2018_dxf，24=ac2013_dwg。
        lower = out_path.lower()
        if lower.endswith('.dxf'):
            file_type = 61      # ac2013_dxf：兼容性最好
        elif lower.endswith('.dwg'):
            file_type = 24      # ac2013_dwg
        else:
            file_type = 61
        try:
            dst.SaveAs(out_path, file_type)
        except Exception:
            # 某些版本不接受第二个参数
            dst.SaveAs(out_path)
        time.sleep(2.0)
    except Exception as exc:
        return {'ok': False, 'error': f'保存失败: {exc}',
                'sheets': placed, 'detail': detail}

    # SaveAs 偶尔会自己加后缀，这里做一次兜底改名
    if not os.path.isfile(out_path):
        alt = out_path + ('.dwg' if lower.endswith('.dxf') else '')
        if os.path.isfile(alt):
            try:
                os.replace(alt, out_path)
            except Exception:
                out_path = alt

    return {'ok': os.path.isfile(out_path), 'out': out_path,
            'sheets': placed,
            'widthMm': round(offset_x - gap_mm, 1),
            'detail': detail}


def describe_layers(dwg_path):
    """列出一张 DWG 里有哪些图层和大致范围（调试用）。"""
    pythoncom.CoInitialize()
    acad = win32com.client.Dispatch('AutoCAD.Application')
    acad.Documents.Open(dwg_path)
    time.sleep(1.2)
    doc = acad.ActiveDocument
    ms = doc.ModelSpace
    out = {}
    for i in range(int(_safe(lambda: ms.Count, 0))):
        try:
            e = ms.Item(i)
            lay = str(e.Layer)
            out[lay] = out.get(lay, 0) + 1
        except Exception:
            continue
    return out
