# -*- coding: utf-8 -*-
"""
acad_plot.py —— 按工程图最外层包围框一键打印 PDF

设计要点（全是这一路实测踩出来的，改之前先看）：
  1. PlotToFile 是**异步**的：调用立刻返回，PDF 要 20~25 秒才落盘。
     查太快会误判失败 —— 必须轮询等文件大小稳定。
  2. 必须**先 SetWindowToPlot、再设 PlotType=4(acWindow)**。
     顺序反了会报「输入无效」。
  3. 设纸型/PlotType 之前必须 RefreshPlotDeviceInfo()，否则同样「输入无效」。
  4. 绘图仪名固定 "DWG To PDF.pc3"；纸张用 CanonicalMediaName（PlotPaperSize 是只读的）。
  5. **绝对不要用 -PLOT 命令行**：会卡在提示符上，把 AutoCAD COM 整个搞挂。
  6. 连 AutoCAD 用 Dispatch 而不是 GetActiveObject：后者常报 MK_E_UNAVAILABLE。
  7. 打印用 monochrome.ctb，DWG 里保留图层颜色但输出纯黑（GB 图纸要黑白）。
"""

import os
import time

import pythoncom
import win32com.client as win32
from win32com.client import VARIANT

# 图幅 -> (宽mm, 高mm)，横向
PAPER_MM = {
    'A0': (1189.0, 841.0),
    'A1': (841.0, 594.0),
    'A2': (594.0, 420.0),
    'A3': (420.0, 297.0),
    'A4': (297.0, 210.0),
    'A5': (210.0, 148.0),
}

PLOT_DEVICE = 'DWG To PDF.pc3'
MONO_STYLE = 'monochrome.ctb'


def _vec2(x, y):
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(x), float(y)))


def connect(exe_hint=None, log=None):
    """连上 AutoCAD，返回 (acad, doc)。

    用 Dispatch 而不是 GetActiveObject（后者常报 MK_E_UNAVAILABLE）。
    exe_hint：面板里配的 acad.exe 路径；没在跑就按这个路径拉起来。
    """
    pythoncom.CoInitialize()
    try:
        import apps
        _, source, msg = apps.ensure_running(apps.AUTOCAD, exe_hint, log=log)
        if log:
            try:
                log(f'AutoCAD: {msg}（来源 {source}）')
            except Exception:
                pass
    except Exception:
        pass

    acad = win32.Dispatch('AutoCAD.Application')
    if acad.Documents.Count == 0:
        acad.Documents.Add()
        time.sleep(1.5)
    return acad, acad.ActiveDocument


def open_or_activate(acad, dwg_path):
    """打开指定 DWG（已打开则直接激活）。返回 doc。

    注意：AutoCAD 里可能同时开着未保存的 DrawingN，
    那种文档取 .FullName 会抛 AttributeError(<unknown>.Name)，
    所以每个属性读取都要单独兜住，不能整段 try 掉。
    """
    target = os.path.normcase(os.path.abspath(dwg_path))
    target_name = os.path.basename(dwg_path)

    def doc_path(d):
        try:
            return d.FullName or ''
        except Exception:
            return ''

    def doc_name(d):
        try:
            return d.Name or ''
        except Exception:
            return ''

    count = 0
    try:
        count = acad.Documents.Count
    except Exception:
        count = 0

    for i in range(count):
        try:
            d = acad.Documents.Item(i)
        except Exception:
            continue
        fp = doc_path(d)
        nm = doc_name(d)
        hit = False
        if fp:
            try:
                hit = os.path.normcase(os.path.abspath(fp)) == target
            except Exception:
                hit = False
        if not hit and nm and nm == target_name:
            hit = True
        if hit:
            try:
                d.Activate()
                time.sleep(1.0)
            except Exception:
                pass
            return acad.ActiveDocument

    doc = acad.Documents.Open(dwg_path)
    time.sleep(2.5)
    try:
        doc.Activate()
    except Exception:
        pass
    time.sleep(1.0)
    return acad.ActiveDocument


def _close_quietly(doc):
    """关文档，任何异常都吞掉（关不掉也不该影响主流程）。"""
    try:
        doc.Close(False)
        return True
    except Exception:
        return False


def export_dxf_from_dwg(dwg_path, dxf_path, exe_hint=None, log=None):
    """用 AutoCAD 把加工后的 DWG 另存成 DXF。

    ⚠ 为什么必须重新导出：
        engine 里 SolidWorks 先导出过一份 DXF，但那份是**AutoCAD 加工之前**的
        （没有 DSH_DIM 标注、视图没搬进图框、没有中心线）。
        用户拿到的 DXF 应该是 AutoCAD 处理后的结果，所以加工完要重新导一次。

    做法：打开 DWG → SaveAs(dxf, 61=ac2013_dxf) → Close(False)。
    SaveAs 只改「文档关联的文件名」，磁盘上的原 DWG 不受影响。
    """
    try:
        import apps
        apps.ensure_running(apps.AUTOCAD, exe_hint, log=log)
    except Exception:
        pass

    pythoncom.CoInitialize()
    acad = win32.Dispatch('AutoCAD.Application')
    try:
        acad.Documents.Open(os.path.abspath(dwg_path))
        time.sleep(1.5)
        doc = acad.ActiveDocument
    except Exception as exc:
        return {'ok': False, 'error': f'打开 DWG 失败: {exc}'}

    try:
        try:
            doc.SaveAs(os.path.abspath(dxf_path), 61)   # ac2013_dxf
        except Exception:
            doc.SaveAs(os.path.abspath(dxf_path))
        time.sleep(1.5)
    except Exception as exc:
        _close_quietly(doc)
        return {'ok': False, 'error': f'另存 DXF 失败: {exc}'}

    _close_quietly(doc)

    if not os.path.isfile(dxf_path):
        # 有些版本会自己补后缀
        alt = dxf_path + '.dwg'
        if os.path.isfile(alt):
            try:
                os.replace(alt, dxf_path)
            except Exception:
                pass

    ok = os.path.isfile(dxf_path) and os.path.getsize(dxf_path) > 0
    if log:
        try:
            log(f'AutoCAD 重新导出 DXF：{"成功" if ok else "失败"} {dxf_path}')
        except Exception:
            pass
    return {'ok': ok, 'dxf': dxf_path,
            'size': os.path.getsize(dxf_path) if ok else 0}


def close_document(dwg_path, exe_hint=None):
    """关掉指定的 DWG 文档（不动 AutoCAD 进程，也不碰用户自己的图）。

    用户要求：「打开过的界面如果后续用不上了，就顺带关掉」——
    出图/标注/打印完成后，这些 DWG 窗口留着只是占内存。
    返回 (是否关了, 说明)
    """
    try:
        acad, _ = connect(exe_hint=exe_hint)
    except Exception as exc:
        return False, f'连不上 AutoCAD: {exc}'

    target = os.path.normcase(os.path.abspath(dwg_path))
    target_name = os.path.basename(dwg_path)
    try:
        count = acad.Documents.Count
    except Exception:
        count = 0

    for i in range(count - 1, -1, -1):
        try:
            d = acad.Documents.Item(i)
        except Exception:
            continue
        hit = False
        try:
            fp = d.FullName or ''
            if fp:
                hit = os.path.normcase(os.path.abspath(fp)) == target
        except Exception:
            pass
        if not hit:
            try:
                hit = (d.Name or '') == target_name
            except Exception:
                hit = False
        if hit:
            try:
                d.Close(False)   # False = 不保存提示，直接关
                return True, '已关闭'
            except Exception as exc:
                return False, f'关闭失败: {exc}'
    return False, '未找到已打开的文档'


def _bbox(e):
    try:
        mn = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (0.0, 0.0, 0.0))
        mx = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (0.0, 0.0, 0.0))
        r = e.GetBoundingBox(mn, mx)
        if r and len(r) >= 2:
            p0, p1 = r[0], r[1]
            return (min(p0[0], p1[0]), min(p0[1], p1[1]),
                    max(p0[0], p1[0]), max(p0[1], p1[1]))
    except Exception:
        pass
    return None


def find_outermost_box(doc):
    """找最外层包围框，返回 [x0, y0, x1, y1]（图纸单位 mm）。

    优先级：
      1. 面积最大的**闭合多段线**（GB 图框通常是矩形多段线）
      2. 图框层 `DSH_FRAME` 的几何范围
      3. 所有可见实体的总范围（最后兜底，总比打不出来强）

    注意：即使图层被关闭/冻结，实体仍在 ModelSpace 里，照样能读到 ——
    用户把包围框藏起来之后这个函数依然有效。

    ⚠ 踩过的坑：只认闭合多段线时，若图框是用 4 条 LINE 画的，
    会报「找不到最外层包围框（没有闭合多段线）」直接打印失败。
    """
    ms = doc.ModelSpace
    best = None
    best_area = -1.0
    frame = None           # DSH_FRAME 层几何范围
    everything = None      # 全部几何范围

    def grow(cur, b):
        if not b:
            return cur
        if cur is None:
            return list(b)
        return [min(cur[0], b[0]), min(cur[1], b[1]),
                max(cur[2], b[2]), max(cur[3], b[3])]

    for i in range(ms.Count):
        try:
            e = ms.Item(i)
            name = str(e.ObjectName)

            if name == 'AcDbPolyline':
                try:
                    if e.Closed:
                        b = _bbox(e)
                        if b:
                            area = (b[2] - b[0]) * (b[3] - b[1])
                            if area > best_area:
                                best_area = area
                                best = b
                except Exception:
                    pass

            # 同时累积兜底范围
            b = _bbox(e)
            if b:
                everything = grow(everything, b)
                try:
                    if str(e.Layer) == 'DSH_FRAME':
                        frame = grow(frame, b)
                except Exception:
                    pass
        except Exception:
            continue

    if best:
        return best
    if frame:
        return frame
    return everything


def guess_paper(box):
    """按包围框尺寸反推图幅；差 3mm 以内算匹配。"""
    w = box[2] - box[0]
    h = box[3] - box[1]
    for name, (pw, ph) in PAPER_MM.items():
        if (abs(w - pw) <= 3.0 and abs(h - ph) <= 3.0) or \
           (abs(w - ph) <= 3.0 and abs(h - pw) <= 3.0):
            return name
    return None


def _pick_media_name(media_names, paper):
    """从 CanonicalMediaNames 里挑出指定图幅的横向纸张名。"""
    if not media_names:
        return None
    pw, ph = PAPER_MM.get(paper, (0, 0))
    if not pw:
        return None
    keys = [
        f'ISO_{paper}_({pw:.2f}_x_{ph:.2f}_MM)'.upper(),
        f'ISO_{paper}_({pw:.0f}_x_{ph:.0f}_MM)'.upper(),
    ]
    for name in media_names:
        if name.upper() in keys:
            return name
    # 退一步：模糊匹配（排除出血/扩展版，那些没有页边距）
    loose = []
    for name in media_names:
        up = name.upper()
        if not up.startswith(f'ISO_{paper}_'):
            continue
        if 'FULL_BLEED' in up or 'EXPAND' in up:
            continue
        loose.append(name)
    return loose[0] if loose else None


def _wait_plot_idle(doc, rounds=20, sleep=0.5):
    for _ in range(rounds):
        try:
            if not doc.Plot.BatchPlotProgress:
                return True
        except Exception:
            return True
        time.sleep(sleep)
    return False


def export_pdf(dwg_path, pdf_path, paper=None, monochrome=True,
               timeout_s=120, log=None, exe_hint=None, box=None):
    """把一个 DWG 按「最外层包围框」打印成 PDF。

    参数
      dwg_path   : 要打印的 DWG
      pdf_path   : 输出 PDF
      paper      : 图幅（A0..A5）；None = 按包围框尺寸自动判定
      monochrome : True 用 monochrome.ctb（黑白输出，DWG 保留颜色）
      timeout_s  : 等 PDF 落盘的最长秒数

    返回 dict：{ok, pdf, size, page_mm, paper, box, seconds, steps, error}
    """
    def emit(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    steps = []
    t0 = time.time()
    result = {'ok': False, 'pdf': pdf_path, 'steps': steps}

    try:
        acad, _ = connect(exe_hint=exe_hint, log=log)
        doc = open_or_activate(acad, dwg_path)
        steps.append('connected')
        emit(f'已连接 AutoCAD：{doc.Name}')

        if doc.ModelSpace.Count <= 0:
            result['error'] = '图纸里没有实体'
            return result

        # box 参数：显式指定打印窗口（调试排版时用 —— 视图摊在框外时
        # 默认取「最外层图框」会把框外的视图裁掉，看不到全貌）。
        if box is None:
            box = find_outermost_box(doc)
        if not box:
            result['error'] = '找不到最外层包围框（没有闭合多段线）'
            return result
        result['box'] = [round(v, 2) for v in box]
        emit(f'打印窗口：({box[0]:.1f},{box[1]:.1f})-({box[2]:.1f},{box[3]:.1f})')

        if not paper:
            paper = guess_paper(box)
        if not paper:
            result['error'] = '无法判定图幅，请显式指定 paper（A0..A5）'
            return result
        result['paper'] = paper
        emit(f'图幅：{paper}')

        _wait_plot_idle(doc)

        lay = doc.ActiveLayout
        lay.RefreshPlotDeviceInfo()
        steps.append('device_refreshed')

        lay.ConfigName = PLOT_DEVICE
        media = _pick_media_name(lay.GetCanonicalMediaNames(), paper)
        if media:
            lay.CanonicalMediaName = media
        else:
            emit(f'⚠ 没有找到 {paper} 纸张，沿用当前纸型')
        steps.append(f'media={lay.CanonicalMediaName}')

        # ★ 顺序关键：先窗口，再 PlotType
        lay.SetWindowToPlot(_vec2(box[0], box[1]), _vec2(box[2], box[3]))
        plot_type_ok = False
        for _attempt in range(3):
            try:
                lay.PlotType = 4          # 4 = acWindow
                plot_type_ok = True
                break
            except Exception:
                time.sleep(1.2)
        if not plot_type_ok:
            result['error'] = '设置打印范围失败（PlotType=acWindow 被拒绝）'
            return result
        steps.append('window_set')

        try:
            lay.CenterPlot = True
            lay.UseStandardScale = True
            lay.StandardScale = 0          # 0 = 布满图纸
            lay.PlotWithLineweights = True
            # 彩色输出时**必须关掉打印样式**，否则 CTB 会把图层颜色统一掉，
            # 出图和预览都是黑白，看不出「哪个实体在哪个图层」。
            lay.PlotWithPlotStyles = bool(monochrome)
            if monochrome:
                # 关键：DWG 里保留图层颜色，输出走纯黑
                try:
                    lay.PlotStyleName = MONO_STYLE
                    steps.append(f'style={MONO_STYLE}')
                except Exception:
                    try:
                        lay.StyleSheet = MONO_STYLE
                        steps.append(f'style={MONO_STYLE}')
                    except Exception:
                        emit('⚠ 设置黑白样式表失败，PDF 会带原色')
                        steps.append('style=default')
        except Exception as exc:
            emit(f'⚠ 部分打印参数设置失败：{exc}')

        # 输出
        os.makedirs(os.path.dirname(pdf_path) or '.', exist_ok=True)
        if os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception:
                pass

        doc.Plot.PlotToFile(pdf_path, PLOT_DEVICE)
        steps.append('submitted')
        emit('已提交打印，等待 PDF 落盘…')

        # ★ PlotToFile 是异步的：轮询等文件写完
        deadline = time.time() + timeout_s
        ok = False
        while time.time() < deadline:
            time.sleep(1.0)
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1000:
                s1 = os.path.getsize(pdf_path)
                time.sleep(1.5)
                if s1 == os.path.getsize(pdf_path):
                    ok = True
                    break

        if not ok:
            result['error'] = f'{timeout_s}s 内 PDF 没有生成'
            return result

        steps.append('written')
        result['ok'] = True
        result['size'] = os.path.getsize(pdf_path)
        result['seconds'] = round(time.time() - t0, 1)

        # 读页面尺寸（A3 应为 420 x 297 mm ±1）
        try:
            with open(pdf_path, 'rb') as fh:
                head = fh.read(16384)
            import re
            m = re.search(
                rb'/MediaBox\s*\[\s*([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)',
                head)
            if m:
                w_pt = float(m.group(3)) - float(m.group(1))
                h_pt = float(m.group(4)) - float(m.group(2))
                result['page_mm'] = [round(w_pt / 72 * 25.4, 1),
                                     round(h_pt / 72 * 25.4, 1)]
        except Exception:
            pass

        emit(f'✅ PDF 完成：{result["size"] / 1024:.1f} KB，'
             f'{result["seconds"]}s，页面 {result.get("page_mm")}')
        return result

    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        return result


def pdf_in_one_call(dwg_path, pdf_path, **kwargs):
    """给 engine 用的薄封装：返回可直接序列化的结果。"""
    return export_pdf(dwg_path, pdf_path, **kwargs)
