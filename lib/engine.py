# -*- coding: utf-8 -*-
"""
dsh-cad-draw —— SolidWorks 自动出工程图（主机侧 Python 引擎）

用 pywin32 COM 驱动本机 SolidWorks，按 GB 国标出图：
  打开模型 → GB 模板建图 → 套 GB 图框 → 单位强制毫米
  → 投标准视图(+轴测) → 自动插入模型尺寸 → 加公差/粗糙度
  → 保存 SLDDRW → 导出 DXF + DWG

═══════════ 本机实测结论（SOLIDWORKS 2020 SP3 @ F:\\sw\\SOLIDWORKS）═══════════

1) 中文版 SolidWorks 的视图名必须是本地化名：
   '*前视' '*上视' '*右视' '*等轴测'   ← 有效
   '*Front' '*Top' '*Isometric'       ← 一律返回 None
   完整候选表见 VIEW_NAMES。

2) InsertModelAnnotations3 签名固定 6 参：
   (Option, Types, AllViews, DuplicateDims, HiddenFeatureDims, UsePlacementInSketch)
   Types 必须是 8(swInsertDimensions) | 32768(swInsertDimensionsMarkedForDrawing) = 32776
   只传 8 或只传 32768 都插不进任何尺寸。返回值是插入对象的数组。

3) InsertModelAnnotations3 对“同一张图纸文档”只生效一次，重复调用返回 None。
   但一个进程内连续处理多个零件没问题（每个零件都是新图纸）。

4) 模板 gb.drwdot 自带单位是英寸(47=3)。
   必须在建视图前把工程图文档改成毫米：
     SetUserPreferenceInteger(263=UnitSystem, 0, 5=MMGS)
     SetUserPreferenceInteger(47=UnitsLinear, 0, 0=swMM)
   否则尺寸显示成 0.04 这类英寸值。

5) SetupSheet5(Name, PaperSize, TemplateIn, Scale1, Scale2, FirstAngle,
                TemplateName, Width, Height, PropertyViewName, RemoveModifiedNotes)
   PaperSize: 6=A4横 7=A4纵 8=A3 9=A2 10=A1 11=A0；Width/Height 用米。
   图框文件在 C:\\ProgramData\\SOLIDWORKS\\SOLIDWORKS 2020\\lang\\Chinese-Simplified\\sheetformat\\
   形如 'a3 - gb.slddrt'。

6) 公差：IDimension.SetToleranceType(t) + SetToleranceValues(min,max)
   t: 0=None 1=BILATERAL 2=LIMIT 3=SYMMETRIC 4=MIN 5=MAX 6=FIT 7=FIT_TOLERANCE
   实测 SetToleranceType(3)+SetToleranceValues(-0.05,0.05) 图上显示 .51/.49 生效。

7) 粗糙度：IModelDocExtension.InsertSurfaceFinishSymbol3(...) 可用，
   MaxRoughness 传 '3.2' 能画出 GB 粗糙度符号。

8) 中立格式（STEP/IGES/Parasolid）不能用 OpenDoc6 直接开：
   实测会让 SolidWorks 进程直接崩溃（RPC 失败 0x800706BE）。
   必须先另存成 .sldprt 再交给本插件。

9) 致命的自动化陷阱：用 Stop-Process -Force 强杀 SolidWorks 后，
   下次启动会弹“SOLIDWORKS 错误报告”(sldexitapp.exe) 模态框，把 COM 卡死。
   规避：HKCU\\Software\\SolidWorks\\SOLIDWORKS 2020\\SW Event Log
        'Disable Crash Diagnosis' = 1 (DWORD)
        'Send Feedback Enabled'   = 0 (DWORD)
   并且永远不要强杀 SolidWorks；本引擎只 CloseDoc，不杀进程。

10) 剖视图(CreateSectionViewAt*)与局部放大图无法纯 API 凭空生成：
    必须先在图纸上画出剖切线/圆并选中，而这些选中操作在 pywin32 晚绑定下
    SelectByID2 会报“类型不匹配”(DISP_E_TYPEMISMATCH)。
    因此本引擎默认不自动加剖视图——留给用户在 SolidWorks 里补，
    或改用预置好剖视图的模板。
"""

import json
import os
import sys
import time
import traceback

# 本目录下的应用路径解析（SolidWorks / AutoCAD 启动路径）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apps  # noqa: E402

# ---------------- SolidWorks 常量（手写，避免依赖 interop dll） ----------------
SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2
SW_DOC_DRAWING = 3

SW_INSERT_DIMENSIONS = 8
SW_INSERT_DIMENSIONS_MARKED = 32768
DEFAULT_ANNOTATION_TYPES = SW_INSERT_DIMENSIONS | SW_INSERT_DIMENSIONS_MARKED  # 32776

# swDwgPaperSizes_e：6=A4横 7=A4纵 8=A3 9=A2 10=A1 11=A0
# A5 没有独立枚举，用 swDwgPapersUserDefined=12 + 实际宽高
PAPER = {
    ('A5', True): 12, ('A5', False): 12,
    ('A4', True): 6, ('A4', False): 7,
    ('A3', True): 8, ('A3', False): 8,
    ('A2', True): 9, ('A2', False): 9,
    ('A1', True): 10, ('A1', False): 10,
    ('A0', True): 11, ('A0', False): 11,
}
# 图幅实际尺寸（米），横向
PAPER_SIZE_M = {
    'A5': (0.210, 0.148), 'A4': (0.297, 0.210), 'A3': (0.420, 0.297),
    'A2': (0.594, 0.420), 'A1': (0.841, 0.594), 'A0': (1.189, 0.841),
}
# 可选图幅（给 UI 与参数校验用）
PAPER_SIZES = ['A5', 'A4', 'A3', 'A2', 'A1', 'A0']

# 中文版 SolidWorks 的本地化视图名
VIEW_NAMES = {
    'front': '*前视', 'top': '*上视', 'right': '*右视',
    'left': '*左视', 'back': '*后视', 'bottom': '*下视',
    'isometric': '*等轴测',
}

PREF_UNITS_LINEAR = 47      # 0=mm 1=cm 2=m 3=inch
PREF_UNITS_DECIMALS = 49
PREF_UNIT_SYSTEM = 263      # 5=MMGS

TOLERANCE_TYPES = {
    'none': 0, 'bilateral': 1, 'limit': 2, 'symmetric': 3,
    'min': 4, 'max': 5, 'fit': 6, 'fit_tolerance': 7,
}

DEFAULT_TEMPLATE = r'F:\sw\SOLIDWORKS\data\templates\gb.drwdot'
SHEETFORMAT_DIR = r'C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2020\lang\Chinese-Simplified\sheetformat'
# 装配图明细表（BOM）的表格模板
BOM_TEMPLATE = r'F:\sw\SOLIDWORKS\lang\chinese-simplified\bom-standard.sldbomtbt'

NEUTRAL_EXTS = ('.step', '.stp', '.iges', '.igs', '.x_t', '.x_b', '.sat')


def emit(obj):
    sys.stdout.write('###DSH_CAD###' + json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def find_sheet_format(paper, landscape=True):
    cand = os.path.join(SHEETFORMAT_DIR, f'{paper.lower()} - gb.slddrt')
    if os.path.exists(cand):
        return cand
    alt = os.path.join(SHEETFORMAT_DIR,
                       f'{paper.lower()} - {"landscape" if landscape else "portrait"}.slddrt')
    return alt if os.path.exists(alt) else None


def kill_report_dialogs():
    """清掉可能残留的“SOLIDWORKS 错误报告”窗口进程。"""
    try:
        import subprocess
        subprocess.run(['taskkill', '/F', '/IM', 'sldexitapp.exe'],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        pass


def connect(visible=True, exe_hint=None, log=None):
    """连上 SolidWorks。

    exe_hint：用户/配置指定的 SLDWORKS.exe 路径（留空自动探测）。
    显式路径的意义：多版本共存或装在非默认盘时，Dispatch 可能连到别的版本，
    或者干脆起不来。先按路径把进程拉起来，再 Dispatch 连运行实例最稳。
    """
    import win32com.client as win32

    exe, source, msg = apps.ensure_running(
        apps.SOLIDWORKS, exe_hint, log=log)
    if log:
        try:
            log(f'SolidWorks 路径: {exe or "未找到"}（{source}）')
        except Exception:
            pass

    sw = win32.Dispatch('SldWorks.Application')
    try:
        sw.Visible = bool(visible)
    except Exception:
        pass
    for _ in range(90):
        try:
            _ = sw.RevisionNumber
            return sw
        except Exception:
            time.sleep(1)
    return sw


def sweep_documents(sw, keep=None):
    """清掉 SolidWorks 里残留的工程图文档。

    ⚠ 这是必须做的！实测踩过：
    每次出图都会新建一个工程图文档，出完不关就一直累积（跑了 8 次就挂了
    8 个文档）。文档一多，InsertModelAnnotations3 会认错「当前图纸」，
    返回 0 个标注且不报错——比报错难查得多。

    keep: 要保留的文档标题（本次要用的零件）
    返回关闭的文档数。
    """
    closed = 0
    try:
        doc = sw.GetFirstDocument
        titles = []
        guard = 0
        while doc is not None and guard < 200:
            try:
                titles.append((doc.GetTitle, doc.GetType))
            except Exception:
                pass
            doc = doc.GetNext
            guard += 1

        for title, doc_type in titles:
            if keep and title == keep:
                continue
            # 只关工程图（type=3），零件留着不影响
            if doc_type == SW_DOC_DRAWING:
                try:
                    sw.CloseDoc(title)
                    closed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return closed


def insert_bom_table(dw, paper, landscape=True, template=None,
                     anchor=3, bom_type=1):
    """在装配图的第一个视图上插入明细表（BOM）。

    ⚠ API 签名（从 sldworks.tlb 读出，别凭记忆）：
        IView::InsertBomTable3(
            UseAnchorPoint: bool,      ← 不是模板路径！
            X: double, Y: double,      ← 米
            AnchorType: int,           ← 0左上 1右上 2左下 3右下
            BomType: int,              ← 0仅零件 1仅顶层 2缩进
            Configuration: str,
            TableTemplate: str,        ← BOM 表格模板 .sldbomtbt
            Hidden: bool)
        返回 IBomTableAnnotation

    GB 习惯：明细表放在标题栏正上方，从下往上排。
    """
    tpl = template if (template and os.path.isfile(template)) else BOM_TEMPLATE
    pw, ph = PAPER_SIZE_M.get(str(paper).upper(), (0.420, 0.297))
    if not landscape:
        pw, ph = ph, pw

    # 第一个实体视图（跳过图纸本身）
    view = None
    v = dw.GetFirstView
    i = 0
    while v is not None and i < 10:
        if i > 0:
            view = v
            break
        v = v.GetNextView
        i += 1
    if view is None:
        return {'ok': False, 'error': '没有可用视图，无法插入明细表'}

    # 标题栏上方、靠右。
    # ⚠ AnchorType=3(右下) 时 X 给的是**左下角**，明细表会从 X 向右铺，
    # 给 315mm 就直接伸出图框（实测 PDF 宽度 483mm > 420）。
    # 所以 X 要按「表格宽度 」往回让：GB 明细表约 180mm 宽。
    x = max(0.030, pw - 0.185)
    y = 0.075
    try:
        tbl = view.InsertBomTable3(False, x, y, int(anchor), int(bom_type),
                                   '', tpl, False)
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                'template': tpl}

    if tbl is None:
        return {'ok': False, 'error': 'InsertBomTable3 返回空',
                'template': tpl}

    rows = None
    try:
        rows = int(tbl.RowCount)
    except Exception:
        pass
    return {'ok': True, 'template': tpl, 'atMm': [round(x * 1000, 1),
                                                  round(y * 1000, 1)],
            'rows': rows}


def assembly_components(asm_doc):
    """列出装配体里用到的零件文件（按路径去重）。

    用 GetComponents(False) 拿**所有层级**的组件（含子装配里的零件），
    同一个零件装多个实例只保留一份——出零件图时每份零件只需要一张图。

    返回 [{'path','name','instances'}]，按出现顺序排列。
    """
    out = []
    index = {}
    try:
        comps = asm_doc.GetComponents(False)
    except Exception as exc:
        return {'ok': False, 'error': f'GetComponents 失败: {exc}', 'parts': []}

    for c in comps or []:
        try:
            path = str(c.GetPathName or '')
        except Exception:
            path = ''
        try:
            name = str(c.Name2 or '')
        except Exception:
            name = ''
        if not path:
            continue
        # 排除虚拟件/ toolbox 里没有实际文件的组件
        if not os.path.isfile(path):
            continue
        # 子装配本身也带路径，但它是 .sldasm，零件图只画 .sldprt
        if os.path.splitext(path)[1].lower() != '.sldprt':
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in index:
            out[index[key]]['instances'] += 1
            continue
        index[key] = len(out)
        out.append({'path': path, 'name': name, 'instances': 1})

    return {'ok': True, 'parts': out,
            'count': len(out),
            'totalInstances': sum(p['instances'] for p in out)}


def insert_balloons(dw, layout=1, style=1, size=2,
                    upper_content=0, lower_content=0,
                    upper_text='', lower_text='', layername='',
                    to_faces=False, ignore_multiple=True):
    """在装配图上自动加零件序号（气泡）。

    ⚠ 两个坑（都实测过）：
      1. AutoBalloon4 **必须先选中视图**，否则直接返回 None，不报错。
      2. 选中视图的 SelectByID2 第 8 个参数 Callout 不能传 None，
         必须传 pythoncom.Empty，否则报「类型不匹配 (None, 8)」。

    签名（从 sldworks.tlb 读，注意顺序，第一个是 Layout 不是 Style）：
        IDrawingDoc::AutoBalloon4(
            Layout, IgnoreMultiple, Style, Size,
            UpperTextContent, UpperText, LowerTextContent, LowerText,
            Layername, BalloonsToFaces)

    Layout（swBalloonLayoutType_e）：1=圆形/自动 2=方形…
    Style （swBalloonStyle_e）  ：1=圆形 2=三角 3=六角 4=方框
    Size  （swBalloonFit_e）    ：0=紧密 1=1字符 2=2字符 3=3字符

    返回 {'ok','selected','result','error'}
    """
    import pythoncom

    # 收集视图 + 轮廓中心（按坐标点选比按名字更可靠）
    targets = []
    v = dw.GetFirstView
    i = 0
    while v is not None and i < 20:
        if i > 0:
            try:
                nm = str(v.GetName2)
                o = v.GetOutline
                cx = (float(o[0]) + float(o[2])) / 2.0
                cy = (float(o[1]) + float(o[3])) / 2.0
                targets.append((nm, cx, cy))
            except Exception:
                pass
        v = v.GetNextView
        i += 1

    if not targets:
        return {'ok': False, 'error': '没有可用视图', 'selected': 0}

    try:
        dw.ClearSelection2(True)
    except Exception:
        pass

    # ★★★ 本鱼调了一晚上才试出来的关键 ★★★
    # SelectByID2 的第 8 个参数 Callout 必须是「**显式的空 IDispatch 指针**」：
    #     VARIANT(pythoncom.VT_DISPATCH, None)
    # 传 None / pythoncom.Empty / 0 全都会报「类型不匹配 (None, 8)」，
    # 因为它们是 VT_EMPTY / VT_I4，不是 VT_DISPATCH。
    # 另外 IView 对象上**没有** Select/Select2 方法，只能走 SelectByID2。
    from win32com.client import VARIANT
    null_disp = VARIANT(pythoncom.VT_DISPATCH, None)

    selected = 0
    for name, cx, cy in targets:
        ok = False
        try:
            ok = bool(dw.Extension.SelectByID2(
                name, 'DRAWINGVIEW', cx, cy, 0, True, 0, null_disp, 0))
        except Exception:
            ok = False
        if ok:
            selected += 1

    if selected == 0:
        return {'ok': False, 'error': '视图一个都没选中', 'selected': 0}

    try:
        r = dw.AutoBalloon4(
            int(layout),                 # Layout
            bool(ignore_multiple),       # IgnoreMultiple
            int(style),                  # Style
            int(size),                   # Size
            int(upper_content), upper_text,
            int(lower_content), lower_text,
            str(layername),
            bool(to_faces))
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                'selected': selected}

    return {'ok': bool(r), 'selected': selected, 'result': r,
            'views': len(targets)}


def open_model(sw, path):
    """按扩展名用正确的文档类型打开。

    ⚠ 踩过的坑：原来只区分 `.sldasm` 和「其它一律当零件」，
    结果打开 `.SLDDRW` 时用零件类型去开，返回 None，
    报错只是 AttributeError: 'NoneType' object has no attribute 'GetTitle'。
    """
    from win32com.client import VARIANT
    from pythoncom import VT_BYREF, VT_I4
    ext = os.path.splitext(path)[1].lower()
    if ext == '.sldasm':
        doc_type = SW_DOC_ASSEMBLY
    elif ext == '.slddrw':
        doc_type = SW_DOC_DRAWING
    else:
        doc_type = SW_DOC_PART
    errs = VARIANT(VT_BYREF | VT_I4, 0)
    warns = VARIANT(VT_BYREF | VT_I4, 0)
    doc = sw.OpenDoc6(path, doc_type, 1, '', errs, warns)
    return doc, errs.value, warns.value


def make_drawing(sw, template, paper, landscape, sheet_format, scale_num=1, scale_den=1):
    dw = sw.NewDocument(template, 0, 0, 0)
    if dw is None:
        raise RuntimeError(f'无法用模板新建工程图: {template}')

    doc = None
    for _ in range(40):
        time.sleep(0.5)
        doc = sw.ActiveDoc
        if doc is not None and doc.GetType == SW_DOC_DRAWING:
            break
    if doc is None or doc.GetType != SW_DOC_DRAWING:
        doc = dw

    paper_code = PAPER.get((paper.upper(), bool(landscape)), 6)
    w_m, h_m = PAPER_SIZE_M.get(paper.upper(), (0.297, 0.210))
    if not landscape:
        w_m, h_m = h_m, w_m

    sheet = doc.GetCurrentSheet
    fmt = sheet_format or find_sheet_format(paper.upper(), landscape)
    # Scale1/Scale2 = 图纸比例 1:den  ->  Scale1/Scale2
    doc.SetupSheet5(sheet.GetName, paper_code, 12,
                    float(scale_num), float(scale_den), True,
                    fmt or '', w_m, h_m, '', True)

    # 单位强制毫米（必须在建视图之前）
    doc.Extension.SetUserPreferenceInteger(PREF_UNIT_SYSTEM, 0, 5)
    doc.Extension.SetUserPreferenceInteger(PREF_UNITS_LINEAR, 0, 0)
    doc.Extension.SetUserPreferenceInteger(PREF_UNITS_DECIMALS, 0, 2)

    # GB 标注样式（字高/箭头/线宽）
    try:
        apply_gb_style(doc, 3.5)
    except Exception:
        pass
    return doc


DEFAULT_LAYOUT = {
    'front': (0.10, 0.20),
    'top': (0.10, 0.09),
    'right': (0.22, 0.20),
    'isometric': (0.33, 0.20),
}

# 图幅可画区（米），留出图框和标题栏边距
PAPER_DRAWABLE = {
    'A4': (0.277, 0.190), 'A3': (0.400, 0.277), 'A2': (0.574, 0.400),
    'A1': (0.821, 0.574), 'A0': (1.169, 0.821),
}


def get_model_size(model_doc):
    """取模型包围盒（米）——用来算合适的视图比例和摆放位置。"""
    try:
        ext = model_doc.Extension
        box = ext.GetBox if False else None
        # IModelDoc2.GetPartBox 返回 [xmin,ymin,zmin,xmax,ymax,zmax]
        b = model_doc.GetPartBox(True)
        if b and len(b) >= 6:
            dx = abs(b[3] - b[0])
            dy = abs(b[4] - b[1])
            dz = abs(b[5] - b[2])
            return (dx, dy, dz)
    except Exception:
        pass
    return None


GB_SCALES = [1, 2, 2.5, 5, 10, 20, 50, 100, 200, 500]
GB_ZOOMS = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]


# GB/T 4458.1 优先比例：缩小 1:1.5 1:2 1:2.5 1:3 1:4 1:5 1:6 1:10 1:15 1:20 …
#                  放大 2:1 2.5:1 5:1 10:1 …
GB_SCALES = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 10, 15, 20, 25, 50, 100, 200, 500, 1000]
GB_ZOOMS = [1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]

# 图框内边距与标题栏（GB/T 14689：左边距 25mm 装订边，其余 5mm；标题栏高约 55mm）
FRAME_LEFT = 0.025
FRAME_OTHER = 0.005
TITLE_H = 0.055
# 视图之间的最小间隙（给尺寸标注留位置）
VIEW_GAP = 0.018
# 尺寸标注向外溢出的经验余量系数（一圈尺寸约 12-18mm）
ANNOT_PAD = 1.55


def drawable_area(paper, landscape=True):
    """返回图框内可画区（米）：(x0, y0, w, h)。

    扣除 GB 装订边、图框边距和右下角标题栏。
    """
    pw, ph = PAPER_SIZE_M.get(str(paper).upper(), (0.420, 0.297))
    if not landscape:
        pw, ph = ph, pw
    x0 = FRAME_LEFT
    y0 = FRAME_OTHER
    w = pw - FRAME_LEFT - FRAME_OTHER
    h = ph - FRAME_OTHER * 2 - TITLE_H
    return x0, y0, max(w, 0.05), max(h, 0.05)


def pick_gb_scale(model_size, paper, landscape=True):
    """按 GB/T 4458.1 优先比例选缩放——**先量零件，再定比例**。

    算法思路（回答「是不是算法不对」）：
      1) 取模型真实包围盒 长(L) 宽(W) 高(H)
      2) 三视图 + 轴测要按国标「长对正、高平齐、宽相等」摆放，
         占用的总版面约为：
             宽方向 = L + W   （主视 + 左视并排）
             高方向 = H + W   （主视 + 俯视上下）
         再乘标注余量 ANNOT_PAD
      3) 用这个总版面去比可画区，取能放下的**最大**优先比例

    这样比逐个视图面积估算更准，因为投影布局本来就是整体占用。

    返回 (scale_num, scale_den)：比例 = num : den
    """
    x0, y0, draw_w, draw_h = drawable_area(paper, landscape)

    if not model_size or len(model_size) < 3:
        return 1, 1

    L = abs(model_size[0])   # 长（X）
    W = abs(model_size[1])   # 宽（Y）
    H = abs(model_size[2])   # 高（Z）

    # 三视图整体占用（含视图间隙）
    total_w_units = L + W + VIEW_GAP
    total_h_units = H + W + VIEW_GAP

    def fits(ratio):
        """ratio = 图上长度 / 实物长度"""
        w = total_w_units * ratio * ANNOT_PAD
        h = total_h_units * ratio * ANNOT_PAD
        return w <= draw_w and h <= draw_h

    # 先试放大（小零件），再从 1:1 开始缩小，取第一个能放下的
    for z in GB_ZOOMS:
        num = int(round(z * 10))
        if fits(z):
            return num, 10
    for den in GB_SCALES:
        if fits(1.0 / den):
            return 1, den
    return 1, 1000


def compute_layout(model_size, paper, scale_den, landscape=True):
    """按国标投影规律计算三视图 + 轴测图的位置。

    GB/T 4458.1 第一角投影的配置要求：
        主视图  左视图          ← 高平齐（同一水平线）
        俯视图  轴测图          ← 长对正（与主视图竖直对齐）

    返回 {view_key: (x, y)}，坐标是**视图中心点**（米）。
    """
    x0, y0, draw_w, draw_h = drawable_area(paper, landscape)

    ratio = 1.0 / max(float(scale_den), 1e-9)
    if model_size and len(model_size) >= 3:
        L = abs(model_size[0]) * ratio
        W = abs(model_size[1]) * ratio
        H = abs(model_size[2]) * ratio
    else:
        L = W = H = 0.05

    gap = VIEW_GAP
    # 列宽：第 1 列放主视/俯视（宽=L），第 2 列放左视/轴测（宽=W）
    col1 = max(L, 0.02)
    col2 = max(W, 0.02)
    # 行高：第 1 行放主视/左视（高=H），第 2 行放俯视/轴测（高=W）
    row1 = max(H, 0.02)
    row2 = max(W, 0.02)

    block_w = col1 + gap + col2
    block_h = row1 + gap + row2

    # 整块在可画区内居中（略微偏上，给底部尺寸留空间）
    start_x = x0 + max((draw_w - block_w) / 2.0, 0.0)
    start_y = y0 + max((draw_h - block_h) / 2.0, 0.0) + block_h * 0.05

    cx1 = start_x + col1 / 2.0
    cx2 = start_x + col1 + gap + col2 / 2.0
    cy1 = start_y + block_h - row1 / 2.0
    cy2 = start_y + row2 / 2.0

    return {
        # 主视图左上、左视图右上（高平齐）
        'front': (cx1, cy1),
        'right': (cx2, cy1),
        # 俯视图在主视图正下方（长对正）、轴测图在右下
        'top': (cx1, cy2),
        'isometric': (cx2, cy2),
        '_size': (L, W, H),
        '_gap': gap,
    }


def add_section_view(doc, ref_view, label='A', direction='vertical',
                     cut_extend=0.030, place=None):
    """自动加一个剖视图（在参考视图上画剖切线再剖切）。

    关键点（踩过）：SelectByID2 第 8 个参数 Callout 必须传
    VARIANT(VT_DISPATCH, None)，传 0 或 None 会报「类型不匹配」。
    """
    from win32com.client import VARIANT
    from pythoncom import VT_DISPATCH

    if ref_view is None:
        return None
    try:
        out = ref_view.GetOutline
        cx = (out[0] + out[2]) / 2.0
        cy = (out[1] + out[3]) / 2.0
    except Exception:
        return None

    empty_callout = VARIANT(VT_DISPATCH, None)
    try:
        doc.InsertSketch2(True)
        sm = doc.SketchManager
        if direction == 'horizontal':
            sm.CreateLine(cx - cut_extend * 1.5, cy, 0, cx + cut_extend * 1.5, cy, 0)
        else:
            sm.CreateLine(cx, cy - cut_extend, 0, cx, cy + cut_extend, 0)
        doc.InsertSketch2(True)
        time.sleep(0.8)

        doc.ClearSelection2(True)
        picked = False
        for name in ('Line1', ''):
            try:
                if doc.Extension.SelectByID2(name, 'SKETCHSEGMENT',
                                             cx, cy, 0, False, 0,
                                             empty_callout, 0):
                    picked = True
                    break
            except Exception:
                continue
        if not picked:
            return None

        px, py = place if place else (cx, cy - 0.10)
        return doc.CreateSectionViewAt5(px, py, 0, label, 0, None, 0)
    except Exception:
        return None
    finally:
        try:
            doc.ClearSelection2(True)
            doc.Extension.SelectByID2('草图1', 'SKETCH', 0, 0, 0,
                                      False, 0, empty_callout, 0)
            doc.EditDelete()
            doc.ClearSelection2(True)
        except Exception:
            pass


def add_detail_view(doc, ref_view, label='A', scale=2.0, radius=0.020, place=None):
    """自动加局部放大图（先在参考视图上画圆，选中后放大）。

    radius 是放大区在图上的半径（米），scale 是放大倍数。
    """
    from win32com.client import VARIANT
    from pythoncom import VT_DISPATCH

    if ref_view is None:
        return None
    try:
        out = ref_view.GetOutline
        cx = (out[0] + out[2]) / 2.0
        cy = (out[1] + out[3]) / 2.0
    except Exception:
        return None

    empty_callout = VARIANT(VT_DISPATCH, None)
    try:
        doc.InsertSketch2(True)
        doc.SketchManager.CreateCircleByRadius(cx, cy, 0, radius)
        doc.InsertSketch2(True)
        time.sleep(0.8)

        doc.ClearSelection2(True)
        picked = doc.Extension.SelectByID2('Arc1', 'SKETCHSEGMENT',
                                           cx, cy, 0, False, 0,
                                           empty_callout, 0)
        if not picked:
            return None

        px, py = place if place else (cx + 0.16, cy - 0.11)
        # CreateDetailViewAt4(X,Y,Z, Style, Scale1, Scale2, Label, Showtype,
        #                     FullOutline, JaggedOutline, NoOutline, ShapeIntensity)
        return doc.CreateDetailViewAt4(px, py, 0, 0, float(scale), 1.0,
                                       label, 0, True, False, False, 0)
    except Exception:
        return None
    finally:
        try:
            doc.ClearSelection2(True)
            doc.Extension.SelectByID2('Circle1', 'SKETCH', 0, 0, 0,
                                      False, 0, empty_callout, 0)
            doc.EditDelete()
            doc.ClearSelection2(True)
        except Exception:
            pass


def view_has_hidden_features(view):
    """粗略判断视图里是否有「看不见的内部结构」——用隐藏线数量做代理指标。

    国标里该加剖视图的典型情形：内部有孔/槽/空腔，外形轮廓看不出壁厚。
    这里用 IsHiddenLines / 隐藏线条数无法直接从 API 稳定取到，
    改为「模型是否含内腔」这种更可靠的信号见 model_needs_section()。
    """
    try:
        # 视图上有隐藏线，往往说明内部有结构
        return bool(view.GetHiddenLinesVisibility)
    except Exception:
        return False


def analyze_model_type(model_doc):
    """判断模型是「参数化模型」还是「导入实体(dumb solid)」。

    这个判断很关键，因为两者的自动标注能力完全不同：

      参数化模型（.sldprt 原生）
        特征树里有草图 + 拉伸/切除等，自带设计尺寸，
        InsertModelAnnotations3 能插入大量尺寸。

      导入实体（STEP/IGES 转来的）
        特征树只有一个 MBimport/Imported 特征，草图数 = 0，
        **没有任何设计尺寸可插**，InsertModelAnnotations3 只能插出 1 个左右。
        实测：不锈钢丝固定片底座.step.SLDPRT = 144面/450边/0草图 → 仅 1 个尺寸。

    返回 dict:
      kind        'parametric' | 'imported' | 'unknown'
      sketches    草图特征数
      features    建模特征数（拉伸/切除等）
      importName  导入特征名（若为导入）
      faces/edges 面/边数量
      note        给用户的中文说明
    """
    info = {'kind': 'unknown', 'sketches': 0, 'features': 0,
            'importName': '', 'faces': 0, 'edges': 0, 'note': ''}
    try:
        f = model_doc.FirstFeature
        guard = 0
        while f is not None and guard < 500:
            try:
                t = str(f.GetTypeName2)
                low = t.lower()
                if t == 'ProfileFeature':
                    info['sketches'] += 1
                elif ('extrusion' in low or 'cut' in low or 'revolve' in low
                      or 'hole' in low or 'fillet' in low or 'chamfer' in low
                      or t == 'ICE'):
                    info['features'] += 1
                elif ('import' in low or 'mbimport' in low
                      or low.startswith('step')):
                    info['importName'] = str(f.Name)
            except Exception:
                pass
            f = f.GetNextFeature
            guard += 1
    except Exception:
        pass

    # 面/边数量（导入实体通常面数很多）
    try:
        bodies = model_doc.GetBodies2(0, True)
        if bodies:
            body = bodies[0]
            faces = body.GetFaces()
            edges = body.GetEdges()
            info['faces'] = len(faces) if faces else 0
            info['edges'] = len(edges) if edges else 0
    except Exception:
        pass

    if info['importName'] and info['sketches'] == 0:
        info['kind'] = 'imported'
        info['note'] = (
            f'这是 STEP/IGES 导入的实体（特征：{info["importName"]}，'
            f'{info["faces"]} 面 / {info["edges"]} 边，草图 0 个）。'
            '导入实体没有设计尺寸，自动标注只能插出极少量尺寸；'
            '要得到完整标注，需在 SolidWorks 里用「特征识别(FeatureWorks)」'
            '或重新建模成参数化零件。')
    elif info['sketches'] > 0 or info['features'] > 0:
        info['kind'] = 'parametric'
        info['note'] = (f'参数化模型：{info["sketches"]} 个草图、'
                        f'{info["features"]} 个建模特征，可正常自动标注。')
    else:
        info['note'] = '未能判定模型类型。'
    return info


def detect_internal_cavity(model_doc):
    """检测模型是否存在「从外面看不见的内部空腔」，决定要不要剖视。

    为什么不能只看有没有切除特征：
      切除-拉伸 也可能只是在外表面挖个槽，剖视没必要。
      而真正的内腔（比如杯子的内胆、阀体的流道）三视图完全看不到壁厚。

    这里用三个证据综合判断：

      证据 1  抽壳特征(shell)
              抽壳几乎必然产生内腔 → 直接判定需要剖视

      证据 2  体积占比 = 实体体积 / 包围盒体积
              实心件通常 >55%；中空/薄壁件会明显偏低
              ⚠ 局限：薄板件也低，所以它只是辅助证据，不能单独定案

      证据 3  切除类特征数量（切除/孔/旋转切除/扫描切除）
              数量多说明形状有内部结构

    返回 dict:
      needSection  是否建议剖视
      reason       中文依据
      volumeRatio  体积占比（0~1）
      volumeMm3    实体体积
      cutCount     切除类特征数
      hasShell     是否有抽壳
    """
    info = {'needSection': False, 'reason': '', 'volumeRatio': None,
            'volumeMm3': None, 'cutCount': 0, 'hasShell': False,
            'cutNames': []}

    # ---- 证据 3 + 1：特征扫描 ----
    cut_count = 0
    has_shell = False
    names = []
    try:
        feat = model_doc.FirstFeature
        guard = 0
        while feat is not None and guard < 800:
            try:
                type_name = str(feat.GetTypeName2)
            except Exception:
                type_name = ''
            try:
                feat_name = str(feat.Name)
            except Exception:
                feat_name = ''
            low = (type_name + ' ' + feat_name).lower()

            if 'shell' in low or '抽壳' in low:
                has_shell = True
                names.append(feat_name)
                cut_count += 1
            elif ('cut' in low or 'hole' in low or 'wizard' in low
                  or 'counterbore' in low
                  or '切除' in low or '孔' in low):
                cut_count += 1
                names.append(feat_name)
            feat = feat.GetNextFeature
            guard += 1
    except Exception:
        pass

    info['cutCount'] = cut_count
    info['hasShell'] = has_shell
    info['cutNames'] = names[:8]

    # ---- 证据 2：体积占比 ----
    # 注意：SolidWorks API 一律用「米」，GetMassProperties 返回数组
    #   索引 0..2 质心，索引 3 体积(m^3)，索引 4 表面积(m^2)
    try:
        bodies = model_doc.GetBodies2(0, True)
        if bodies:
            mp = bodies[0].GetMassProperties(1)
            vol_m3 = float(mp[3])
            box = model_doc.GetPartBox(True)
            if box:
                dx = abs(box[3] - box[0])
                dy = abs(box[4] - box[1])
                dz = abs(box[5] - box[2])
                bbox = dx * dy * dz
                if bbox > 0:
                    info['volumeRatio'] = vol_m3 / bbox
                    info['volumeMm3'] = vol_m3 * 1e9
    except Exception:
        pass

    ratio = info['volumeRatio']

    # ---- 综合判定 ----
    if has_shell:
        info['needSection'] = True
        info['reason'] = (f'检出抽壳特征，必然有内腔，需剖视表达壁厚'
                          f'（体积占比 {(ratio*100):.1f}%）' if ratio
                          else '检出抽壳特征，必然有内腔，需剖视表达壁厚')
    elif cut_count > 0 and ratio is not None and ratio < 0.45:
        info['needSection'] = True
        info['reason'] = (f'检出 {cut_count} 个切除/孔类特征，'
                          f'且实体只占包围盒 {ratio*100:.1f}%，'
                          f'判定为中空结构，需剖视')
    elif cut_count > 0:
        info['needSection'] = True
        info['reason'] = (f'检出 {cut_count} 个切除/孔类特征'
                          f'（{", ".join(names[:3])}），存在内部结构，建议剖视')
    else:
        info['needSection'] = False
        info['reason'] = '无切除/孔/抽壳特征，属外形件，按国标无需剖视'

    return info


def model_needs_section(model_doc):
    """兼容旧接口：返回 (是否需要剖视, 切除特征数, 特征名列表)。"""
    info = detect_internal_cavity(model_doc)
    return info['needSection'], info['cutCount'], info['cutNames'][:6]


def spread_views_outside(doc, paper, landscape, gap_factor=1.8, margin_m=0.040):
    """把全部视图摊到图框外面，排成一行，间隔足够大。

    —— 用户要求：「拉的间距大胆一点，选中一个视图最起码拉到
       一个视图长度以上的间隔」，而且可以就在图框外操作。

    为什么要摊到框外：
      1) 视图在框内时，SolidWorks 的投影对齐约束会让它们互相牵制，
         挪一个动一串，永远拉不开（实测两个视图死活挤在一起）。
      2) 摊到框外后每个视图周围都是空白，GetOutline 拿到的边界
         不再互相重叠，后续在 AutoCAD 里按层移动/分层也干净。
      3) 间距按「相邻两视图较大者的 gap_factor 倍」算，
         默认 1.8 倍，即至少留一个视图那么宽的空档。

    摆放：图框正上方一行，从左往右。
    返回 {viewName: (x_mm, y_mm)}
    """
    from win32com.client import VARIANT
    import pythoncom

    def arr2(a, b):
        return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                       (float(a), float(b)))

    pw, ph = PAPER_SIZE_M.get(str(paper).upper(), (0.420, 0.297))
    if not landscape:
        pw, ph = ph, pw

    items = []
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 20:
        if i > 0:
            try:
                o = v.GetOutline
                items.append({'view': v, 'name': str(v.GetName2),
                              'w': abs(o[2] - o[0]) or 0.05,
                              'h': abs(o[3] - o[1]) or 0.05})
            except Exception:
                pass
        v = v.GetNextView
        i += 1

    if not items:
        return {}

    # 起始位置：图框正上方
    max_h = max(t['h'] for t in items)
    y = ph + margin_m + max_h / 2.0
    x = margin_m

    placed = {}
    for t in items:
        tx = x + t['w'] / 2.0
        # ⚠ 必须先解除投影对齐（长对正/高平齐）再设位置。
        # Create1stAngleViews2 建的视图带对齐约束，直接 Position 会被
        # 拉回去：实测俯视图设到 x=313.5，GetOutline 仍报 x=74，
        # 图面上两个视图重合在一起。IView.RemoveAlignment 是解开它的钥匙。
        try:
            t['view'].RemoveAlignment()
        except Exception:
            pass
        ok = False
        try:
            t['view'].Position = arr2(tx, y)
            ok = True
        except Exception:
            ok = False
        if ok:
            placed[t['name']] = (round(tx * 1000, 1), round(y * 1000, 1))
        # 下一个：至少留「本视图或下一视图较大者的 gap_factor 倍」
        x += t['w'] + t['w'] * gap_factor
    # 导致后续按框分图层时两个视图互相错配，有个视图层直接空了。
    try:
        doc.EditRebuild3()
        doc.GraphicsRedraw2()
    except Exception:
        pass
    try:
        import time as _t
        _t.sleep(0.8)
        doc.EditRebuild3()
        _t.sleep(0.4)
    except Exception:
        pass
    return placed

def collect_view_boxes(doc, pad_m=0.004):
    """收集每个工程图视图的矩形边界（毫米），交给 AutoCAD 画框+排位。

    实测：在 SolidWorks 图纸上画的草图矩形不会随 DWG 导出
    （导出后 DSH_VIEWBOX 层上一个实体都没有），所以框必须在 AutoCAD 里画。
    这里只负责把「视图边界」这个数据报告出去。

    pad_m: 边界外扩量，留一点余量包住尺寸线/中心线。

    返回 [{'name': str, 'x0': mm, 'y0': mm, 'x1': mm, 'y1': mm}, ...]
    """
    out = []
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 20:
        if i > 0:
            try:
                o = v.GetOutline
                out.append({
                    'name': str(v.GetName2),
                    'x0': round((o[0] - pad_m) * 1000.0, 2),
                    'y0': round((o[1] - pad_m) * 1000.0, 2),
                    'x1': round((o[2] + pad_m) * 1000.0, 2),
                    'y1': round((o[3] + pad_m) * 1000.0, 2),
                })
            except Exception:
                pass
        v = v.GetNextView
        i += 1
    return out


def position_views_gb(doc, paper, landscape, title_h_m=0.055,
                      gap_m=0.030, margin_m=0.030):
    """在 SolidWorks 里把各视图拉开、摆到国标位置。

    —— 用户要求：「在 SolidWorks 上就要把各个视图分开，而且距离拉远点」

    ⚠ 关键坑（实测）：IView.Position 必须传 VARIANT 数组，
    直接写 v.Position = (x, y) 虽然不报错，但**所有视图都会变成同一个位置**
    （实测三个视图全被设成 (0,130)，图面上挤成一团）。
    正确写法：
        v.Position = VARIANT(VT_ARRAY | VT_R8, (x, y))

    布局（GB/T 4458.1 第一角投影）：
        主视  左视     ← 高平齐（同一 y）
        俯视  轴测     ← 长对正（主↔俯 同一 x）

    间距由 gap_m 控制，默认 30mm，比 SolidWorks 自动摆放宽得多。

    返回 {viewName: (x_mm, y_mm)}
    """
    from win32com.client import VARIANT
    import pythoncom

    def arr2(a, b):
        return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                       (float(a), float(b)))

    pw, ph = PAPER_SIZE_M.get(str(paper).upper(), (0.420, 0.297))
    if not landscape:
        pw, ph = ph, pw

    # 收集视图及其尺寸
    items = []
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 20:
        if i > 0:
            try:
                o = v.GetOutline
                items.append({'view': v, 'name': str(v.GetName2),
                              'w': abs(o[2] - o[0]), 'h': abs(o[3] - o[1]),
                              'cx': (o[0] + o[2]) / 2.0,
                              'cy': (o[1] + o[3]) / 2.0})
            except Exception:
                pass
        v = v.GetNextView
        i += 1

    if not items:
        return {}

    # 轴测图单独放右边。
    # ⚠ 坑：Create1stAngleViews2 建的视图名是「工程图视图1..4」，
    # 名字里根本没有「轴测」字样，靠名字识别会全部失败（实测 iso=[]，
    # 四个视图全被当成标准视图，槽位冲突后有个视图没被移动）。
    # 组合判断：①名字带轴测 ②视图>3 时最后一个是补建的轴测 ③宽高比接近方形且最大
    iso = [t for t in items
           if any(k in t['name'] for k in ('等轴测', '轴测'))
           or 'isometric' in t['name'].lower()]
    if not iso and items:
        if len(items) > 3:
            iso = [items[-1]]
        else:
            biggest = max(items, key=lambda t: t['w'] * t['h'])
            if abs(biggest['w'] - biggest['h']) < biggest['w'] * 0.45:
                iso = [biggest]
    std = [t for t in items if t not in iso]

    # 三视图按相对象限分到 主/俯/左
    slots = {}
    if std:
        xs = [t['cx'] for t in std]
        ys = [t['cy'] for t in std]
        mx = (min(xs) + max(xs)) / 2.0
        my = (min(ys) + max(ys)) / 2.0
        used = set()
        for t in std:
            left = t['cx'] <= mx
            top = t['cy'] >= my
            if left and top:
                key = 'front'
            elif (not left) and top:
                key = 'right'
            elif left and (not top):
                key = 'top'
            else:
                key = 'right'
            if key in used:
                for alt in ('top', 'right', 'front'):
                    if alt not in used:
                        key = alt
                        break
            used.add(key)
            slots[key] = t
    for t in iso:
        slots.setdefault('isometric', t)

    # 按**每个槽位自己的尺寸**排版，不能统一用最大值：
    # 轴测图比三视图大近一倍，用全局最大值会把底排挤到图框外
    # （实测俯视图被推到 y=3mm，几乎贴着纸边）。
    def _slot_size(key):
        t = slots.get(key)
        if t:
            return max(t['w'], 0.02), max(t['h'], 0.02)
        return 0.06, 0.06

    fw, fh = _slot_size('front')
    rw, rh = _slot_size('right')
    tw_, th_ = _slot_size('top')
    iw, ih = _slot_size('isometric')

    col1_w = max(fw, tw_)
    col2_w = max(rw, iw)
    row1_h = max(fh, rh)
    row2_h = max(th_, ih)

    col_step1 = col1_w + gap_m
    col_step2 = col2_w + gap_m
    row_step1 = row1_h + gap_m
    row_step2 = row2_h + gap_m

    total_w = col_step1 + col_step2
    total_h = row_step1 + row_step2

    # 整组在图框内居中，并保证上下都不越界
    left_x = margin_m + col1_w / 2.0 + max(0.0, (pw - 2 * margin_m - total_w) / 2.0)
    top_y = ph - title_h_m - margin_m - row1_h / 2.0
    bottom_y = top_y - row_step1
    # 底排越界就整体上移
    overflow_bottom = margin_m - (bottom_y - row2_h / 2.0)
    if overflow_bottom > 0:
        top_y += overflow_bottom
        bottom_y += overflow_bottom

    targets = {
        'front':     (left_x, top_y),
        'top':       (left_x, bottom_y),
        'right':     (left_x + col_step1, top_y),
        'isometric': (left_x + col_step1, bottom_y),
    }
    placed = {}
    for key, t in slots.items():
        tx, ty = targets[key]
        ok = False
        # ⚠ 关键：Create1stAngleViews2 建的投影视图带**对齐约束**，
        # 直接设 Position 之后 SolidWorks 会把非基准视图拉回对齐位置
        # （实测基准视图移动成功，另外两个留在 y=-104 / x=430 的原地）。
        # 必须先用 RemoveAlignment() 解除「长对正/高平齐」再设位置。
        try:
            t['view'].RemoveAlignment()
        except Exception:
            try:
                t['view'].SetAlignment(0, False)
            except Exception:
                pass
        try:
            t['view'].Position = arr2(tx, ty)
            ok = True
        except Exception:
            ok = False
        if ok:
            placed[t['name']] = (round(tx * 1000, 1), round(ty * 1000, 1))

    try:
        doc.EditRebuild3()
    except Exception:
        pass
    return placed

def create_standard_views(doc, model_path, extra_views=('isometric',), scale_den=None):
    """让 SolidWorks 自己按第一角投影一次性生成标准三视图。

    ⚠ 这是正确做法：Create1stAngleViews2 内部会自动建立「长对正、高平齐、
    宽相等」的投影对齐关系，并把三视图摆到图框中央。如果改成自己逐个
    CreateDrawViewFromModelView3 再挪位置，就丢失了对齐约束，
    视图会互相重叠、尺寸线挤出图框——这条弯路走过，别走第二次。

    生成后再补轴测图等额外视图（这类视图本来就不参与投影对齐）。

    返回 dict: {front/top/right/...: bool}
    """
    made = {}
    try:
        ok = doc.Create1stAngleViews2(model_path)
        made['_standard'] = bool(ok)
    except Exception:
        made['_standard'] = False

    if not made['_standard']:
        # 退回逐个创建（至少保证有图）
        for key in ('front', 'top', 'right'):
            try:
                v = doc.CreateDrawViewFromModelView3(
                    model_path, VIEW_NAMES.get(key, key), 0.12, 0.20, 0)
                made[key] = bool(v is not None and v is not False)
            except Exception:
                made[key] = False
        return made, None

    # 收集三视图对象
    by_size = {}
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 10:
        if i > 0:
            try:
                o = v.GetOutline
                w = abs(o[2] - o[0])
                h = abs(o[3] - o[1])
                by_size[v.GetName2] = (v, w, h, (o[0] + o[2]) / 2, (o[1] + o[3]) / 2)
            except Exception:
                pass
        v = v.GetNextView
        i += 1

    view_list = list(by_size.values())
    for name, (v, w, h, cx, cy) in by_size.items():
        made.setdefault('_view_' + name, {'w': round(w * 1000, 1), 'h': round(h * 1000, 1)})

    # 找前视图 = 面积最大的那个视图（通常前视最大）；退而取第一个
    front = None
    if view_list:
        front = max(view_list, key=lambda t: t[1] * t[2])[0]

    # 补轴测图：放在右视图右侧、与前视图同高
    for key in extra_views:
        if key not in ('isometric', 'left', 'back', 'bottom'):
            continue
        try:
            # 找右视图的位置做基准
            right = None
            for name, (v, w, h, cx, cy) in by_size.items():
                if w < h:      # 竖长的是侧视图
                    right = (cx, cy, w)
                    break
            if right:
                px = right[0] + right[2] / 2.0 + 0.075
                py = right[1]
            else:
                px, py = 0.33, 0.20
            v = doc.CreateDrawViewFromModelView3(
                model_path, VIEW_NAMES.get(key, key), px, py, 0)
            made[key] = bool(v is not None and v is not False)
            # 轴测图默认 1:1，会比其他视图大一倍（实测 100×124 vs 57×71），
            # 摆进去会压住相邻视图。统一成图纸比例。
            if v is not None and v is not False and scale_den:
                try:
                    v.ScaleDecimal = 1.0 / float(scale_den)
                except Exception:
                    pass
        except Exception:
            made[key] = False

    made['front'] = made.get('front', front is not None)
    made['top'] = made.get('top', len(view_list) >= 2)
    made['right'] = made.get('right', len(view_list) >= 3)
    return made, front


def annotate(doc, types=SW_INSERT_DIMENSIONS_MARKED, duplicate_dims=False):
    """插入模型尺寸。

    参数取自 github.com/BlakeOlinger/AutoDimensioner 的实战实现：
      EntitiesToDimension  = swImportModelItemsFromEntireModel(0)
      Types                = swInsertDimensionsMarkedForDrawing(32768)
      AllViews             = True
      DuplicateDims        = False   ← 关键！False 才能让同一尺寸在各视图分别出现
      HiddenFeatureDims    = False
      UsePlacementInSketch = False

    实测（电池座，19 个草图尺寸）：
      DuplicateDims=False → 34 个尺寸
      DuplicateDims=True  → 19 个尺寸
    与直觉相反：False 才是多视图投影的常规行为。
    """
    res = doc.InsertModelAnnotations3(0, types, True, bool(duplicate_dims), False, False)
    if res is None:
        return []
    try:
        return list(res)
    except TypeError:
        return [res]


def collect_dimensions(doc):
    """一次性把工程图里所有尺寸抽成结构化数据。

    写法参考 NVasadze/NDwgAutoTool 的 Dim4Service.GetDimensionsInDrawing()：
    用 View.GetDisplayDimensions() 一次拿到该视图的全部尺寸数组，
    比 GetFirstDisplayDimension4 + GetNext4 逐条遍历简单也更稳。

    每条记录：
      view      视图名
      display   IDisplayDimension
      dim       IDimension
      mm       绝对值（毫米）
      type     swDimensionType_e（IDisplayDimension.Type2）
      fullName 模型侧唯一名
      text     显示文本
      isRef    是否参考尺寸（文本含 REF）
      x, y     标注文字位置（米）

    返回 list[dict]
    """
    # 关键：先确保文档真的被激活。
    # 实测踩过：文档在后台时 GetDimension2/GetText 会抛 COM 错误
    # 「服务器出现意外情况」(RPC_E_SERVERFAULT)，数值全取不到，
    # 表现为 mm=0、text='' —— 会静默跳过所有分类和参考尺寸标记。
    try:
        import win32com.client as _w
        from win32com.client import VARIANT as _V
        from pythoncom import VT_BYREF as _VB, VT_I4 as _I4
        _sw = _w.Dispatch('SldWorks.Application')
        _e = _V(_VB | _I4, 0)
        _sw.ActivateDoc3(doc.GetTitle, True, 0, _e)
        time.sleep(0.6)
    except Exception:
        pass

    out = []
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 25:
        if i > 0:
            try:
                view_name = str(v.GetName2)
            except Exception:
                view_name = ''
            try:
                # 注意：GetDisplayDimensions 在 pywin32 晚绑定下是**属性**，
                # 写成 v.GetDisplayDimensions() 会报 'tuple' object is not callable。
                # 直接取属性即可，返回的是 tuple of IDisplayDimension。
                dims = v.GetDisplayDimensions
            except Exception:
                dims = None

            # GetDisplayDimensions 有时返回单个对象而非数组，统一成列表
            if dims is None:
                arr = []
            elif isinstance(dims, (list, tuple)):
                arr = list(dims)
            else:
                try:
                    arr = list(dims)
                except TypeError:
                    arr = [dims]

            for d in arr:
                if d is None:
                    continue
                rec = {'view': view_name, 'display': d, 'dim': None,
                       'mm': 0.0, 'type': -1, 'fullName': '',
                       'text': '', 'isRef': False, 'x': 0.0, 'y': 0.0}
                # 标注位置
                try:
                    ann = d.GetAnnotation
                    pos = ann.GetPosition
                    rec['x'] = float(pos[0])
                    rec['y'] = float(pos[1])
                except Exception:
                    pass
                # 尺寸对象与真实值
                # 注意：GetDimension2(0) 在「刚重新打开的文档」上可能返回空对象，
                # 表现为 mm 全 0、fullName 为空。要有回退链。
                dim = None
                for getter in ('GetDimension2', 'GetDimension'):
                    try:
                        dim = getattr(d, getter)(0) if getter == 'GetDimension2' else getattr(d, getter)
                        if dim is not None:
                            break
                    except Exception:
                        dim = None
                rec['dim'] = dim
                if dim is not None:
                    for val_getter in ('SystemValue', 'GetSystemValue2'):
                        try:
                            if val_getter == 'SystemValue':
                                v_ = float(dim.SystemValue)
                            else:
                                v_ = float(dim.GetSystemValue2(''))
                            if abs(v_) > 1e-12:
                                rec['mm'] = abs(v_) * 1000.0
                                break
                        except Exception:
                            continue
                    try:
                        rec['fullName'] = str(dim.FullName)
                    except Exception:
                        pass
                # 回退：从显示文本里解析数值（如 "60 REF" -> 60）
                if rec['mm'] == 0.0:
                    try:
                        txt = str(d.GetText(5) or '')
                        import re as _re
                        m = _re.search(r'\d+(?:\.\d+)?', txt)
                        if m:
                            rec['mm'] = float(m.group(0))
                    except Exception:
                        pass
                # 显示文本（WhichText=5 是主显示槽位）
                try:
                    rec['text'] = str(d.GetText(5) or '')
                except Exception:
                    try:
                        rec['text'] = str(d.GetText(2) or '')
                    except Exception:
                        pass
                # 类型
                try:
                    rec['type'] = int(d.Type2)
                except Exception:
                    pass
                # 参考尺寸判定（国标里参考尺寸要加括号或 REF）
                rec['isRef'] = 'REF' in rec['text'].upper()
                out.append(rec)
        v = v.GetNextView
        i += 1
    return out


def mark_reference_dimensions(doc, keep_full_names, cfg=None):
    """把不参与加工的从属尺寸标成参考尺寸（加 REF），而不是删掉。

    这是国标 GB/T 4458.4 对「参考尺寸」的处理方式：
    参考尺寸不是不要，而是标明「仅供参考，不作为检验依据」，
    这样图面完整、又不会让加工误以为它是要保证的尺寸。

    keep_full_names: 要保留为正式尺寸的 fullName 集合；不在其中的就标 REF。

    返回 (标记数, 跳过数)
    """
    marked = 0
    skipped = 0
    for rec in collect_dimensions(doc):
        name = rec['fullName']
        if not name or name in keep_full_names or rec['isRef']:
            continue
        try:
            base = rec['text'].strip()
            # 去掉已有的括号/REF 再重加，避免叠加
            base = base.replace('(', '').replace(')', '').replace('REF', '').strip()
            rec['display'].SetText(5, base + ' REF')
            marked += 1
        except Exception:
            skipped += 1
    return marked, skipped


def strip_dimension_leaders(doc):
    """去掉尺寸的引线（SetLeader3 + swNO_LEADER），让图面更干净。

    NVasadze/NDwgAutoTool 里移动标注后就会顺手清引线，
    否则尺寸文字会拖一条长线穿过图形。

    返回处理的条数。
    """
    SW_NO_LEADER = 0
    n = 0
    for rec in collect_dimensions(doc):
        try:
            ann = rec['display'].GetAnnotation
            ann.SetLeader3(SW_NO_LEADER, 0, False, False, False, False)
            n += 1
        except Exception:
            pass
    return n


def count_dims(doc):
    total = 0
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 20:
        try:
            total += v.GetDimensionCount
        except Exception:
            pass
        v = v.GetNextView
        i += 1
    return total


def iter_dimensions(doc):
    """遍历所有显示尺寸，产出 IDimension。"""
    v = doc.GetFirstView
    guard = 0
    while v is not None and guard < 20:
        try:
            n = v.GetDimensionCount
        except Exception:
            n = 0
        if n:
            try:
                d = v.GetFirstDisplayDimension4
            except Exception:
                d = v.GetFirstDisplayDimension
            k = 0
            while d is not None and k < n + 5:
                try:
                    yield d.GetDimension
                except Exception:
                    pass
                for nxt in ('GetNext4', 'GetNext3', 'GetNext'):
                    try:
                        d = getattr(d, nxt)
                        break
                    except Exception:
                        d = None
                k += 1
        v = v.GetNextView
        guard += 1


def apply_tolerances(doc, spec):
    """spec: {type: 'symmetric', plus: 0.05, minus: 0.05, onlyOver: 1.0}

    只给“线性尺寸值 >= onlyOver(mm)”的尺寸加公差，避免小尺寸全被加公差别扭。
    """
    if not spec:
        return 0
    tol_type = TOLERANCE_TYPES.get(str(spec.get('type', 'symmetric')).lower(), 3)
    plus = float(spec.get('plus', 0.05))
    minus = float(spec.get('minus', -plus))
    only_over = float(spec.get('onlyOver', 0.0))

    applied = 0
    for dim in iter_dimensions(doc):
        try:
            sysval = dim.GetSystemValue2('')
            mm = abs(float(sysval)) * 1000.0
            if mm < only_over:
                continue
            dim.SetToleranceType(tol_type)
            dim.SetToleranceValues(minus, plus)
            applied += 1
        except Exception:
            continue
    return applied


def add_surface_finish(doc, value='3.2', x=0.15, y=0.06):
    """加 GB 粗糙度符号。返回是否成功。"""
    try:
        symbol = doc.Extension.InsertSurfaceFinishSymbol3(
            0,          # SymType: 0=基本符号(带去掉材料)
            2,          # LeaderType: 2=无引线
            x, y, 0,
            0,          # LaySymbol
            0,          # ArrowType
            '', '', '', '',
            str(value), '', ''
        )
        return symbol is not None
    except Exception:
        return False


# 标题栏常见的自定义属性名（GB 图框里通常用这些）
TITLE_BLOCK_FIELDS = {
    'material': ['材质', '材料', 'Material'],
    'title': ['图纸名称', '名称', 'Title', 'Description'],
    'number': ['图纸编号', '图号', '代号', 'DrawingNo', 'Number'],
    'revision': ['版本', '版次', 'Revision'],
    'designer': ['设计', 'Designer', 'DesignedBy'],
    'checker': ['审核', '校对', 'Checker'],
    'date': ['日期', 'Date'],
    'company': ['单位', '公司', 'Company'],
    'weight': ['重量', 'Weight'],
    'scale': ['比例', 'Scale'],
}


def set_document_custom_properties(doc, props):
    """把图纸信息写进文档自定义属性（标题栏靠这些字段取值）。

    props: {'material': '6061', 'title': '上电池架', 'number': 'SW-001', ...}
    返回真正写成功的字段列表。
    """
    written = []
    if not props:
        return written

    try:
        ext = doc.Extension
        cpm = ext.CustomPropertyManager('')
    except Exception:
        return written

    for key, value in props.items():
        if value is None or str(value).strip() == '':
            continue
        # 一个逻辑字段可能对应多个候选名，全部写一遍，命中的那个会显示出来
        names = TITLE_BLOCK_FIELDS.get(key, [key])
        for name in names:
            try:
                cpm.Add3(name, 30, str(value), 2)  # 30=swCustomInfoText, 2=覆盖
                written.append(name)
            except Exception:
                try:
                    cpm.Add2(name, 30, str(value))
                    written.append(name)
                except Exception:
                    continue
    return written


def update_title_block(doc):
    """重建图纸，让标题栏里的属性值刷新出来。"""
    try:
        doc.ForceRebuild3(True)
    except Exception:
        try:
            doc.EditRebuild3()
        except Exception:
            pass
    try:
        doc.GraphicsRedraw2()
    except Exception:
        pass


def apply_gb_style(doc, text_height_mm=3.5):
    """按 GB/T 4458 设置标注样式：字高、箭头、线宽。

    GB 常用值：
      尺寸文字高度 3.5mm（A3/A4 图幅）
      箭头长度 ≈ 字高，宽度 ≈ 字高/3
      可见轮廓线 = 粗实线 ≈ 0.5mm（与图框配合）
      尺寸线/尺寸界线 = 细实线 ≈ 0.25mm
      剖面线 = 细实线

    注意：SolidWorks 的偏好值单位是米，所以 3.5mm -> 0.0035。
    """
    h = float(text_height_mm) / 1000.0
    applied = {}
    ext = doc.Extension

    def set_double(idx, value, label):
        try:
            ext.SetUserPreferenceDouble(idx, 0, float(value))
            applied[label] = float(value)
        except Exception:
            pass

    def set_int(idx, value, label):
        try:
            ext.SetUserPreferenceInteger(idx, 0, int(value))
            applied[label] = int(value)
        except Exception:
            pass

    # 文字高度
    set_double(0, h, 'noteFontHeight')       # swDetailingNoteFontHeight
    set_double(1, h, 'dimFontHeight')        # swDetailingDimFontHeight
    set_double(22, h * 0.75, 'tolTextHeight')  # 公差文字略小
    # 箭头（GB：长≈字高，宽≈字高/3）
    set_double(24, h, 'arrowHeight')
    set_double(25, h / 3.0, 'arrowWidth')
    set_double(26, h, 'arrowLength')
    set_double(27, h, 'sectionArrowHeight')
    set_double(28, h / 3.0, 'sectionArrowWidth')
    set_double(29, h, 'sectionArrowLength')
    # 尺寸界线
    set_double(13, h * 0.5, 'witnessLineGap')
    set_double(14, h * 0.5, 'witnessLineExtension')
    set_double(16, h * 2.0, 'dimToDimOffset')
    set_double(15, h * 1.5, 'objectToDimOffset')

    # 线宽（swLineFontXXXThickness，单位米）
    set_int(53, 4, 'visibleEdgesThickness')   # 粗实线（SW 用枚举/等级）
    set_int(55, 1, 'hiddenEdgesThickness')
    set_int(63, 1, 'dimensionsThickness')
    set_int(61, 1, 'sectionLineThickness')

    # 精度：2 位小数
    set_int(49, 2, 'linearDecimalPlaces')

    try:
        doc.ForceRebuild3(True)
    except Exception:
        pass
    return applied


def arrange_dimensions(doc, spacing_m=0.010):
    """按 GB/T 4458.4 把自动插入的尺寸分层排开。

    SolidWorks 刚插入的模型尺寸会挤成一团、互相穿插，违反「尺寸线不得
    与其他图线重合、平行尺寸线间距不小于字高」的规定。
    AlignDimensions 会把它们拉成分层，是最接近国标「尺寸排列整齐」的官方开关。

    ⚠ 实测教训：只调一个方向会把所有尺寸全堆到视图上方，多层叠起来
    直接冲出图框顶边（实测超出 46.8mm）。四个方向都要调，让尺寸分散到四周：
      swAutodimHorizontalPlacementAbove =  1
      swAutodimHorizontalPlacementBelow = -1
      swAutodimVerticalPlacementRight   =  1
      swAutodimVerticalPlacementLeft    = -1

    返回 {方向: 是否成功}
    """
    results = {}
    for align_type, label in ((1, 'above'), (-1, 'below'),
                              (1, 'right'), (-1, 'left')):
        try:
            results[label] = bool(doc.Extension.AlignDimensions(
                align_type, float(spacing_m)))
        except Exception:
            results[label] = False
    results['any'] = any(v for k, v in results.items() if k != 'any')
    return results


def dedupe_dimensions(doc, tol_mm=0.01):
    """删除重复标注，符合 GB/T 4458.4「同一要素不得在多视图重复标注」。

    SolidWorks 的 InsertModelAnnotations3 会把同一个模型尺寸在多视图上
    重复投影出来（尤其剖视图也开了 AllViews）。同一要素重复标注会造成
    尺寸封闭矛盾，加工/检验时不知道该保证哪个。

    判定方式：同一个 IDimension 的模型侧引用（GetDimension 的 Name）相同、
    且数值相同，视为重复。保留「在表达最清楚视图上」的第一个 ——
    按视图顺序，前视优先，其次上视/右视，剖视图最后。

    返回被删掉的条数。
    """
    # 收集所有显示尺寸：(viewName, displayDim, dimObj, value, name)
    entries = []
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 25:
        if i > 0:
            try:
                vname = v.GetName2
                n = v.GetDimensionCount
            except Exception:
                vname, n = '', 0
            if n:
                try:
                    d = v.GetFirstDisplayDimension4
                except Exception:
                    d = v.GetFirstDisplayDimension
                k = 0
                while d is not None and k < n + 5:
                    try:
                        dim = d.GetDimension
                        val = round(abs(float(dim.GetSystemValue2(''))), 6)
                        # 模型侧尺寸的唯一标识
                        try:
                            name = str(dim.GetName)
                        except Exception:
                            name = ''
                        entries.append((vname, d, dim, val, name))
                    except Exception:
                        pass
                    for nxt in ('GetNext4', 'GetNext3', 'GetNext'):
                        try:
                            d = getattr(d, nxt)
                            break
                        except Exception:
                            d = None
                    k += 1
        v = v.GetNextView
        i += 1

    # 视图优先级：前视 > 上视 > 右视 > 其他 > 剖视
    def view_rank(name):
        if '前视' in name or '工程图视图1' == name:
            return 0
        if '上视' in name or '工程图视图2' == name:
            return 1
        if '右视' in name or '工程图视图3' == name:
            return 2
        if '剖面' in name or '剖' in name:
            return 9
        return 5

    entries.sort(key=lambda e: view_rank(e[0]))

    seen = {}
    removed = 0
    keep = []
    for vname, disp, dim, val, name in entries:
        key = (name, val) if name else (id(dim), val)
        if key in seen:
            # 重复：删掉这一条（保留先出现的）
            try:
                doc.ClearSelection2(True)
                disp.Select(False, 0) if hasattr(disp, 'Select') else None
                doc.EditDelete()
                removed += 1
                continue
            except Exception:
                pass
        else:
            seen[key] = True
        keep.append((vname, dim))

    try:
        doc.ClearSelection2(True)
    except Exception:
        pass
    return removed, len(keep)


# swDimensionType_e（IDisplayDimension.Type2 的取值）
DIM_LINEAR = (2, 11, 12)      # 线性（含水平/垂直）
DIM_ANGULAR = (3,)            # 角度
DIM_ARCLEN = (4,)             # 弧长
DIM_RADIAL = (5,)             # 半径
DIM_DIAMETER = (6,)           # 直径
DIM_CHAMFER = (10,)           # 倒角
DIM_ORDINATE = (1, 7, 8)      # 坐标


def classify_dimensions(doc):
    """按 GB 的「定形/定位/总体」思路给尺寸分类。

    分类依据（国标口径）：
      总体尺寸 = 模型包围盒的 长/宽/高
      定形尺寸 = 直径、半径、倒角、角度（决定孔/圆角/斜面本身大小）
      定位尺寸 = 线性尺寸里非总体、且不是极小的
      细节尺寸 = 数值极小（<1mm）的非功能结构，通常不必标

    IDisplayDimension.Type2 取 swDimensionType_e：
      2/11/12 线性，3 角度，4 弧长，5 半径，6 直径，10 倒角，1/7/8 坐标

    返回 {'overall': [...], 'form': [...], 'position': [...], 'detail': [...]}
    每条含 collect_dimensions() 的完整字段。
    """
    # 模型包围盒（米）—— 工程图文档上 GetPartBox 取不到，
    # 要顺着视图找到它引用的模型文档再取。
    bbox = None
    try:
        b = doc.GetPartBox(True)
        if b and len(b) >= 6:
            bbox = sorted([abs(b[3] - b[0]), abs(b[4] - b[1]), abs(b[5] - b[2])])
    except Exception:
        pass
    if bbox is None:
        try:
            v = doc.GetFirstView
            i = 0
            while v is not None and i < 10:
                if i > 0:
                    try:
                        mdoc = v.ReferencedDocument
                    except Exception:
                        mdoc = None
                    if mdoc is not None:
                        b = mdoc.GetPartBox(True)
                        if b and len(b) >= 6:
                            bbox = sorted([abs(b[3] - b[0]),
                                           abs(b[4] - b[1]),
                                           abs(b[5] - b[2])])
                            break
                v = v.GetNextView
                i += 1
        except Exception:
            pass

    groups = {'overall': [], 'form': [], 'position': [], 'detail': []}
    tol = 0.0005  # 0.5mm 容差，避免浮点误差

    for rec in collect_dimensions(doc):
        dtype = rec['type']
        mm = rec['mm']
        val = mm / 1000.0

        # 特征来源（fullName 形如 D1@切除-拉伸3@电池座.Part）
        # 这比只看数值准确得多：能知道这个尺寸是从哪个特征来的。
        src = (rec.get('fullName') or '').lower()

        if dtype in DIM_DIAMETER or dtype in DIM_RADIAL:
            # 直径/半径 = 定形尺寸（孔径、圆角半径）
            groups['form'].append(rec)
        elif dtype in DIM_CHAMFER or dtype in DIM_ANGULAR or dtype in DIM_ARCLEN:
            groups['form'].append(rec)
        elif bbox and any(abs(val - bx) < tol for bx in bbox):
            # 与包围盒对齐 = 总体尺寸
            groups['overall'].append(rec)
        elif 0 < mm < 1.0:
            # 极小值 = 非功能细节（间隙、微小倒角）
            groups['detail'].append(rec)
        else:
            # 其余按定位尺寸处理，特征来源留给调用方参考
            rec['source'] = src
            groups['position'].append(rec)
    return groups


def select_key_dimensions(doc, min_mm=1.0, max_count=40):
    """按国标「定形/定位/总体」思路筛选关键尺寸，删掉冗余的小尺寸。

    GB/T 4458.4-2003 的原则：尺寸要「必要、充分、无矛盾」，不是越多越好。
    SolidWorks 自动插入的是模型里所有草图尺寸，往往含大量 2mm、3mm 非功能
    细节，全标上去会挤成一团，违反「清晰」要求。细小结构（倒角、退刀槽、
    沉孔）可用简化注法或一般公差覆盖，不必逐个标。

    策略（保守，优先保留信息量大的）：
      1) 删掉 < min_mm 的极小尺寸（默认 1mm）
      2) 仍超过 max_count 时，保留最大的若干个（总体尺寸优先）

    返回 (删掉的条数, 剩余条数)
    """
    entries = []
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 25:
        if i > 0:
            try:
                n = v.GetDimensionCount
            except Exception:
                n = 0
            if n:
                try:
                    d = v.GetFirstDisplayDimension4
                except Exception:
                    d = v.GetFirstDisplayDimension
                k = 0
                while d is not None and k < n + 5:
                    try:
                        dim = d.GetDimension
                        mm = abs(float(dim.GetSystemValue2(''))) * 1000.0
                        entries.append((mm, d))
                    except Exception:
                        pass
                    for nxt in ('GetNext4', 'GetNext3', 'GetNext'):
                        try:
                            d = getattr(d, nxt)
                            break
                        except Exception:
                            d = None
                    k += 1
        v = v.GetNextView
        i += 1

    removed = 0

    def drop(disp):
        nonlocal removed
        try:
            doc.ClearSelection2(True)
            disp.Select(False, 0)
            doc.EditDelete()
            removed += 1
            return True
        except Exception:
            return False

    # 1) 删极小尺寸
    for mm, disp in list(entries):
        if 0 < mm < min_mm:
            if drop(disp):
                entries.remove((mm, disp))

    # 2) 超量则只留最大的
    if len(entries) > max_count:
        entries.sort(key=lambda e: e[0], reverse=True)
        for mm, disp in entries[max_count:]:
            drop(disp)
        entries = entries[:max_count]

    try:
        doc.ClearSelection2(True)
    except Exception:
        pass
    return removed, len(entries)


def clamp_dimensions_inside(doc, paper, landscape=True, margin=0.004):
    """把跑出图框的尺寸拉回图框内。

    剖视图这类视图插入后，SolidWorks 会自动把模型尺寸摊在它四周，
    纵向跨度可能远超视图本身（实测剖视图本体 75mm，尺寸摊到 187mm）。
    单纯移动视图没用，必须逐个把越界的尺寸文字位置拉回来。

    做法：遍历所有显示尺寸，凡越界的，用 IAnnotation.SetPosition 平移
    到最近的合法位置（保持相对次序，只做最小位移）。

    返回被修正的尺寸个数。
    """
    pw, ph = PAPER_SIZE_M.get(paper.upper(), (0.420, 0.297))
    if not landscape:
        pw, ph = ph, pw
    lo_x, hi_x = 0.025 + margin, pw - 0.005 - margin
    lo_y, hi_y = 0.005 + margin, ph - 0.005 - margin

    fixed = 0
    v = doc.GetFirstView
    i = 0
    while v is not None and i < 20:
        if i > 0:
            try:
                n = v.GetDimensionCount
            except Exception:
                n = 0
            if n:
                try:
                    d = v.GetFirstDisplayDimension4
                except Exception:
                    d = v.GetFirstDisplayDimension
                k = 0
                while d is not None and k < n + 5:
                    try:
                        ann = d.GetAnnotation
                        pos = ann.GetPosition
                        x, y = float(pos[0]), float(pos[1])
                        nx = min(max(x, lo_x), hi_x)
                        ny = min(max(y, lo_y), hi_y)
                        if abs(nx - x) > 1e-6 or abs(ny - y) > 1e-6:
                            # SetPosition 不接受元组（会报类型不匹配），
                            # 实际签名是 SetPosition(x, y, z) 三个 Double；
                            # Z 用原值，不要动（动 Z 会把标注甩到图纸平面外）。
                            z = float(pos[2]) if len(pos) > 2 else 0.0
                            for call in (
                                lambda: ann.SetPosition(nx, ny, z),
                                lambda: ann.SetPosition2(nx, ny, z),
                            ):
                                try:
                                    call()
                                    fixed += 1
                                    break
                                except Exception:
                                    continue
                    except Exception:
                        pass
                    for nxt in ('GetNext4', 'GetNext3', 'GetNext'):
                        try:
                            d = getattr(d, nxt)
                            break
                        except Exception:
                            d = None
                    k += 1
        v = v.GetNextView
        i += 1
    return fixed


def check_overflow(doc, paper, landscape=True, margin=0.002):
    """检查是否超出图框可画区——**视图轮廓和尺寸文字都要查**。

    ⚠ 踩过的坑：只查 IView.GetOutline 会漏掉尺寸线。
    GetOutline 只返回几何轮廓，不含引出去的尺寸线和数字，
    所以会出现「图上的尺寸明明溢出图框，检查却报通过」的假阴性。
    这里额外遍历每个显示尺寸，用 IAnnotation.GetPosition 取文字位置一起判断。

    返回 (ok, worst)
    """
    pw, ph = PAPER_SIZE_M.get(paper.upper(), (0.420, 0.297))
    if not landscape:
        pw, ph = ph, pw
    # GB 图框内边距：装订边 25、其余 5
    lo_x, hi_x = 0.025 - margin, pw - 0.005 + margin
    lo_y, hi_y = 0.005 - margin, ph - 0.005 + margin

    worst = None
    worst_over = 0.0

    def note(kind, name, x, y):
        nonlocal worst, worst_over
        over = max(lo_x - x, x - hi_x, lo_y - y, y - hi_y, 0.0)
        if over > worst_over:
            worst_over = over
            worst = {'kind': kind, 'view': name,
                     'atMm': [round(x * 1000, 1), round(y * 1000, 1)],
                     'overflowMm': round(over * 1000, 1)}

    v = doc.GetFirstView
    i = 0
    while v is not None and i < 20:
        if i > 0:
            # 1) 几何轮廓
            try:
                o = v.GetOutline
                for cx, cy in ((o[0], o[1]), (o[2], o[1]), (o[0], o[3]), (o[2], o[3])):
                    note('view', v.GetName2, cx, cy)
            except Exception:
                pass

            # 2) 尺寸文字位置（GetOutline 不含这部分！）
            try:
                n = v.GetDimensionCount
            except Exception:
                n = 0
            if n:
                try:
                    d = v.GetFirstDisplayDimension4
                except Exception:
                    d = v.GetFirstDisplayDimension
                k = 0
                while d is not None and k < n + 5:
                    try:
                        ann = d.GetAnnotation
                        pos = ann.GetPosition
                        note('dim', v.GetName2, float(pos[0]), float(pos[1]))
                    except Exception:
                        pass
                    for nxt in ('GetNext4', 'GetNext3', 'GetNext'):
                        try:
                            d = getattr(d, nxt)
                            break
                        except Exception:
                            d = None
                    k += 1
        v = v.GetNextView
        i += 1

    return (worst is None), worst


def push_dims_outside(doc, views, offset=0.006):
    """把每个视图的尺寸推到视图外侧，符合 GB/T 4458.4「尺寸尽量标注在视图外面」。

    做法：对每个视图调 AlignDimensions 的两个方向，让尺寸线拉到轮廓外。
    视图四周都试一遍，取第一个成功的方向。
    """
    pushed = 0
    for _key, v in views.items():
        if v is None:
            continue
        ok = False
        for align_type in (1, -1):
            try:
                # AlignDimensions 作用于当前选中视图
                doc.ClearSelection2(True)
                v.SelectEntity(v, False)
                if doc.Extension.AlignDimensions(align_type, float(offset)):
                    ok = True
                    break
            except Exception:
                continue
        if ok:
            pushed += 1
    try:
        doc.ClearSelection2(True)
    except Exception:
        pass
    return pushed


def auto_fit_scale(model_size, paper, landscape, requested_den=None, attempts=4):
    """选比例：优先用户指定；否则按 GB 优先比例自动算。

    注意：不再额外「降一档」。
    因为 pick_gb_scale 已经把标注溢出余量(ANNOT_PAD)算进去了，
    再降一档会把图缩得过小（实测 350mm 的零件被缩到 1:10，只占图幅 1/4）。

    返回 scale_den（图纸比例为 1:den）
    """
    if requested_den:
        try:
            return float(requested_den)
        except Exception:
            pass
    num, den = pick_gb_scale(model_size, paper, landscape)
    # num/den 表示 num:den，转成 1:x 形式
    if num and num != 1:
        return max(float(den) / float(num), 0.01)
    return float(den)


def edit_dwg_title_block(dwg_path, fields, visible=False, exe_hint=None):
    """第二步：调 AutoCAD 改写 DWG 标题栏。

    SolidWorks 自带 GB 图框的标题栏是静态文字，只能导出 DWG 后在 AutoCAD 里改。
    见 acad_editor.py 顶部的实测说明。
    """
    import importlib
    import acad_editor
    importlib.reload(acad_editor)
    return acad_editor.edit_title_block(dwg_path, fields, None, visible,
                                        exe_hint=exe_hint)



def save_and_export(doc, out_drw, out_dxf, out_dwg):
    from win32com.client import VARIANT
    from pythoncom import VT_BYREF, VT_I4
    errs = VARIANT(VT_BYREF | VT_I4, 0)
    warns = VARIANT(VT_BYREF | VT_I4, 0)
    out = {}

    def do_save(path, key):
        if not path:
            return
        try:
            ok = bool(doc.SaveAs4(path, 0, 1, errs, warns))
            out[key] = ok
            out[key + '_size'] = os.path.getsize(path) if os.path.exists(path) else 0
            out[key + '_err'] = errs.value
            out[key + '_warn'] = warns.value
        except Exception as exc:
            out[key] = False
            out[key + '_error'] = str(exc)

    do_save(out_drw, 'drw')
    do_save(out_dxf, 'dxf')
    do_save(out_dwg, 'dwg')
    return out


def render_preview(doc, png_path, width=2200, height=1500):
    """导出预览图。

    注意：SolidWorks 的 SaveBMP 对含中文的路径可能失败并写到一个奇怪的
    文件名（实测出过 '.bmp' 这种），所以统一走「ASCII 临时 BMP」再转存。
    """
    try:
        doc.ViewZoomtofit2()
        time.sleep(1.0)
    except Exception:
        pass

    import tempfile
    tmp_bmp = os.path.join(tempfile.gettempdir(), 'dsh_cad_preview.bmp')
    try:
        if os.path.exists(tmp_bmp):
            try:
                os.remove(tmp_bmp)
            except Exception:
                pass
        ok = doc.SaveBMP(tmp_bmp, width, height)
        if not ok or not os.path.exists(tmp_bmp):
            try:
                emit({'stage': 'preview_fail', 'reason': 'SaveBMP failed',
                      'ok': bool(ok), 'exists': os.path.exists(tmp_bmp),
                      'doc': str(doc.GetTitle)})
            except Exception:
                pass
            return None
        from PIL import Image
        Image.open(tmp_bmp).convert('RGB').save(png_path, quality=92)
        try:
            os.remove(tmp_bmp)
        except Exception:
            pass
        return png_path
    except Exception as exc:
        try:
            emit({'stage': 'preview_fail',
                  'reason': f'{type(exc).__name__}: {exc}'})
        except Exception:
            pass
        return None


def import_neutral_to_part(sw, neutral_path, out_dir, emit):
    """把 STEP / IGES / Parasolid 导入并另存为 .sldprt，返回可用的 .sldprt 路径。

    为什么必须「另存 + 重开」而不是直接用导入后的文档：
      导入后的文档没有保存过，GetPathName 返回空串，
      Create1stAngleViews2(空串) 直接返回 False、一个视图都不出。
      存成 .sldprt 再打开，路径就有了，视图才正常。

    注意 LoadFile4 的两个坑（实测）：
      1. 签名是 (FileName, ArgString, ImportData, Errors)，**只有 4 个参数**；
      2. ImportData 传 Python None 会报「类型不匹配」，
         必须传 VARIANT(VT_DISPATCH, None)。
    """
    from win32com.client import VARIANT
    from pythoncom import VT_BYREF, VT_I4, VT_DISPATCH

    ext = os.path.splitext(neutral_path)[1].lower()
    stem = os.path.splitext(os.path.basename(neutral_path))[0]
    part_path = os.path.join(out_dir, stem + '.SLDPRT')

    emit({'stage': 'neutral_import', 'from': neutral_path,
          'ext': ext, 'to': part_path})

    # 先关掉所有文档：上一轮残留的同名 .sldprt 会占着文件，
    # 导致下面 SaveAs4 直接 err=1（实测踩过）。
    try:
        sw.CloseAllDocuments(True)
        time.sleep(1.2)
    except Exception:
        pass

    errs = VARIANT(VT_BYREF | VT_I4, 0)
    doc = sw.LoadFile4(neutral_path, '', VARIANT(VT_DISPATCH, None), errs)
    if doc is None:
        raise RuntimeError(
            f'中立格式导入失败（{ext}，err={errs.value}）。'
            'STEP 建议用 AP203/AP214；若文件来自高版本请另存后重试。')
    emit({'stage': 'neutral_loaded', 'title': str(doc.GetTitle),
          'err': errs.value})

    # 统计几何规模，方便事后判断能否标注
    faces = edges = 0
    try:
        bodies = doc.GetBodies2(0, True)
        if bodies:
            faces = len(bodies[0].GetFaces() or [])
            edges = len(bodies[0].GetEdges() or [])
    except Exception:
        pass
    time.sleep(0.8)

    # 另存为 .sldprt（若目标被占，重试最多 3 次）
    ok = False
    e2 = VARIANT(VT_BYREF | VT_I4, 0)
    w2 = VARIANT(VT_BYREF | VT_I4, 0)
    for attempt in range(3):
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        try:
            ok = bool(doc.SaveAs4(part_path, 0, 0, e2, w2))
        except Exception as exc:
            emit({'stage': 'neutral_save_retry', 'attempt': attempt + 1,
                  'error': f'{type(exc).__name__}: {exc}'})
            ok = False
        if ok and os.path.exists(part_path):
            break
        emit({'stage': 'neutral_save_retry', 'attempt': attempt + 1,
              'err': e2.value, 'warn': w2.value})
        time.sleep(1.5)
    if not ok or not os.path.exists(part_path):
        raise RuntimeError(f'另存 .sldprt 失败 err={e2.value} warn={w2.value}')
    emit({'stage': 'neutral_saved', 'part': part_path,
          'size': os.path.getsize(part_path), 'faces': faces, 'edges': edges})

    # 关掉导入文档，重新打开已保存的 .sldprt（必须有路径才能建视图）
    try:
        sw.CloseAllDocuments(True)
    except Exception:
        pass
    time.sleep(1.2)
    e3 = VARIANT(VT_BYREF | VT_I4, 0)
    w3 = VARIANT(VT_BYREF | VT_I4, 0)
    part = sw.OpenDoc6(part_path, 1, 1, '', e3, w3)
    if part is None:
        raise RuntimeError(f'重新打开 .sldprt 失败 err={e3.value}')
    time.sleep(1.0)

    emit({'stage': 'neutral_ready', 'part': part_path,
          'note': 'STEP/IGES 是导入实体，没有设计尺寸，'
                  '自动插入尺寸为 0；需要按几何手动/脚本标注。'})
    return part_path


def build(payload):
    model = (payload.get('model') or '').strip()
    if not model:
        raise ValueError(
            '没有拿到模型路径。浏览器里 File 对象没有本地路径，'
            '面板应该先 POST /api/upload 把文件存到本机再出图。')

    # model 必须是绝对路径：只有文件名（如 "四氟盖.SLDPRT"）说明调用方
    # 把浏览器里的 file.name 直接传过来了，dirname 会是 ''，
    # 落到 os.makedirs('') 就报 [WinError 3] 系统找不到指定的路径。: ''
    if not os.path.isabs(model):
        raise ValueError(
            f'模型路径不是绝对路径：{model!r}。'
            '浏览器的 File 对象不含本地路径，请先调用 /api/upload 上传模型，'
            '或用 cad_draw_build 传模型文件的绝对路径。')

    out_dir = payload.get('outDir') or os.path.dirname(model)
    if not out_dir:
        raise ValueError(f'推导不出输出目录（model={model!r}）')
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(model))[0]

    out_drw = os.path.join(out_dir, base + '.SLDDRW')
    out_dxf = os.path.join(out_dir, base + '.dxf') if payload.get('exportDxf', True) else None
    out_dwg = os.path.join(out_dir, base + '.dwg') if payload.get('exportDwg', False) else None

    if not os.path.exists(model):
        raise FileNotFoundError(f'模型不存在: {model}')

    kill_report_dialogs()
    sw = connect(payload.get('visible', True),
                 exe_hint=payload.get('solidworksExe'))
    emit({'stage': 'connected', 'revision': str(sw.RevisionNumber)})

    # ── 中立格式（STEP/IGES/Parasolid）：先导入再另存 .sldprt ──
    # 实测结论（2026-10 校准）：
    #   OpenDoc6 直接开 STEP 会 RPC 崩溃 → 必须用 LoadFile4；
    #   LoadFile4 第 3 个参数 ImportData 传 None 会「类型不匹配」，
    #   必须传 VARIANT(VT_DISPATCH, None)；签名只有 4 个参数（没有 Warnings）。
    # 导入后的文档是未保存的（GetPathName 为空），Create1stAngleViews2 拿不到路径，
    # 所以必须先 SaveAs4 存成 .sldprt，再重新打开，视图才能正常出。
    if os.path.splitext(model)[1].lower() in NEUTRAL_EXTS:
        model = import_neutral_to_part(sw, model, out_dir, emit)
        base = os.path.splitext(os.path.basename(model))[0]
        out_drw = os.path.join(out_dir, base + '.SLDDRW')
        out_dxf = os.path.join(out_dir, base + '.dxf') if payload.get('exportDxf', True) else None
        out_dwg = os.path.join(out_dir, base + '.dwg') if payload.get('exportDwg', False) else None

    # 清掉上次残留的工程图文档（否则标注会认错当前图纸，返回 0 个）
    swept = sweep_documents(sw)
    if swept:
        emit({'stage': 'swept', 'closed': swept})
        time.sleep(0.5)

    doc_model, err, warn = open_model(sw, model)
    if doc_model is None:
        raise RuntimeError(f'打开模型失败 err={err} warn={warn}: {model}')
    emit({'stage': 'model_opened', 'err': err, 'warn': warn})

    # 装配体要走装配图流程：多了明细表（BOM）和零件序号
    is_assembly = os.path.splitext(model)[1].lower() == '.sldasm'
    if is_assembly:
        try:
            comps = int(doc_model.GetComponentCount(0))
        except Exception:
            comps = 0
        emit({'stage': 'assembly', 'components': comps})

    # 判定模型类型：参数化模型 vs STEP/IGES 导入实体。
    # 两者自动标注能力天差地别——导入实体没有设计尺寸，
    # InsertModelAnnotations3 插不出东西（实测 144面/450边/0草图的零件
    # 只能插 1 个尺寸），提前告警比事后让用户困惑好。
    model_info = analyze_model_type(doc_model)
    emit({'stage': 'model_type', **model_info})

    # ── 标注标准（GB / ISO / ASME）──
    # 用户要求：标注前必须先读标准，别凭记忆标。
    # Host 侧 cad_draw_standard 已把规则正文交给模型；这里只回显实际生效的标准，
    # 供日志与前端确认，避免"选了 ISO 实际按 GB 标"的错配。
    _std_raw = str(payload.get('annotationStandard') or 'GB').strip().upper()
    if 'ISO' in _std_raw:
        _std = 'ISO'
    elif 'ASME' in _std_raw or 'ANSI' in _std_raw or 'Y14' in _std_raw:
        _std = 'ASME'
    else:
        _std = 'GB'
    STD_INFO = {
        'GB':   {'diameter': 'φ', 'projection': '第一角', 'generalTol': 'GB/T 1804-m'},
        'ISO':  {'diameter': '⌀', 'projection': '第一角', 'generalTol': 'ISO 2768-m'},
        'ASME': {'diameter': '⌀', 'projection': '第三角', 'generalTol': '标题栏声明'},
    }[_std]
    emit({'stage': 'annotation_standard', 'standard': _std, **STD_INFO,
          'note': '标注前请先读 cad_draw_standard 返回的规则正文'})

    # ── 技术要求 / 配合公差 / 夹具中心线（用户按需填，留空自动跳过）──
    # 用户要求："这些需求做到侧边栏输入框里让用户输入，如果有需求的话"。
    # 这里只负责把用户填的内容整理成可直接写进图的编号条目，
    # 真正落到 AutoCAD 由标注环节执行；空项一律不生成，避免图面出现空条目。
    _tr = payload.get('technicalRequirements') or {}
    _req = []
    _lvl = str(_tr.get('generalToleranceLevel') or '').strip().lower()
    if _lvl in ('f', 'm', 'c', 'v'):
        _req.append(f'未注尺寸公差按 GB/T 1804-{_lvl}。')
    _v = str(_tr.get('unnotedFillet') or '').strip()
    if _v:
        _req.append(f'未注圆角 {_v}。')
    _v = str(_tr.get('unnotedChamfer') or '').strip()
    if _v:
        _req.append(f'未注倒角 {_v}。')
    _v = str(_tr.get('unnotedRoughness') or '').strip()
    if _v:
        _req.append(f'未注表面粗糙度 {_v}。')
    _v = str(_tr.get('surfaceTreatment') or '').strip()
    if _v:
        _req.append(f'表面处理：{_v}。')
    _extra = str(_tr.get('extraNotes') or '').strip()
    if _extra:
        for _ln in _extra.splitlines():
            _ln = _ln.strip()
            if _ln:
                _req.append(_ln if _ln[-1:] in '。.' else _ln + '。')
    _fits = payload.get('fits') or None
    if isinstance(_fits, dict):
        _ft = str(_fits.get('target') or '').strip()
        _fc = str(_fits.get('code') or '').strip()
        _fits = {'target': _ft, 'code': _fc,
                 'text': f'{_ft} {_fc}'.strip()} if (_ft and _fc) else None
    _fix = payload.get('fixture') or {}
    emit({'stage': 'technical_requirements',
          'lines': _req,
          'numbered': [f'{_i}. {_s}' for _i, _s in enumerate(_req, 1)],
          'count': len(_req),
          'fits': _fits,
          'fixture': {'enabled': bool(_fix.get('enabled')),
                      'note': str(_fix.get('note') or '').strip()}})
    paper = (payload.get('paperSize') or 'A3').upper()
    landscape = payload.get('landscape', True)

    # 自动选 GB 优先比例 + 排布四视图，避免视图超框/挤在一起
    model_size = get_model_size(doc_model)
    if payload.get('sheetScale'):
        sc = payload['sheetScale']
        scale_num, scale_den = float(sc[0]), float(sc[1])
    else:
        # auto_fit_scale 会多降一档，给尺寸标注留出溢出余量
        scale_num = 1
        scale_den = auto_fit_scale(model_size, paper, landscape,
                                   requested_den=payload.get('forceScaleDen'))
    layout = payload.get('layout') or compute_layout(model_size, paper, scale_den, landscape)
    emit({'stage': 'layout',
          'modelSizeMm': [round(v * 1000, 1) for v in (model_size or (0, 0, 0))],
          'scale': f'{scale_num:g}:{scale_den:g}',
          'positions': {k: [round(v[0], 4), round(v[1], 4)]
                        for k, v in layout.items() if not k.startswith('_')}})

    dw = make_drawing(sw,
                      payload.get('template') or DEFAULT_TEMPLATE,
                      paper, landscape, payload.get('sheetFormat'),
                      scale_num, scale_den)
    emit({'stage': 'drawing_created', 'paper': paper, 'landscape': landscape,
          'scale': f'{scale_num:g}:{scale_den:g}'})

    views = payload.get('views') or ['front', 'top', 'right', 'isometric']
    extra = [k for k in views if k in ('isometric', 'left', 'back', 'bottom')]
    made, ref_view = create_standard_views(dw, model, extra_views=extra,
                                             scale_den=scale_den)
    emit({'stage': 'views', 'result': made})

    # ── 记录每个视图的边界，交给 AutoCAD 画框并重新排位 ──
    # 实测三条教训（都试过）：
    #   1) 直接给对齐的标准视图设 View.Position，SolidWorks 会为维持
    #      投影对齐重新排布整组，结果跑到图框外（实测 x=-35、y=357）。
    #   2) 在图纸上画的草图矩形**不会**随 DWG 导出
    #      （导出后 DSH_VIEWBOX 图层上 0 个实体）。
    #   3) 靠几何聚类猜视图边界不可靠（轴测图稀疏，会被拆成几簇）。
    # 所以：SolidWorks 只负责「报告每个视图的矩形边界」，
    # AutoCAD 收到边界后自己画框（保证进 DWG、可分图层），
    # 再用框把视图整块选中移到国标位置，最后删框。
    # ── 第一步：把视图摆好 ──
    # 用户要求：「在 SolidWorks 上就要把各个视图分开，而且距离拉远点」
    # ⚠ IView.Position 必须传 VARIANT 数组，传 tuple 会让所有视图
    # 挤到同一个位置（实测三个视图全变成 (0,130)）。
    #
    # ⚠ 两种策略必须配套，否则视图会留在图框外（实测溢出 130mm）：
    #   · spread_views_outside：故意摊到框外，等 AutoCAD 后处理再搬回框内
    #   · position_views_gb   ：直接在 SolidWorks 里摆进框内
    # 只要 AutoCAD 那步不跑，就不能用「摊到框外」。
    acad_will_run = bool(payload.get('acadDimension', True)) and bool(out_dwg)
    # 视图到底要不要让 AutoCAD 再搬一次？
    #   · 摊到框外（spread_outside）→ 必须搬回框内，moveViews=True
    #   · 已在 SolidWorks 里摆好（gb_layout）→ 再搬一次只会搬乱，moveViews=False
    # 实测：gb_layout 已经把视图放对（(107.6,72.4) 等全在 A3 内），
    # 但 AutoCAD 又按图层搬了一遍，结果两个视图压到标题栏上。
    used_spread = False
    if payload.get('layoutViews', True):
        try:
            if acad_will_run and payload.get('spreadOutside', True):
                placed_views = spread_views_outside(
                    dw, paper, landscape,
                    gap_factor=float(payload.get('viewGapFactor', 1.8)))
                used_spread = True
            else:
                # 没有 AutoCAD 兜底时，直接在 SolidWorks 里摆进图框
                placed_views = position_views_gb(
                    dw, paper, landscape,
                    gap_m=float(payload.get('viewGapMm', 60.0)) / 1000.0)
            if placed_views:
                emit({'stage': 'views_positioned', 'placed': placed_views,
                      'strategy': 'spread_outside' if used_spread else 'gb_layout'})
        except Exception as exc:
            emit({'stage': 'views_positioned', 'error': str(exc)})

    # ── 装配体：插入明细表（BOM）──
    # 装配图比零件图多两样东西：明细表 + 零件序号。
    # 明细表 API 的坑（实测 2026-11）：IView::InsertBomTable3 的第一个参数
    # 是 **UseAnchorPoint(bool)**，不是模板路径 —— 照老签名把模板路径塞第一个
    # 位置会报「类型不匹配。 (None, 1)」，很难看出来。
    bom_result = None
    balloon_result = None
    if is_assembly and payload.get('bom', True):
        try:
            bom_result = insert_bom_table(dw, paper, landscape,
                                          payload.get('bomTemplate') or BOM_TEMPLATE,
                                          anchor=int(payload.get('bomAnchor', 3)),
                                          bom_type=int(payload.get('bomType', 1)))
            emit({'stage': 'bom', **bom_result})
        except Exception as exc:
            bom_result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
            emit({'stage': 'bom', **bom_result})

    # ── 装配体：零件序号（气泡）──
    # 必须在明细表之后、保存之前做；有明细表时气泡编号才能和表里的「项目」对上。
    if is_assembly and payload.get('balloons', True):
        try:
            balloon_result = insert_balloons(
                dw,
                layout=int(payload.get('balloonLayout', 1)),
                style=int(payload.get('balloonStyle', 1)),
                size=int(payload.get('balloonSize', 2)))
            emit({'stage': 'balloons', **balloon_result})
        except Exception as exc:
            balloon_result = {'ok': False,
                              'error': f'{type(exc).__name__}: {exc}'}
            emit({'stage': 'balloons', **balloon_result})

    # ── 让视图显示隐藏线（内部结构以虚线导出）──
    # 用户要求：出「能看到内部」的视图。
    # swDisplayMode_e: swHIDDEN_GREYED = 1 → 隐藏线可见（导 DWG 后是 HIDDEN 线型虚线）
    # 实测：默认模式下 DWG 只有 193 个可见轮廓实体；
    #       设成 1 之后变 314 个，其中 214 条是 HIDDEN 虚线，
    #       前视图区就有 85 条，内壁 x=117.2/118.8 清晰可见 → 可以标内腔尺寸。
    if payload.get('showHiddenLines', True):
        try:
            hv = dw.GetFirstView
            hi = 0
            hn = 0
            while hv is not None and hi < 15:
                if hi > 0:
                    try:
                        hv.SetDisplayMode3(False, 1, False, False)
                        hn += 1
                    except Exception:
                        pass
                hv = hv.GetNextView
                hi += 1
            try:
                dw.EditRebuild3()
            except Exception:
                pass
            emit({'stage': 'hidden_lines', 'views': hn})
        except Exception as exc:
            emit({'stage': 'hidden_lines', 'error': str(exc)})

    view_boxes = []
    try:
        view_boxes = collect_view_boxes(dw)
        if view_boxes:
            emit({'stage': 'view_boxes', 'count': len(view_boxes),
                  'boxes': view_boxes})
    except Exception as exc:
        emit({'stage': 'view_boxes', 'error': str(exc)})
    # ── 剖视图：按国标按需添加，不无脑加 ──
    # 判断依据：模型里有切除/孔/抽壳类特征（说明有内腔或内孔），
    # 三视图看不出壁厚，才需要剖视表达。纯外形件加剖视图是画蛇添足。
    section_ok = None
    section_reason = None
    sec_mode = payload.get('sectionView', 'auto')   # 'auto' | true | false
    if sec_mode is True:
        need_section, reason = True, '调用方指定'
    elif sec_mode is False:
        need_section, reason = False, '调用方关闭'
    else:
        cavity = detect_internal_cavity(doc_model)
        need_section = cavity['needSection']
        reason = cavity['reason']
        emit({'stage': 'cavity_check',
              'needSection': need_section,
              'hasShell': cavity['hasShell'],
              'cutCount': cavity['cutCount'],
              'volumeRatio': (round(cavity['volumeRatio'], 4)
                              if cavity['volumeRatio'] is not None else None),
              'volumeMm3': (round(cavity['volumeMm3'])
                            if cavity['volumeMm3'] else None),
              'cutNames': cavity['cutNames'][:4],
              'reason': reason})
    if need_section and ref_view is not None:
        # 剖视图按国标应「位于剖切符号箭头所指方向」，并在母视图附近。
        # 布局策略：三视图占左上/右上/左下、轴测占右下，
        # 剖视图放在「右下区、轴测图下方偏左」，并用可画区边界算，
        # 绝不能压到标题栏（标题栏占右下角，高度约 55mm）。
        sec_place = payload.get('sectionPlace')
        if not sec_place:
            try:
                x0, y0, dw_w, dh_h = drawable_area(paper, landscape)
                title_h = 0.055
                # 可画区下边界之上、标题栏右侧留白处
                sx = x0 + dw_w * 0.62
                sy = y0 + title_h + dh_h * 0.28
                sec_place = (sx, sy)
            except Exception:
                sec_place = None
        sec = add_section_view(dw, ref_view,
                               label=payload.get('sectionLabel', 'A'),
                               direction=payload.get('sectionDirection', 'vertical'),
                               place=sec_place)
        section_ok = sec is not None
        if section_ok:
            made['section'] = True
            try:
                made['section_name'] = sec.GetName2
            except Exception:
                pass
    section_reason = reason
    emit({'stage': 'section_view', 'ok': section_ok, 'reason': reason})

    # ── 局部放大图：同样按需 ──
    detail_ok = None
    detail_cfg = payload.get('detailView')
    if detail_cfg and ref_view is not None:
        d = add_detail_view(dw, ref_view,
                            label=detail_cfg.get('label', 'A'),
                            scale=float(detail_cfg.get('scale', 2.0)),
                            radius=float(detail_cfg.get('radius', 0.020)),
                            place=detail_cfg.get('place'))
        detail_ok = d is not None
        if detail_ok:
            made['detail'] = True
            try:
                made['detail_name'] = d.GetName2
            except Exception:
                pass
        emit({'stage': 'detail_view', 'ok': detail_ok})

    # 标注参数参考 github.com/BlakeOlinger/AutoDimensioner 的实现：
    #   InsertModelAnnotations3(swImportModelItemsFromEntireModel=0,
    #                          swInsertDimensionsMarkedForDrawing=32768,
    #                          AllViews=true, DuplicateDims=false,
    #                          HiddenFeatureDims=false, UsePlacementInSketch=false)
    # 实测这个组合比 DuplicateDims=True 多出近一倍尺寸（34 vs 19），
    # 因为 False 才允许同一模型尺寸在不同视图各自显示。
    default_types = payload.get('annotationTypes', SW_INSERT_DIMENSIONS_MARKED)
    # 注意：参考代码用 False，但那是在它自己的装配体+双层视图场景下。
    # 实测本机零件上用 False 会插 51 个尺寸、排不下并压到标题栏，反而更差。
    # 所以这里默认 True（19 个），更符合「清晰」要求。
    default_dup = payload.get('duplicateDims', True)
    anns = annotate(dw, default_types, duplicate_dims=default_dup)
    dims = count_dims(dw)
    emit({'stage': 'annotated', 'objects': len(anns), 'dimensions': dims})

    # 按 GB/T 4458.4 去重：同一要素不得在多视图重复标注
    dedupe_removed = 0
    if payload.get('dedupeDimensions', True):
        try:
            dedupe_removed, kept = dedupe_dimensions(dw)
            dims = count_dims(dw)
            emit({'stage': 'deduped', 'removed': dedupe_removed, 'kept': kept})
        except Exception as exc:
            emit({'stage': 'dedupe_error', 'error': str(exc)})

    # 按国标「必要、充分、无矛盾」处理尺寸：
    # 不问青红皂白全删是不对的。国标的做法是——
    #   细节尺寸（<1mm 的非功能结构）：删掉，用一般公差覆盖
    #   从属/重复尺寸：保留但标成 REF 参考尺寸（标「仅供参考」而非删除）
    #   总体/定形/定位尺寸：保留为正式尺寸
    pruned = 0
    ref_marked = 0
    if payload.get('selectKeyDimensions', True):
        try:
            groups = classify_dimensions(dw)
            emit({'stage': 'dimensions_classified',
                  'overall': len(groups['overall']),
                  'form': len(groups['form']),
                  'position': len(groups['position']),
                  'detail': len(groups['detail'])})

            # 1) 删掉极小细节尺寸
            min_mm = float(payload.get('minDimMm', 1.0))
            for it in groups['detail']:
                if it['mm'] < min_mm:
                    try:
                        dw.ClearSelection2(True)
                        it['display'].Select(False, 0)
                        dw.EditDelete()
                        pruned += 1
                    except Exception:
                        pass

            # 2) 超量时把「定位」类里最不关键的标成参考尺寸（不删）
            max_dims = int(payload.get('maxDims', 40))
            official = (len(groups['overall']) + len(groups['form'])
                        + len(groups['position']) - pruned)
            if official > max_dims and payload.get('markRefInsteadOfDelete', True):
                excess = official - max_dims
                # 定位尺寸里取最后 excess 个（越靠后越可能是从属尺寸）
                pos_list = groups['position']
                if excess > len(pos_list):
                    excess = len(pos_list)
                to_ref = pos_list[len(pos_list) - excess:] if excess > 0 else []
                for it in to_ref:
                    try:
                        # 注意：GetText(5) 常取回空串（不是实际数值文本），
                        # 直接拿它拼接会生成 "REF2"、"(64" 这种残缺标注。
                        # 数值以 it['mm'] 为准重新生成文本。
                        mm = float(it.get('mm') or 0)
                        if mm <= 0:
                            continue
                        txt = ('%.2f' % mm).rstrip('0').rstrip('.')
                        # GB/T 4458.4：参考尺寸加圆括号，如 (60)
                        it['display'].SetText(5, '(' + txt + ')')
                        ref_marked += 1
                    except Exception:
                        pass

            try:
                dw.ClearSelection2(True)
            except Exception:
                pass
            dims = count_dims(dw)
            emit({'stage': 'dimensions_pruned', 'removed': pruned,
                  'refMarked': ref_marked, 'kept': dims})
        except Exception as exc:
            emit({'stage': 'prune_error', 'error': str(exc)})

    # 去掉尺寸引线，图面更干净（参考 NDwgAutoTool 的做法）
    if payload.get('stripLeaders', True):
        try:
            stripped = strip_dimension_leaders(dw)
            if stripped:
                emit({'stage': 'leaders_stripped', 'count': stripped})
        except Exception as exc:
            emit({'stage': 'leader_error', 'error': str(exc)})

    # 按 GB/T 4458.4 把尺寸分层排开（否则会挤成一团、互相穿插）
    if payload.get('arrangeDimensions', True):
        arr = arrange_dimensions(dw, float(payload.get('dimSpacing', 0.010)))
        emit({'stage': 'dimension_arranged', **arr})

    # 检查是否溢出图框（GB 要求图样不得超出图框）
    # 先把越界尺寸拉回图框内，再复查
    clamped = 0
    if payload.get('clampDimensions', True):
        try:
            clamped = clamp_dimensions_inside(dw, paper, landscape)
            if clamped:
                emit({'stage': 'dimensions_clamped', 'fixed': clamped})
        except Exception as exc:
            emit({'stage': 'clamp_error', 'error': str(exc)})

    overflow_ok, overflow_detail = check_overflow(dw, paper, landscape)
    emit({'stage': 'overflow_check', 'ok': overflow_ok, 'detail': overflow_detail})

    tol_count = 0
    tol_spec = payload.get('tolerance')
    if tol_spec and tol_spec.get('enabled', True):
        tol_count = apply_tolerances(dw, tol_spec)
        emit({'stage': 'tolerances', 'applied': tol_count})

    rough = payload.get('surfaceFinish')
    rough_ok = None
    if rough and rough.get('enabled', True):
        rough_ok = add_surface_finish(
            dw,
            str(rough.get('value', '3.2')),
            float(rough.get('x', 0.15)),
            float(rough.get('y', 0.06)))
        emit({'stage': 'surface_finish', 'ok': rough_ok})

    # 图纸信息（材质/图纸名称/编号/版本…）写入标题栏属性
    info = payload.get('titleBlock') or {}
    info_written = set_document_custom_properties(dw, info)
    if info_written:
        update_title_block(dw)
        emit({'stage': 'title_block', 'written': info_written})

    saved = save_and_export(dw, out_drw, out_dxf, out_dwg)
    emit({'stage': 'saved', **saved})

    # 用完就关：模型零件在导出后就没用了（后续预览用工程图 dw，不用零件）。
    # 不关会一直堆在 SolidWorks 里，文档一多 InsertModelAnnotations3 会认错图纸。
    if payload.get('closeModelAfter', True):
        try:
            if doc_model is not None:
                sw.CloseDoc(doc_model.GetTitle)
                emit({'stage': 'model_closed', 'title': str(doc_model.GetTitle)})
        except Exception as exc:
            emit({'stage': 'model_closed', 'error': str(exc)})

    # ===== 第二步：在 AutoCAD 里做标注（新路线，比 SolidWorks 自动标注可控）=====
    # 理由：SolidWorks 的 InsertModelAnnotations3 只能插模型里本来就有的草图尺寸，
    # 导入实体一个都插不出；而且位置/样式由模板决定，不好精细控制。
    # AutoCAD 这边几何已经投影成 2D，可以直接量几何建尺寸，
    # 还能用 SetVariable 把 DIMTXT/DIMASZ/DIMLFAC 全套设成国标样式。
    acad_dim_result = None
    acad_result = None
    tb = payload.get('titleBlock') or {}
    want_acad = acad_will_run
    if out_dwg and saved.get('dwg') and (want_acad or tb):
        try:
            import acad_dimension
            opts = dict(payload.get('acadOptions') or {})
            opts.setdefault('paper', paper)
            # 关键：告诉 AutoCAD 图纸比例，它要用 DIMLFAC 把尺寸数字换算成真实值
            opts.setdefault('scaleDen', scale_den)
            opts.setdefault('scaleNum', scale_num)

            # 把 SolidWorks 报告的视图边界交给 AutoCAD：
            # 它据此画框 → 按框整块选中视图 → 移到国标位置 → 删框
            if view_boxes:
                opts.setdefault('viewBoxes', view_boxes)
            # 只有「已经把视图摊到框外」时才让 AutoCAD 搬回框内；
            # 视图本来就在框内（gb_layout）就别再搬一次，否则会压到标题栏上。
            opts.setdefault('moveViews', used_spread)
            if want_acad:
                acad_dim_result = acad_dimension.dimension_dwg(
                    out_dwg, opts,
                    payload.get('acadVisible', False),
                    None,   # None = 就地保存
                    exe_hint=payload.get('autocadExe'))
                emit({'stage': 'acad_dimension', **(acad_dim_result or {})})

            # 标题栏也用 AutoCAD 填（SolidWorks 图框是静态文字，填不进去）
            if tb:
                acad_result = edit_dwg_title_block(
                    out_dwg, tb, payload.get('acadVisible', False),
                    exe_hint=payload.get('autocadExe'))
                emit({'stage': 'acad_title_block', **(acad_result or {})})
        except Exception as exc:
            emit({'stage': 'acad_post_error', 'error': str(exc),
                  'type': type(exc).__name__})

        # 用完就关：AutoCAD 里这个 DWG 已经不需要了，留着只是占内存。
        # 用户要求「打开过的界面如果后续用不上了，就顺带关掉」。
        # 只关本文档，不动 AutoCAD 进程，也不碰用户自己开的图。
        if payload.get('closeAcadAfter', True):
            try:
                from acad_plot import close_document
                ok_close, msg = close_document(
                    out_dwg, exe_hint=payload.get('autocadExe'))
                emit({'stage': 'acad_closed', 'closed': bool(ok_close),
                      'note': msg})
            except Exception as exc:
                emit({'stage': 'acad_closed', 'closed': False,
                      'note': f'{type(exc).__name__}: {exc}'})

    preview = None
    if payload.get('preview', True):
        # 顺序坑：AutoCAD 那一步会把 SolidWorks 挤到后台，此时 dw 不是活动
        # 文档，SaveBMP 会失败。先把工程图重新激活再截图。
        try:
            from win32com.client import VARIANT as _V
            from pythoncom import VT_BYREF as _VB, VT_I4 as _I4
            _e = _V(_VB | _I4, 0)
            sw.ActivateDoc3(dw.GetTitle, True, 0, _e)
            time.sleep(1.0)
        except Exception:
            pass
        preview = render_preview(dw, os.path.join(out_dir, base + '.png'))
        emit({'stage': 'preview', 'path': preview})

    # 收尾：清掉本次的工程图文档，免得下次出图认错图纸
    # （不杀 SolidWorks 进程——强杀会留下"错误报告"模态框，那样更糟）
    try:
        closed = sweep_documents(sw)
        if closed:
            emit({'stage': 'cleanup', 'closed': closed})
    except Exception:
        pass

    return {
        'ok': bool(saved.get('drw')),
        'modelInfo': model_info,
        'isAssembly': is_assembly,
        'bom': bom_result,
        'balloons': balloon_result,
        'model': model,
        'drawing': out_drw,
        'dxf': out_dxf,
        'dwg': out_dwg,
        'dimensions': dims,
        'dedupeRemoved': dedupe_removed,
        'overflowOk': overflow_ok,
        'overflowDetail': overflow_detail,
        'annotationObjects': len(anns),
        'tolerancesApplied': tol_count,
        'surfaceFinish': rough_ok,
        'titleBlockWritten': info_written,
        'titleBlockInDwg': acad_result,
        'acadDimension': acad_dim_result,
        'sectionView': section_ok,
        'sectionReason': section_reason,
        'detailView': detail_ok,
        'views': made,
        'preview': preview,
        'saved': saved,
    }


def probe(payload):
    """环境探测：**不启动** SolidWorks / AutoCAD，只报告路径与状态。

    为什么不在 probe 里启动：启动 SolidWorks 要 30~60 秒，
    用户点一下「检测路径」却卡住一分钟会很困惑。
    这里只回答「路径找到没、程序在不在跑」，真正启动留到出图时。
    """
    info = {'ok': True, 'solidworks': None, 'template': None, 'sheetFormats': []}

    # 「启动路径」探测结果
    try:
        info['paths'] = apps.probe_info(
            payload.get('solidworksExe'), payload.get('autocadExe'))
    except Exception as exc:
        info['paths'] = {'error': str(exc)}

    sw_path = (info.get('paths') or {}).get('solidworks') or {}
    ac_path = (info.get('paths') or {}).get('autocad') or {}

    # 路径都没找到 → 才算探测失败
    if not sw_path.get('exists') and not ac_path.get('exists'):
        info['ok'] = False

    # SolidWorks：只在已运行时才尝试 COM 连接（不主动拉起）
    if sw_path.get('running'):
        try:
            kill_report_dialogs()
            sw = connect(False, exe_hint=payload.get('solidworksExe'))
            info['solidworks'] = {'revision': str(sw.RevisionNumber),
                                  'running': True}
        except Exception as exc:
            info['solidworks'] = {'running': True, 'error': str(exc)}
    else:
        info['solidworks'] = {
            'running': False,
            'note': ('未检测到运行中的 SolidWorks；出图时会按上面的路径启动'
                     if sw_path.get('exists') else
                     '没找到 SLDWORKS.exe，请在面板里填启动路径'),
        }

    tpl = payload.get('template') or DEFAULT_TEMPLATE
    info['template'] = {'path': tpl, 'exists': os.path.exists(tpl)}
    if os.path.isdir(SHEETFORMAT_DIR):
        info['sheetFormats'] = sorted(
            f for f in os.listdir(SHEETFORMAT_DIR) if f.lower().endswith('.slddrt'))
    info['python'] = sys.version.split()[0]
    return info


def close_all(payload):
    kill_report_dialogs()
    sw = connect(False, exe_hint=payload.get('solidworksExe'))
    sw.CloseAllDocuments(True)
    return {'ok': True, 'closed': True}


def read_payload():
    """任务来源：python engine.py <payload.json>  或  stdin。

    文件方式优先——Windows 上用管道喂中文/反斜杠路径容易被编码搞坏。
    """
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        with open(sys.argv[1], 'r', encoding='utf-8-sig') as fh:
            return json.load(fh)
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def print_pdf(payload):
    """按工程图最外层包围框，把 DWG 一键打印成 PDF。

    范围来源：图纸里面积最大的闭合多段线（就是那张「最外面一圈」的边框）。
    即使该图层被关闭/冻结也能读到 —— 用户把包围框藏起来不影响本功能。
    输出默认黑白（monochrome.ctb），DWG 里的图层颜色原样保留。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from acad_plot import export_pdf

    dwg = payload.get('dwg') or ''
    if not dwg:
        return {'ok': False, 'error': '缺少 dwg 参数'}
    if not os.path.isfile(dwg):
        return {'ok': False, 'error': f'DWG 不存在: {dwg}'}

    pdf = payload.get('pdf') or (os.path.splitext(dwg)[0] + '.pdf')
    paper = (payload.get('paper') or '').strip().upper() or None
    mono = payload.get('monochrome', True)
    if isinstance(mono, str):
        mono = mono.strip().lower() not in ('0', 'false', 'no', '')
    try:
        timeout_s = float(payload.get('timeoutSec') or 120)
    except Exception:
        timeout_s = 120.0

    emit({'stage': 'pdf_start', 'dwg': dwg, 'pdf': pdf,
          'paper': paper, 'monochrome': bool(mono),
          'autocadExe': payload.get('autocadExe') or None})

    res = export_pdf(dwg, pdf, paper=paper, monochrome=bool(mono),
                     timeout_s=timeout_s,
                     exe_hint=payload.get('autocadExe'),
                     log=lambda m: emit({'stage': 'pdf_log', 'msg': m}))
    return res


def run():
    payload = read_payload()
    action = payload.get('action', 'build')
    try:
        if action == 'build':
            result = build(payload)
        elif action == 'drawset':
            # 整套图纸：装配总图 + 每个零件图 → 合并成一个 DXF
            import drawset
            result = drawset.build_set(payload, emit)
        elif action == 'probe':
            result = probe(payload)
        elif action == 'close':
            result = close_all(payload)
        elif action == 'pdf':
            result = print_pdf(payload)
        else:
            result = {'ok': False, 'error': f'未知 action: {action}'}
    except Exception as exc:
        result = {
            'ok': False,
            'error': str(exc),
            'errorType': type(exc).__name__,
            'trace': traceback.format_exc()[-3000:],
        }
    emit({'stage': 'done', **result})
    sys.exit(0 if result.get('ok') or action == 'probe' else 1)






if __name__ == '__main__':
    run()
