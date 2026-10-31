# -*- coding: utf-8 -*-
"""
dsh-cad-draw / acad_dimension.py

在 AutoCAD 里做自动标注 —— 绕开 SolidWorks 自动标注的瓶颈。

═══════════ 为什么这么做（设计理由）═══════════

SolidWorks 的 InsertModelAnnotations3 有两个硬限制：
  1) 只能插入「模型里本来就有的草图尺寸」。STEP 导入的哑实体没有草图，
     一个尺寸都插不出来（实测 144面/450边/0草图 → 0 个尺寸）。
  2) 标注位置、样式由 SolidWorks 工程图模板决定，不好精细控制外观。

而 AutoCAD 这边：
  * 几何已经投影成 2D 线了，可以直接量任意线段/圆弧/圆心 → 建尺寸
  * 用 SetVariable 设 DIMTXT/DIMASZ/... 就是完整的国标标注样式
  * AddDimRotated / AddDimDiametric / AddDimRadial / AddDimAngular 齐全

所以「出图 → 导出 DWG → AutoCAD 按国标样式自动标注」是更稳的路线。

═══════════ 本机实测结论（AutoCAD 2018）═══════════

1) DimStyle 对象上的 Dim* 属性在 pywin32 晚绑定下**读不到也写不了**
   （报 "Property 'Add.Dimtxt' can not be set"）。
   正确做法是用 doc.SetVariable/GetVariable 设标注系统变量，17 个全成功。

2) 创建尺寸时点的参数必须传 VARIANT 数组：
     VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (x, y, z))
   直接传 tuple 会报 "发生意外" (-2147024809)。

3) 常用方法：AddDimRotated(p1, p2, dimline, angle)
             AddDimAligned(p1, p2, dimline)
             AddDimDiametric(p1, p2, leader)
             AddDimRadial(center, chord, leader)
             AddDimAngular(angleVertex, p1, p2, arcpoint)
"""

import os
import sys
import time
import math
import json
import traceback

# ---------------- GB 标注样式变量（GB/T 4458.4 + GB/T 14691）----------------
# 字高 3.5mm 是国标最小推荐值；线宽按 GB 分粗细：
#   粗实线 b = 0.5~0.7（轮廓），细实线 = b/2 ≈ 0.25~0.35（尺寸线/尺寸界线）
# ⚠ 原来 DIMLWD/DIMLWE 设成 9（0.09mm）——能打印的最细值，但
#   GB/T 4458.4 要求尺寸线是**细实线 b/2**，0.09 已经细到打印发虚，
#   和 0.7 的轮廓线配对也不成比例。改成 25（0.25mm）。
GB_DIM_VARS = {
    'DIMTXT': 3.5,      # 尺寸文字高度 (mm)
    'DIMASZ': 2.5,      # 箭头大小（GB：箭头长约 2.5，约为字高 2/3）
    'DIMEXE': 2.0,      # 尺寸界线超出尺寸线 2~3mm
    'DIMEXO': 2.0,      # 尺寸界线离开轮廓 ≥2mm（GB/T 4458.4 明确要求）
    'DIMGAP': 1.0,      # 文字与尺寸线间隙
    'DIMTAD': 1,        # 文字置于尺寸线上方（国标）
    'DIMTIH': 0,        # 尺寸界线内文字水平
    'DIMTOH': 0,        # 尺寸界线外文字水平
    'DIMDEC': 2,        # 小数位 2
    'DIMZIN': 8,        # 消除尾随零
    'DIMLFAC': 1.0,     # 线性比例
    'DIMSCALE': 1.0,    # 全局比例
    'DIMCLRD': 7,       # 尺寸线 = 白/黑（显式 7，别用 256 ByBlock）
    'DIMCLRE': 7,       # 尺寸界线
    'DIMCLRT': 7,       # 文字
    # ⚠ 原来写 256（ByBlock）。ByBlock 在「无块上下文」的尺寸实体上
    #   会解析成白色，单色打印（monochrome.ctb）时白字打在白纸上 →
    #   PDF 里一个字都提取不到，看上去就像「完全没标注」。
    #   实测：尺寸线/箭头都在，就是没有数字。
    'DIMTDEC': 2,       # 公差小数位
    'DIMTFAC': 1.0,     # 公差文字比例
    'DIMATFIT': 3,      # 箭头/文字都放不下时移出
    'DIMJUST': 0,       # 文字居中
    # 线宽：1/100mm。尺寸线/尺寸界线 = 细实线 0.25mm
    'DIMLWD': 25,       # 尺寸线线宽 0.25mm（细实线）
    'DIMLWE': 25,       # 尺寸界线线宽 0.25mm
}

# GB 线宽搭配：粗实线 0.5、细实线 0.25、细虚线 0.25、细点画线 0.25
LW_THICK = 50          # 可见轮廓线（粗实线）0.50mm
LW_THIN = 25           # 尺寸线/尺寸界线/虚线/中心线（细线）0.25mm
LINETYPE_CENTER = 'CENTER'   # 点画线（中心线）


def emit(obj):
    sys.stdout.write('###DSH_CAD###' + json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def _vec(t):
    """把点转成 AutoCAD COM 需要的 VARIANT 数组。

    注意：t 可能是 tuple，也可能是 AutoCAD 返回的 VARIANT/数组，
    不能无脑 float(x)（会报 "float() argument must be ... not 'VARIANT'"）。
    """
    from win32com.client import VARIANT
    import pythoncom
    vals = []
    for x in t:
        try:
            vals.append(float(x))
        except Exception:
            try:
                vals.append(float(x[0]))   # 万一是嵌套的
            except Exception:
                vals.append(0.0)
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(vals))


def connect_acad(visible=False, timeout=120, exe_hint=None):
    """连上 AutoCAD，并等到 Documents 集合真正可用。

    实测坑：Dispatch 成功后立刻 acad.Documents.Open 会报
    AttributeError '<unknown>.Open' —— 因为 AutoCAD 还在启动，
    Documents 集合尚未暴露。必须轮询到它能列出为止。

    exe_hint：面板里配的 acad.exe 路径；没在跑就按这个路径拉起来。
    """
    import win32com.client as win32

    # 先确保进程在跑（按用户指定的路径启动，找不到就退回直接 Dispatch）
    try:
        import apps
        apps.ensure_running(apps.AUTOCAD, exe_hint)
    except Exception:
        pass

    start = time.time()
    acad = None
    while time.time() - start < timeout:
        try:
            acad = win32.Dispatch('AutoCAD.Application')
            try:
                acad.Visible = bool(visible)
            except Exception:
                pass
            _ = acad.Version
            # 等 Documents 可用
            for _ in range(60):
                try:
                    docs = acad.Documents
                    _ = docs.Count
                    return acad
                except Exception:
                    time.sleep(1)
        except Exception:
            acad = None
        time.sleep(2)
    raise RuntimeError('连不上 AutoCAD（或 Documents 集合一直不可用）。')


def open_doc(acad, path, timeout=180):
    """打开文档，带重试（AutoCAD 启动中 Documents.Open 会短暂不可用）。

    ⚠ 不能直接 `return acad.Documents.Open(...)`：晚绑定下拿到的对象
    不暴露完整 Document 接口，访问 .ModelSpace 会报
    `AttributeError: Open.ModelSpace`（实测踩过）。
    必须拿到之后再取 acad.ActiveDocument，那才是可用的 Document 代理。
    """
    target = os.path.abspath(path)
    start = time.time()
    last = None
    while time.time() - start < timeout:
        try:
            acad.Documents.Open(target)
            # Open 之后 ActiveDocument 可能还没切过来，轮询等它可用
            for _ in range(20):
                try:
                    doc = acad.ActiveDocument
                    if doc is not None:
                        try:
                            doc.ModelSpace.Count          # 探活
                            return doc
                        except Exception:
                            pass
                except Exception:
                    pass
                time.sleep(0.5)
            last = 'Documents.Open 返回的对象不可用（ModelSpace 取不到）'
        except Exception as exc:
            last = exc
        time.sleep(2)
    raise RuntimeError(f'AutoCAD 打开失败：{last}')


def setup_gb_dimstyle(doc, name='GB', scale_den=None, scale_num=1):
    """建立国标标注样式并设为当前。

    用 SetVariable 而不是 DimStyle 属性——后者在晚绑定下不可写。

    ⚠ 关键参数 scale_den：SolidWorks 导出的 DWG 里，视图几何是按图纸
    比例缩放过的（实测 1:2 时 113mm 的零件在图上只剩 56.5mm）。
    AutoCAD 尺寸默认显示「实测长度」，会显示 56.5 而不是 113，
    这在国标图纸上是错的（国标要求标真实尺寸）。
    必须用 DIMLFAC = 1/比例 放大回去：1:2 → DIMLFAC=2。

    返回 (样式名, 成功设置的变量数)
    """
    # 建样式（已存在就复用）
    try:
        doc.DimStyles.Add(name)
    except Exception:
        pass
    try:
        doc.ActiveDimStyle = doc.DimStyles.Item(name)
    except Exception:
        pass

    ok = 0
    for k, v in GB_DIM_VARS.items():
        try:
            doc.SetVariable(k, v)
            ok += 1
        except Exception:
            pass

    # 图纸比例补偿
    if scale_den:
        try:
            lfac = float(scale_den) / float(scale_num or 1)
            doc.SetVariable('DIMLFAC', lfac)
            ok += 1
            emit({'stage': 'acad_scale_factor',
                  'scale': f'{scale_num}:{scale_den}', 'dimlfac': lfac})
        except Exception:
            pass
    return name, ok

# ───────────────── 图幅与字高 ─────────────────
# GB/T 14689 图幅（横向，mm）
PAPER_MM = {
    'A5': (210.0, 148.0), 'A4': (297.0, 210.0), 'A3': (420.0, 297.0),
    'A2': (594.0, 420.0), 'A1': (841.0, 594.0), 'A0': (1189.0, 841.0),
}
# GB/T 14691 字高系列：1.8 2.5 3.5 5 7 10 14 20
# 按图幅推荐：小图纸 3.5，大图纸逐级加大，保证「适合人看」
PAPER_TEXT_H = {
    'A5': 2.5, 'A4': 3.5, 'A3': 3.5, 'A2': 5.0, 'A1': 7.0, 'A0': 10.0,
}


def paper_size_mm(paper, landscape=True):
    pw, ph = PAPER_MM.get(str(paper).upper(), (420.0, 297.0))
    if not landscape:
        pw, ph = ph, pw
    return pw, ph


def text_height_for_paper(paper, view_h=None, max_ratio=0.12):
    """按图幅（并参考视图大小）自动选标注字高，保证看得清。

    GB/T 14691 规定字高不小于 3.5mm（小图）并给出优先系列。
    但如果视图本身很小（比如局部小零件），固定 3.5mm 会显得字很大，
    所以再加一层：字高不超过视图高度的 max_ratio（默认 12%）。

    返回 (字高 mm, 依据说明)
    """
    base = PAPER_TEXT_H.get(str(paper).upper(), 3.5)
    if view_h and view_h > 0:
        cap = view_h * max_ratio
        if cap < base:
            # 向下取 GB 系列里不小于 cap 的最小值
            for h in (1.8, 2.5, 3.5, 5.0, 7.0, 10.0):
                if h >= cap:
                    return h, f'视图高 {view_h:.0f}mm 限制，字高取 {h}mm'
            return base, f'图幅推荐 {base}mm'
    return base, f'图幅推荐 {base}mm'


# ───────────────── 视图定位到图框内固定位置 ─────────────────
def paper_targets(paper='A3', landscape=True, title_h=55.0):
    """国标四宫格目标中心点（mm）：主视/左视/俯视/轴测。

    GB/T 4458.1 第一角投影配置：
        主视  左视      ← 高平齐
        俯视  轴测      ← 长对正（主↔俯）
    """
    pw, ph = paper_size_mm(paper, landscape)
    left, other = 25.0, 5.0
    x0, y0 = left, other
    dw = pw - left - other
    dh = ph - other * 2 - title_h
    return {
        'front':     (x0 + dw * 0.26, y0 + dh * 0.72),
        'right':     (x0 + dw * 0.70, y0 + dh * 0.72),
        'top':       (x0 + dw * 0.26, y0 + dh * 0.24),
        'isometric': (x0 + dw * 0.70, y0 + dh * 0.24),
    }


def _shift_cluster(cluster, dx, dy):
    """把一簇几何整体平移（AutoCAD Entity.Move）。"""
    moved = 0
    for key in ('lines', 'circles', 'arcs'):
        for item in cluster.get(key, []):
            obj = item[-1]
            try:
                obj.Move(_vec((0.0, 0.0, 0.0)), _vec((dx, dy, 0.0)))
                moved += 1
            except Exception:
                pass
    bb = cluster['bbox']
    cluster['bbox'] = (bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy)
    return moved


def layout_views_in_frame(clusters, paper='A3', landscape=True,
                          top_n=4, title_h=55.0):
    """把视图簇搬到图框内的国标固定位置。

    用户思路：SolidWorks 里视图先摊在框外，导出 DWG 后在 AutoCAD 里
    「一个个挪到图内固定位置」。这里就是那一步。

    视图归属判断：用各簇中心点的相对象限。
        左上 → 主视    右上 → 左视
        左下 → 俯视    右下 → 轴测
    这样不依赖具体是哪个视图，只要 SolidWorks 的排布保持相对关系就行。

    返回 {'front': (x,y), ...} 实际落位
    """
    big = [c for c in clusters
           if (c['bbox'][2] - c['bbox'][0]) > 8.0
           and (c['bbox'][3] - c['bbox'][1]) > 8.0][:top_n]
    if not big:
        return {}

    xs = [(c['bbox'][0] + c['bbox'][2]) / 2.0 for c in big]
    ys = [(c['bbox'][1] + c['bbox'][3]) / 2.0 for c in big]
    mx = (min(xs) + max(xs)) / 2.0
    my = (min(ys) + max(ys)) / 2.0

    targets = paper_targets(paper, landscape, title_h)
    used = set()
    placed = {}

    for c, cx, cy in zip(big, xs, ys):
        left_side = cx <= mx
        top_side = cy >= my
        if left_side and top_side:
            key = 'front'
        elif (not left_side) and top_side:
            key = 'right'
        elif left_side and (not top_side):
            key = 'top'
        else:
            key = 'isometric'
        # 冲突时找空闲位
        if key in used:
            for alt in ('isometric', 'right', 'top', 'front'):
                if alt not in used:
                    key = alt
                    break
        used.add(key)

        tx, ty = targets[key]
        dx, dy = tx - cx, ty - cy
        _shift_cluster(c, dx, dy)
        placed[key] = (round(tx, 1), round(ty, 1))

    return placed


# ───────────────── 分层管理：一视图一图层，图框单独一层 ─────────────────
# 用户方案（比几何聚类/包围框可靠得多）：
#   「把各个视图都分图层，工程图框也一个图层，按图层来移动」
#
# 为什么这是对的：
#   SolidWorks 的 IView **没有** Layer 属性（实测 IView 上零个含 Layer 的成员），
#   所以视图没法在 SolidWorks 侧分层，只能导出后在 AutoCAD 里分。
#   一旦每个视图独占一个图层，「哪些线属于这个视图」就不再需要猜——
#   直接按图层选，整块移动绝不会撕碎，标注也能逐层做。
LAYER_FRAME = 'DSH_FRAME'        # 图框 + 分区标记
LAYER_TITLE = 'DSH_TITLE'        # 标题栏
LAYER_BOM = 'DSH_BOM'            # 明细表（独立图层，不参与视图搬动）
LAYER_CENTER = 'DSH_CENTER'      # 中心线（GB 细点画线）
VIEW_LAYER_FMT = 'DSH_VIEW_{}'   # 每个视图一层：DSH_VIEW_1 / _2 …

# 视图图层顺序 → 国标位置
VIEW_TARGET_ORDER = ['front', 'top', 'right', 'isometric']


def ensure_view_layers(doc, count):
    """建好 FRAME / TITLE / VIEW_1..N 图层，返回视图图层名列表。"""
    ensure_layer(doc, LAYER_FRAME, color=7, plottable=True, locked=False,
                 description='工程图框与分区标记')
    ensure_layer(doc, LAYER_TITLE, color=7, plottable=True, locked=False,
                 description='标题栏')
    names = []
    for i in range(1, max(1, count) + 1):
        nm = VIEW_LAYER_FMT.format(i)
        ensure_layer(doc, nm, color=7, plottable=True, locked=False,
                     description=f'视图 {i}')
        names.append(nm)
    return names


def draw_centerlines(doc, view_layers, extend=3.0, dedup_tol=0.5):
    """给视图里的圆/圆弧画中心线（GB 细点画线）。

    GB/T 4457.4：中心线用**细点画线**，两端应超出轮廓 2~5mm。
    插件原来完全没画中心线——按国标这是硬伤：孔、圆柱、回转体
    都必须有中心线，否则读图时无法判断孔位基准。

    做法：对每个圆/圆弧圆心画一横一竖两条点画线，
    长度 = 半径 + extend（两端各超出一段）。同心圆只画一次。
    """
    ms = doc.ModelSpace
    lay = None
    try:
        lay = ensure_layer(doc, LAYER_CENTER, color=7)
    except Exception:
        lay = None
    try:
        lay.Linetype = LINETYPE_CENTER      # 点画线
    except Exception:
        pass
    try:
        lay.Lineweight = LW_THIN
    except Exception:
        pass

    seen = set()
    made = 0
    for lname in view_layers:
        for i in range(ms.Count):
            try:
                e = ms.Item(i)
                if str(e.Layer) != lname:
                    continue
                onm = str(e.ObjectName)
                if onm not in ('AcDbCircle', 'AcDbArc'):
                    continue
                c = e.Center
                cx, cy = float(c[0]), float(c[1])
                r = float(e.Radius)
            except Exception:
                continue
            key = (round(cx, 1), round(cy, 1))
            if key in seen:
                continue
            seen.add(key)

            half = r + extend
            for p1, p2 in (((cx - half, cy, 0), (cx + half, cy, 0)),
                           ((cx, cy - half, 0), (cx, cy + half, 0))):
                try:
                    ln = ms.AddLine(_vec(p1), _vec(p2))
                    ln.Layer = LAYER_CENTER
                    try:
                        ln.Linetype = LINETYPE_CENTER
                    except Exception:
                        pass
                    try:
                        ln.Lineweight = LW_THIN
                    except Exception:
                        pass
                    made += 1
                except Exception:
                    pass
    return {'ok': made > 0, 'lines': made, 'centers': len(seen)}


def normalize_lineweights(doc, view_layers):
    """把视图里的线宽按规定归一到 GB 的粗细搭配。

    GB/T 4457.4：粗实线 b、细实线 b/2，两者比例 **2:1**。
    SolidWorks 导出的轮廓线是 0.70mm，而我们细线用 0.25mm，
    比例 2.8:1 —— 打印出来粗细对比过头，图纸显得发毛。
    这里统一成 b=0.50 / 细=0.25。

    规则：
      · 视图图层上的 Continuous（可见轮廓）→ 粗实线 0.50
      · HIDDEN（不可见轮廓）/ CENTER（中心线）→ 细线 0.25
      · 已经是 0.25 的不动
    """
    ms = doc.ModelSpace
    changed = 0
    for i in range(ms.Count):
        try:
            e = ms.Item(i)
            if str(e.Layer) not in view_layers:
                continue
            onm = str(e.ObjectName)
            if onm not in ('AcDbLine', 'AcDbCircle', 'AcDbArc', 'AcDbPolyline',
                           'AcDbEllipse', 'AcDbSpline'):
                continue
            lt = str(e.Linetype)
            try:
                cur = int(e.Lineweight)
            except Exception:
                cur = -1
            if 'HIDDEN' in lt.upper() or 'CENTER' in lt.upper() or 'DASH' in lt.upper():
                want = LW_THIN
            else:
                want = LW_THICK
            if cur != want:
                try:
                    e.Lineweight = want
                    changed += 1
                except Exception:
                    pass
        except Exception:
            continue
    return {'ok': True, 'changed': changed,
            'thick': LW_THICK, 'thin': LW_THIN}


def resolve_dimension_overlaps(doc, text_h=3.5, rounds=3, gap_mm=1.0):
    """检查标注文字是否互相遮挡；遮挡就挪位置。**多轮**直到稳定。

    用户要求：「19 后面应该加一个检查是否有遮挡，如果遮挡要调整标注位置，
    还有调整标注的颜色线宽，而且还有多次检查」。

    做法（每轮）：
      1. 收集所有 Dimension，取文字框（TextPosition + 字高估算）
      2. 两两比较文字框，重叠且中心距 < 阈值 → 判定遮挡
      3. 把「后出现」的那个沿**远离对方**的方向平移一段（逐次加长）
      4. 再检查是否跑出视图/压到别的标注
    多轮的原因：挪一次可能撞上第三个，所以要迭代到没有新冲突为止。

    返回 {'rounds': N, 'moved': M, 'remaining': K}
    """
    ms = doc.ModelSpace
    moved_total = 0
    remaining = 0
    used_rounds = 0
    tw = text_h * 0.62      # 每个字符宽度的粗略估计
    pad = max(0.6, gap_mm)

    for r in range(int(rounds)):
        used_rounds = r + 1
        # 收集标注的文字框
        items = []
        for i in range(ms.Count):
            try:
                e = ms.Item(i)
                onm = str(e.ObjectName)
                if 'Dimension' not in onm:
                    continue
                tp = e.TextPosition
                cx, cy = float(tp[0]), float(tp[1])
                try:
                    val = float(e.Measurement)
                except Exception:
                    val = 0.0
                txt = f'{val:.2f}'
                w = max(text_h * 1.4, len(txt) * tw)
                h = text_h * 1.35
                items.append({'e': e, 'cx': cx, 'cy': cy,
                              'box': (cx - w / 2, cy - h / 2,
                                      cx + w / 2, cy + h / 2),
                              'kind': onm})
            except Exception:
                continue

        moved_this = 0
        conflicts = 0
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if not _rects_overlap(a['box'], b['box'], pad):
                    continue
                conflicts += 1
                # 挪「后出现的」那个：沿远离对方中心的方向
                dx = b['cx'] - a['cx']
                dy = b['cy'] - a['cy']
                if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                    dx, dy = 0.0, -1.0
                ln = math.hypot(dx, dy) or 1.0
                step = (text_h * 1.6) * (r + 1)
                nx = b['cx'] + dx / ln * step
                ny = b['cy'] + dy / ln * step
                try:
                    b['e'].TextPosition = _vec((nx, ny, 0))
                    b['cx'], b['cy'] = nx, ny
                    h = (b['box'][3] - b['box'][1]) / 2
                    w = (b['box'][2] - b['box'][0]) / 2
                    b['box'] = (nx - w, ny - h, nx + w, ny + h)
                    moved_this += 1
                    moved_total += 1
                except Exception:
                    pass
        remaining = conflicts
        if conflicts == 0:
            break

    return {'rounds': used_rounds, 'moved': moved_total,
            'remainingConflicts': remaining}


def normalize_dimension_style(doc, text_h=3.5):
    """统一所有标注的颜色、线宽、字高、小数位。

    用户要求「调整标注的颜色线宽」。ByBlock 在单色打印下会变白，
    所以统一改成显式颜色 7；线宽统一细实线。
    """
    ms = doc.ModelSpace
    n = 0
    for i in range(ms.Count):
        try:
            e = ms.Item(i)
            if 'Dimension' not in str(e.ObjectName):
                continue
            try:
                if abs(float(e.TextHeight) - text_h) > 1e-6:
                    e.TextHeight = text_h
            except Exception:
                pass
            try:
                e.color = 7
            except Exception:
                pass
            try:
                e.Lineweight = LW_THIN
            except Exception:
                pass
            # 小数位：整数→0位，否则2位
            try:
                val = float(e.Measurement)
                e.PrimaryUnitsPrecision = 0 if abs(val - round(val)) < 0.005 else 2
            except Exception:
                pass
            n += 1
        except Exception:
            continue
    return {'ok': True, 'count': n, 'height': text_h}


def _entity_bbox(e, name):
    """求任意图元的包围盒 (x0, y0, x1, y1)。

    重要教训（用户指出「有的视图移动时没有都移动进去」）：
    原先只处理 Line/Circle/Arc，结果 DWG 里的
    Polyline(9个) / Ellipse(5个) / Spline(1个) 全部留在图层 0 上，
    按图层整块移动时自然漏掉它们，视图就搬不全。

    正确做法：用 Entity.GetBoundingBox() —— 它对任何图元都有效。
    注意它的返回值才是数据：
        r = e.GetBoundingBox(mn, mx)   # r = (minPoint, maxPoint)
    两个 VARIANT 参数传进去是空的，要从返回值取。
    """
    # 1) 通用方法：GetBoundingBox（覆盖 Polyline/Ellipse/Spline/Block/MText…）
    try:
        from win32com.client import VARIANT as _V
        import pythoncom as _pc
        mn = _V(_pc.VT_ARRAY | _pc.VT_R8, (0.0, 0.0, 0.0))
        mx = _V(_pc.VT_ARRAY | _pc.VT_R8, (0.0, 0.0, 0.0))
        r = e.GetBoundingBox(mn, mx)
        if r and len(r) >= 2:
            p0, p1 = r[0], r[1]
            return (min(p0[0], p1[0]), min(p0[1], p1[1]),
                    max(p0[0], p1[0]), max(p0[1], p1[1]))
    except Exception:
        pass

    # 2) 退回手工算（GetBoundingBox 不可用时）
    try:
        if name == 'AcDbLine':
            s = e.StartPoint
            t = e.EndPoint
            return (min(s[0], t[0]), min(s[1], t[1]),
                    max(s[0], t[0]), max(s[1], t[1]))
        if name in ('AcDbCircle', 'AcDbArc'):
            c = e.Center
            r = e.Radius
            return (c[0] - r, c[1] - r, c[0] + r, c[1] + r)
        ip = e.InsertionPoint
        return (ip[0], ip[1], ip[0], ip[1])
    except Exception:
        return None

def classify_into_layers(doc, view_boxes, paper_w, paper_h, tol=15.0,
                         bom_box=None):
    """把模型空间里的图元分到 FRAME / TITLE / BOM / VIEW_n 图层。

    view_boxes: SolidWorks 报告的视图边界（毫米），用于判断图元属于哪个视图。
    bom_box   : 明细表的外包围框 (x0,y0,x1,y1)，单位毫米。
                用户思路：「BOM 也是包围框的思路，给外边缘做包围框，
                然后 BOM 独立图层，避免和工程图图框以及视图重叠」。

    分配优先级：
      0) 落在明细表包围框内    → DSH_BOM（最高优先级，独立图层）
      1) 右下角标题栏区域      → DSH_TITLE
      2) 落在某个视图框内      → DSH_VIEW_n（多个框重叠时取面积最小的）
      3) 贴图幅边缘 / 跨整幅   → DSH_FRAME
      4) 与「视图框外扩 tol」相交 → DSH_VIEW_n
      5) 其余                  → 不动（保留原图层）

    返回 {layerName: entityCount}
    """
    ms = doc.ModelSpace
    n_views = max(1, len(view_boxes or []))
    view_layers = ensure_view_layers(doc, n_views)
    counts = {LAYER_FRAME: 0, LAYER_TITLE: 0, LAYER_BOM: 0}
    for nm in view_layers:
        counts[nm] = 0

    boxes = sorted(view_boxes or [], key=lambda b: (b['x1'] - b['x0']) * (b['y1'] - b['y0']))
    bom = None
    if bom_box and len(bom_box) >= 4:
        bom = (float(bom_box[0]), float(bom_box[1]),
               float(bom_box[2]), float(bom_box[3]))

    # ⚠ 必须先把这个图层建出来！
    # 如果 DSH_BOM 不存在，`e.Layer = 'DSH_BOM'` 会抛异常，
    # 而异常又被下面 try/except 吞掉 → 计数永远是 0、图元永远留在 0 层。
    # 这个坑很隐蔽：代码看着完全正确，就是不动。
    for _lay, _color in ((LAYER_FRAME, 7), (LAYER_TITLE, 7), (LAYER_BOM, 3)):
        try:
            ensure_layer(doc, _lay, color=_color)
        except Exception:
            pass

    for i in range(ms.Count):
        try:
            e = ms.Item(i)
            name = str(e.ObjectName)
            cur_layer = str(e.Layer)
        except Exception:
            continue

        # 已经是我们的图层就不再处理
        if cur_layer.startswith('DSH_'):
            continue

        bb = _entity_bbox(e, name)
        if bb is None:
            # 文字等：按位置归类
            try:
                ip = e.InsertionPoint
                cx, cy = ip[0], ip[1]
                bb = (cx, cy, cx, cy)
            except Exception:
                continue
        x0, y0, x1, y1 = bb
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

        target = None

        # 0) 明细表包围框内 → 独立图层（最高优先级）
        #    必须先判，否则表格线会被当成图框或视图的一部分搬走。
        if bom is not None:
            if (cx >= bom[0] and cx <= bom[2]
                    and cy >= bom[1] and cy <= bom[3]):
                target = LAYER_BOM

        # 1) 标题栏区域（右下角）
        # 1) 标题栏区域（右下角）
        # ⚠ 必须加 target is None：否则会把已经判成 DSH_BOM 的
        # 明细表图元再改回 DSH_TITLE（明细表也在右下角，两者区域重叠）。
        if target is None and paper_w and paper_h:
            if cx > paper_w * 0.55 and cy < paper_h * 0.25:
                target = LAYER_TITLE

        # 2) 视图框内优先判断（必须优先于图框判断！）
        # ⚠ 顺序很关键：视图会被故意摊到图框外面（用户要求），
        # 那些图元坐标都超出纸面范围，若先做「贴边/跨幅」判断，
        # 会把视图几何误判成图框线，视图图层直接变空。
        if target is None and boxes:
            for idx, b in enumerate(boxes):
                if b['x0'] <= cx <= b['x1'] and b['y0'] <= cy <= b['y1']:
                    target = VIEW_LAYER_FMT.format(len(boxes) - idx)
                    break

        # 3) 图框：必须落在纸面内，且贴边或跨整幅
        if target is None and paper_w and paper_h:
            inside_paper = (0 <= cx <= paper_w and 0 <= cy <= paper_h)
            if inside_paper:
                span_x = x1 - x0
                span_y = y1 - y0
                near_edge = (min(x0, x1) < tol or min(y0, y1) < tol
                             or max(x0, x1) > paper_w - tol
                             or max(y0, y1) > paper_h - tol)
                spans = span_x > paper_w * 0.85 or span_y > paper_h * 0.85
                if near_edge or spans:
                    target = LAYER_FRAME

        # 4) 就近兜底：只把**贴着视图框**的图元归给视图。
        #
        # ⚠ 这里以前是「无条件归给最近视图」，导致图框内部的分区线、
        # 标题栏线条全被塞进视图图层，把视图包围框撑成
        # 「高 340mm」这种尺寸（A3 只有 297），视图再也摆不进图框。
        # 现在要求图元必须和「视图框外扩 tol」相交才算该视图的。
        if target is None and boxes:
            for idx, b in enumerate(boxes):
                exp = (b['x0'] - tol, b['y0'] - tol,
                       b['x1'] + tol, b['y1'] + tol)
                if _rects_overlap((x0, y0, x1, y1), exp, 0.0):
                    target = VIEW_LAYER_FMT.format(len(boxes) - idx)
                    break

        if target:
            try:
                e.Layer = target
                counts[target] = counts.get(target, 0) + 1
            except Exception:
                pass

    return counts


def _rects_overlap(a, b, pad=0.0):
    """两个 (x0,y0,x1,y1) 矩形是否重叠（pad 为额外安全间距）。"""
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0]
                or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _place_avoiding(rect, target, reserved, placed, bounds, pad=6.0,
                    step=8.0, max_try=40):
    """把视图摆到 target，但避开 reserved（明细表/标题栏）和已放置的视图。

    用户思路：「装配图也用零件图的思路——做出包围框再移动」。
    既然每个视图都有包围框，那就让包围框**互相不重叠、也不压明细表**：
      1. 从 target 开始，先尝试原地
      2. 冲突就往上推（+y），推到顶就退回底部再往左挪（-x）
      3. 都不行就接受最后一次位置（总比跑出图框强）

    rect     : 本视图包围框 (x0,y0,x1,y1)
    target   : 期望中心 (tx, ty)
    reserved : [rect, ...] 不能进入的区域
    placed   : [rect, ...] 已摆放的视图
    bounds   : (x_min, y_min, x_max, y_max) 可画区
    返回 (tx, ty, {'shifted': 次数, 'reason': ...})
    """
    cx = (rect[0] + rect[2]) / 2.0
    cy = (rect[1] + rect[3]) / 2.0
    w = max(rect[2] - rect[0], 1.0)
    h = max(rect[3] - rect[1], 1.0)
    bx0, by0, bx1, by1 = bounds

    def rect_at(tx, ty):
        return (tx - w / 2.0, ty - h / 2.0, tx + w / 2.0, ty + h / 2.0)

    def inside(r):
        return (r[0] >= bx0 - 0.5 and r[1] >= by0 - 0.5
                and r[2] <= bx1 + 0.5 and r[3] <= by1 + 0.5)

    def ok(r):
        if not inside(r):
            return False
        for rr in reserved:
            if _rects_overlap(r, rr, pad):
                return False
        for rr in placed:
            if _rects_overlap(r, rr, pad):
                return False
        return True

    tx, ty = float(target[0]), float(target[1])
    if ok(rect_at(tx, ty)):
        return tx, ty, {'shifted': 0, 'reason': 'original'}

    # 沿 y 往上顶
    tries = 0
    for _ in range(max_try):
        tries += 1
        ty += step
        r = rect_at(tx, ty)
        if r[3] > by1:
            break
        if ok(r):
            return tx, ty, {'shifted': tries, 'reason': 'up'}

    # 沿 x 往左挪
    ty0 = float(target[1])
    for k in range(1, max_try + 1):
        tries += 1
        tx = float(target[0]) - k * step
        r = rect_at(tx, ty0)
        if r[0] < bx0:
            break
        if ok(r):
            return tx, ty0, {'shifted': tries, 'reason': 'left'}

    # 网格搜索：把整个可画区按 step 铺一遍，从「离目标最近」的格子开始试。
    # 明细表很大时（13 行 ≈ 91mm 高），单纯上推左挪会找不到位置
    # （实测 shifted=70 仍然 clamped），必须全图找空位。
    cands = []
    yy = by1 - h / 2.0
    while yy >= by0 + h / 2.0 - 0.1:
        xx = bx0 + w / 2.0
        while xx <= bx1 - w / 2.0 + 0.1:
            # 距离目标越近越优先
            d = ((xx - target[0]) ** 2 + (yy - target[1]) ** 2) ** 0.5
            cands.append((d, xx, yy))
            xx += step
        yy -= step
    cands.sort(key=lambda c: c[0])
    for _d, xx, yy in cands:
        tries += 1
        if ok(rect_at(xx, yy)):
            return xx, yy, {'shifted': tries, 'reason': 'grid'}

    # 实在摆不下：至少别跑到图框外
    tx = min(max(float(target[0]), bx0 + w / 2.0), bx1 - w / 2.0)
    ty = min(max(float(target[1]), by0 + h / 2.0), by1 - h / 2.0)
    return tx, ty, {'shifted': tries, 'reason': 'clamped'}


def move_views_by_layer(doc, view_layers, paper='A3', landscape=True,
                        title_h=55.0, order=None, reserved=None,
                        gap_mm=6.0):
    """按图层把每个视图整块移到国标固定位置。

    用户要求的做法：「按图层来移动」。
    按图层选中的所有图元一起 Move，整块平移，绝不会撕裂。

    reserved：不能占用的矩形列表 [(x0,y0,x1,y1)]，单位 mm。
              装配图上要传「明细表 + 标题栏」所占的区域，
              否则视图会被摆到明细表上面去（实测真实装配图就是这样）。

    返回 {layerName: (x, y)}
    """
    targets = paper_targets(paper, landscape, title_h)
    order = order or VIEW_TARGET_ORDER
    ms = doc.ModelSpace
    reserved = list(reserved or [])

    # 算每个图层的当前包围盒和中心
    info = []
    for nm in view_layers:
        xs, ys = [], []
        for i in range(ms.Count):
            try:
                e = ms.Item(i)
                if str(e.Layer) != nm:
                    continue
            except Exception:
                continue
            bb = _entity_bbox(e, str(e.ObjectName))
            if bb:
                xs += [bb[0], bb[2]]
                ys += [bb[1], bb[3]]
        if xs:
            info.append({'layer': nm,
                         'cx': (min(xs) + max(xs)) / 2.0,
                         'cy': (min(ys) + max(ys)) / 2.0,
                         'x0': min(xs), 'y0': min(ys),
                         'x1': max(xs), 'y1': max(ys)})

    if not info:
        return {}

    # 按相对象限决定哪个图层去哪个位置
    xs_all = [t['cx'] for t in info]
    ys_all = [t['cy'] for t in info]
    mx = (min(xs_all) + max(xs_all)) / 2.0
    my = (min(ys_all) + max(ys_all)) / 2.0

    placed = {}
    used = set()
    assignments = []
    for t in info:
        left = t['cx'] <= mx
        top = t['cy'] >= my
        if left and top:
            key = 'front'
        elif (not left) and top:
            key = 'right'
        elif left and (not top):
            key = 'top'
        else:
            key = 'isometric'
        assignments.append((t, key))

    placed = {}
    placed_rects = []

    pw_mm, ph_mm = paper_size_mm(paper, landscape)
    # 可画区：左边装订 25、其余 5；上边留 5
    bounds = (25.0, 5.0, pw_mm - 5.0, ph_mm - 5.0)

    for t, key in assignments:
        if key in used:
            for alt in ('isometric', 'right', 'top', 'front'):
                if alt not in used:
                    key = alt
                    break
        used.add(key)
        tx, ty = targets[key]

        # 尺寸感知的边界约束（原来就有的兜底）
        w_v = (t['x1'] - t['x0'])
        h_v = (t['y1'] - t['y0'])
        min_cy = float(title_h) + h_v / 2.0 + 8.0
        if ty < min_cy:
            ty = min_cy

        # ★ 关键修复：按包围框避让明细表和已放视图
        # 之前视图直接被摆到明细表上（真实装配图实测压住 BOM）。
        rect = (t['x0'], t['y0'], t['x1'], t['y1'])
        tx, ty, how = _place_avoiding(rect, (tx, ty), reserved, placed_rects,
                                      bounds, pad=gap_mm)

        dx, dy = tx - t['cx'], ty - t['cy']
        moved = 0
        for i in range(ms.Count):
            try:
                e = ms.Item(i)
                if str(e.Layer) != t['layer']:
                    continue
                e.Move(_vec((0, 0, 0)), _vec((dx, dy, 0)))
                moved += 1
            except Exception:
                pass
        # 记录最终落位后的包围框，供后续视图避让
        placed_rects.append((t['x0'] + dx, t['y0'] + dy,
                             t['x1'] + dx, t['y1'] + dy))
        placed[t['layer']] = {'target': (round(tx, 1), round(ty, 1)),
                              'size': (round(w_v, 1), round(h_v, 1)),
                              'moved': moved, 'slot': key, **how}

    return placed

# ───────────────── 图层管理（避免辅助线干扰正式图） ─────────────────
LAYER_GUIDE = 'DSH_GUIDE'      # 排布辅助框（临时，画完删/关层）
LAYER_DIM = 'DSH_DIM'          # 国标尺寸标注
LAYER_NOTE = 'DSH_NOTE'        # 文字注释


def ensure_layer(doc, name, color=1, plottable=True, locked=False,
                 description=''):
    """确保图层存在并设置属性。

    辅助框单独放 DSH_GUIDE 层（红色、不可打印、锁定），
    这样它绝不会干扰正式图，也不怕忘了删。
    """
    try:
        try:
            lay = doc.Layers.Item(name)
        except Exception:
            lay = doc.Layers.Add(name)
        try: lay.color = color
        except Exception: pass
        try: lay.Plottable = plottable
        except Exception: pass
        try: lay.Lock = locked
        except Exception: pass
        if description:
            try: lay.Description = description
            except Exception: pass
        return lay
    except Exception:
        return None


def setup_layers(doc):
    """建好三层：几何层保持不变，标注和辅助各自独立。"""
    ensure_layer(doc, LAYER_DIM, color=256, plottable=True, locked=False,
                 description='GB 尺寸标注')
    ensure_layer(doc, LAYER_NOTE, color=256, plottable=True, locked=False,
                 description='文字注释')
    # 辅助框：红色 + 不打印 + 锁定，双保险防干扰
    ensure_layer(doc, LAYER_GUIDE, color=1, plottable=False, locked=True,
                 description='DSH 排布辅助框（临时，可不打印）')


def draw_guide_rects(doc, rects, layer=LAYER_GUIDE, delete_after=True):
    """把若干矩形画到辅助图层上，用于目视检查排布是否重叠。

    rects: [(x0, y0, x1, y1), ...]
    delete_after=True 时画完立即删（只作为排布计算的可视化验证）。
    返回创建的矩形数。
    """
    ms = doc.ModelSpace
    objs = []
    for (x0, y0, x1, y1) in rects:
        try:
            # 用轻量多段线画闭合框
            pts = [x0, y0, x1, y0, x1, y1, x0, y1]
            from win32com.client import VARIANT
            import pythoncom
            arr = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(float(v) for v in pts))
            pl = ms.AddLightWeightPolyline(arr)
            pl.Closed = True
            try: pl.Layer = layer
            except Exception: pass
            objs.append(pl)
        except Exception:
            pass
    if delete_after:
        for o in objs:
            try: o.Delete()
            except Exception: pass
    return len(objs)

# ───────────────── 尺寸沿边「区间装箱」排布 ─────────────────
# ───────────────── 尺寸的三类包围盒 ─────────────────
# 用户指出的关键点：一个尺寸实体其实有 3 个包围盒，
# 而且是三种完全不同的「碰撞特性」：
#
#   尺寸线(DIMLINE)    沿标注方向的细线段  → 碰，但只有一个线宽那么厚
#   尺寸界线(EXTLINE)  轮廓到尺寸线的垂直细线 → 基本不碰（细线交叉在图上很常见）
#   文字(TEXT)         文字块              → 碰，而且最需要留空隙
#
# 如果拿「含尺寸界线的整体 bbox」去做排布，bbox 会被尺寸界线撑长，
# 间距就被迫拉大——这正是「分得太开」的根因。
#
# 所以排布只认「尺寸线+文字」的紧凑条带，尺寸界线不参与碰撞判断。

def dim_footprint(text_h, with_text=True):
    """一条尺寸在「垂直于标注方向」上的实际占位厚度（mm）。

    尺寸线本身厚度≈线宽（可忽略），文字高 text_h；
    文字通常压在尺寸线上方（国标 DIMTAD=1），
    所以整条带子的厚度 ≈ 文字高 + 上下各留一点空隙。
    """
    th = float(text_h)
    if with_text:
        return th * 1.45          # 文字 + 尺寸线 + 少量余量
    return th * 0.25              # 只有尺寸线（无文字时）


def text_box_width(text_h, n_chars=4):
    """估算文字块宽度（mm），用于「文字之间不打架」的判断。

    AutoCAD 的 ISOCPEUR/txt 字宽约 0.6×字高，数字串按 4 位估。
    """
    return float(text_h) * 0.6 * max(1, int(n_chars))


def pack_rows(spans, gap=3.0):
    """一维区间装箱：把若干区间分到最少的「排」，同排区间不重叠。

    这就是用户说的「沿边做包围框，调到不重合但紧凑，再把框删掉」：
      spans: [(lo, hi), ...]  每条尺寸在边方向上的跨度
    返回 rows: [[span_index, ...], ...]，同一 row 内区间互不重叠，
    排号即尺寸线离视图边的层数。

    贪心算法：按跨度长度降序，放进第一个放得下的排。
    """
    order = sorted(range(len(spans)), key=lambda i: -(spans[i][1] - spans[i][0]))
    rows = []      # 每排已占用的区间列表
    assign = {}
    for i in order:
        lo, hi = spans[i]
        placed = False
        for r, occupied in enumerate(rows):
            if all(hi + gap <= a or lo >= b + gap for (a, b) in occupied):
                occupied.append((min(lo, hi), max(lo, hi)))
                assign[i] = r
                placed = True
                break
        if not placed:
            rows.append([(min(lo, hi), max(lo, hi))])
            assign[i] = len(rows) - 1
    return assign, len(rows)


# 图框与标题栏所在的图层（SolidWorks 导出 DWG 的固定规律，实测）
FRAME_LAYERS = {'5', '10', 'SLD-0', 'FORMAT', 'FRAME', 'DEFPOINTS'}
# 标题栏区域（右下角），用图幅比例表示：x > 55%, y < 25%
TITLE_ZONE = (0.55, 0.25)


def _in_title_zone(x, y, paper_w, paper_h):
    """判断点是否落在右下角标题栏区域。"""
    if not paper_w or not paper_h:
        return False
    return (x > paper_w * TITLE_ZONE[0]) and (y < paper_h * TITLE_ZONE[1])


def collect_entities(doc, skip_layers=None, paper=None):
    """收集模型空间里的几何，供自动标注分析。

    skip_layers: 要跳过的图层名集合（图框/标题栏）。
                 SolidWorks 导出 DWG 时，图框在图层 5、标题栏在 10，
                 零件几何在图层 0 —— 实测出来的固定规律。
    paper:       (w, h) 图幅尺寸，给了就额外排除右下角标题栏区域。

    返回 dict:
      lines   [(x1,y1,x2,y2,obj)]
      circles [(cx,cy,r,obj)]
      arcs    [(cx,cy,r,a1,a2,obj)]
      points  [(x,y,obj)]
    """
    out = {'lines': [], 'circles': [], 'arcs': [], 'points': []}
    skip = set(skip_layers if skip_layers is not None else FRAME_LAYERS)
    pw, ph = (paper if paper else (None, None))
    ms = doc.ModelSpace
    try:
        n = ms.Count
    except Exception:
        return out
    for i in range(n):
        try:
            e = ms.Item(i)
            name = str(e.ObjectName)
            layer = str(e.Layer)
        except Exception:
            continue
        # 跳过图框/标题栏图层
        if layer in skip:
            continue
        try:
            if name == 'AcDbLine':
                # 几何方式排除图框。
                # 实测规律：SolidWorks 导出的 A3 图框是内缩 10mm 的大矩形
                #   (10,10)-(10,287) / (10,287)-(410,287) / (410,...)
                # 即边长 ≈ 图幅宽-20 或 图幅高-20，且是水平/垂直长线。
                if pw and ph:
                    try:
                        s0 = e.StartPoint
                        t0 = e.EndPoint
                        ln_len = math.hypot(t0[0] - s0[0], t0[1] - s0[1])
                        is_horiz = abs(s0[1] - t0[1]) < 1.0
                        is_vert = abs(s0[0] - t0[0]) < 1.0
                        if (is_horiz and ln_len > pw * 0.85) or \
                           (is_vert and ln_len > ph * 0.85):
                            continue
                        # 图框上的分区标记短线：一端贴着图幅边缘（0 / 图幅）
                        # 或贴着图框线（内缩 10mm 处），且长度很短（<25mm）
                        EDGE_TOL = 12.0
                        def _on_edge(v, limit):
                            return (abs(v) < EDGE_TOL
                                    or abs(v - limit) < EDGE_TOL
                                    or abs(v - 10.0) < 2.0
                                    or abs(v - (limit - 10.0)) < 2.0)
                        if ln_len < 30.0:
                            if _on_edge(s0[0], pw) or _on_edge(t0[0], pw) or \
                               _on_edge(s0[1], ph) or _on_edge(t0[1], ph):
                                continue
                    except Exception:
                        pass
                s = e.StartPoint
                t = e.EndPoint
                out['lines'].append((float(s[0]), float(s[1]),
                                     float(t[0]), float(t[1]), e))
            elif name == 'AcDbCircle':
                c = e.Center
                out['circles'].append((float(c[0]), float(c[1]),
                                       float(e.Radius), e))
            elif name == 'AcDbArc':
                c = e.Center
                out['arcs'].append((float(c[0]), float(c[1]),
                                    float(e.Radius),
                                    float(e.StartAngle), float(e.EndAngle), e))
            elif name == 'AcDbPoint':
                p = e.Coordinates
                out['points'].append((float(p[0]), float(p[1]), e))
        except Exception:
            continue
    return out


def _is_frame_line(x1, y1, x2, y2, paper_w, paper_h, tol=8.0):
    """判断一根线是不是图框/标题栏边框，不是零件轮廓。

    ⚠ 踩过的坑：SolidWorks 导出的 DWG 里带着图框和标题栏，
    直接取全部几何的包围盒会得到 (0,0,420,297) —— 也就是 A3 图幅本身，
    于是「总体尺寸」被标成 420×297，完全错了。
    必须先把贴着图幅边界的线排除。
    """
    # 图框线可能贴 0，也可能内缩 5mm（GB 图框边距），两种情况都算
    def near(v, target):
        return abs(v - target) < tol or abs(v - (target + 5.0)) < tol

    near_left = near(x1, 0.0) and near(x2, 0.0)
    near_right = near(x1, paper_w) and near(x2, paper_w)
    near_bottom = near(y1, 0.0) and near(y2, 0.0)
    near_top = near(y1, paper_h) and near(y2, paper_h)
    return near_left or near_right or near_bottom or near_top


def _bbox(geo, exclude_frame=None):
    """几何包围盒 (xmin, ymin, xmax, ymax)。

    exclude_frame: (paper_w, paper_h)，给了就排除贴边的图框线。
    """
    xs, ys = [], []
    pw, ph = exclude_frame if exclude_frame else (None, None)
    for x1, y1, x2, y2, _ in geo['lines']:
        if exclude_frame and _is_frame_line(x1, y1, x2, y2, pw, ph):
            continue
        xs += [x1, x2]
        ys += [y1, y2]
    for cx, cy, r, _ in geo['circles']:
        xs += [cx - r, cx + r]
        ys += [cy - r, cy + r]
    for cx, cy, r, a1, a2, _ in geo['arcs']:
        xs += [cx - r, cx + r]
        ys += [cy - r, cy + r]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def cluster_views(geo, gap=18.0):
    """把几何按二维邻近性聚成「视图」（连通分量）。

    ⚠ 教训：不能只按 x 排序做线性合并。
    工程图里主视图和俯视图是**上下叠放**的（长对正），
    只按 x 排会把它们当成一簇、或者把同一视图拆成碎片
    （实测按 x 排会把 4 个视图切成 15 个碎片）。

    正确做法：并查集做二维连通分量 —— 两个元素只要
    在 x 和 y 两个方向上的间隙都不超过 gap，就连成一簇。

    返回按面积降序的簇列表
    """
    items = []
    for ln in geo['lines']:
        x1, y1, x2, y2 = ln[0], ln[1], ln[2], ln[3]
        items.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), 'line', ln))
    for c in geo['circles']:
        cx, cy, r = c[0], c[1], c[2]
        items.append((cx - r, cy - r, cx + r, cy + r, 'circle', c))
    for a in geo['arcs']:
        cx, cy, r = a[0], a[1], a[2]
        items.append((cx - r, cy - r, cx + r, cy + r, 'arc', a))

    n = len(items)
    if n == 0:
        return []

    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    # 贪心：按 x0 排序后只和后面的比较（剪枝），
    # 但比较条件是二维间隙，保证上下叠放也能连上
    order = sorted(range(n), key=lambda i: items[i][0])
    for oi in range(n):
        i = order[oi]
        ax0, ay0, ax1, ay1 = items[i][:4]
        for oj in range(oi + 1, n):
            j = order[oj]
            bx0, by0, bx1, by1 = items[j][:4]
            # x 方向已经排序，若 b 的左边界超出 a 右边界 + gap 就可以提前停
            if bx0 > ax1 + gap:
                break
            dx = max(0.0, max(ax0 - bx1, bx0 - ax1))
            dy = max(0.0, max(ay0 - by1, by0 - ay1))
            if dx <= gap and dy <= gap:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out = []
    for members in groups.values():
        c = {'lines': [], 'circles': [], 'arcs': []}
        xs0 = []; ys0 = []; xs1 = []; ys1 = []
        for i in members:
            x0, y0, x1, y1, kind, data = items[i]
            xs0.append(x0); ys0.append(y0); xs1.append(x1); ys1.append(y1)
            if kind == 'line':
                c['lines'].append(data)
            elif kind == 'circle':
                c['circles'].append(data)
            else:
                c['arcs'].append(data)
        bb = (min(xs0), min(ys0), max(xs1), max(ys1))
        c['bbox'] = bb
        c['area'] = max(bb[2] - bb[0], 1.0) * max(bb[3] - bb[1], 1.0)
        out.append(c)

    out.sort(key=lambda c: -c['area'])
    return out


def group_by_view_boxes(doc, boxes, paper='A3', landscape=True):
    """按 SolidWorks 报告的视图边界，把图元分到各个视图。

    这是用户提的方案的落地关键：
      「给视图做上矩形的包围框，转到 AutoCAD 后还是要把视图合到一起对齐」

    有了显式矩形，就不用再靠几何聚类去猜——判断规则很干脆：
    图元的包围盒落在哪个矩形里，就属于哪个视图。

    返回 [(box_dict, [entity, ...]), ...]
    """
    ms = doc.ModelSpace
    groups = [{'box': b, 'entities': []} for b in boxes]

    for i in range(ms.Count):
        try:
            e = ms.Item(i)
            name = str(e.ObjectName)
            layer = str(e.Layer)
        except Exception:
            continue
        # 图框/标题栏/辅助层不参与
        if layer in FRAME_LAYERS or layer in (LAYER_GUIDE, LAYER_DIM, LAYER_NOTE):
            continue

        # 求该图元的包围盒
        try:
            if name == 'AcDbLine':
                s = e.StartPoint; t = e.EndPoint
                x0, y0 = min(s[0], t[0]), min(s[1], t[1])
                x1, y1 = max(s[0], t[0]), max(s[1], t[1])
            elif name == 'AcDbCircle':
                c = e.Center; r = e.Radius
                x0, y0, x1, y1 = c[0] - r, c[1] - r, c[0] + r, c[1] + r
            elif name == 'AcDbArc':
                c = e.Center; r = e.Radius
                x0, y0, x1, y1 = c[0] - r, c[1] - r, c[0] + r, c[1] + r
            else:
                continue
        except Exception:
            continue

        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

        # 归属判断：SolidWorks 的 GetOutline 是「视觉外框」，
        # 轴测图和相邻视图的框常常互相重叠（实测 视图3 与 视图4 的框
        # 在 x 方向重叠 50mm），单靠「落在框内」会把图元分错视图。
        # 规则：在所有**包含**该点的框里取面积最小的（小框更具体）；
        #       都不包含时取中心最近的。
        best = None
        best_area = None
        for g in groups:
            b = g['box']
            if b['x0'] <= cx <= b['x1'] and b['y0'] <= cy <= b['y1']:
                area = (b['x1'] - b['x0']) * (b['y1'] - b['y0'])
                if best_area is None or area < best_area:
                    best_area = area
                    best = g
        if best is None:
            dmin = None
            for g in groups:
                b = g['box']
                bcx = (b['x0'] + b['x1']) / 2.0
                bcy = (b['y0'] + b['y1']) / 2.0
                d = (cx - bcx) ** 2 + (cy - bcy) ** 2
                if dmin is None or d < dmin:
                    dmin = d
                    best = g
        if best is not None:
            best['entities'].append(e)

    return [(g['box'], g['entities']) for g in groups if g['entities']]


def move_views_to_layout(doc, boxed_views, paper='A3', landscape=True,
                         title_h=55.0, draw_boxes=True, delete_boxes=True):
    """按视图边界把每个视图整块移到国标固定位置。

    用户要求：「移动视图也要把视图线段全选再移动」——
    这里正是靠矩形边界把整个视图的线段一次性选齐，再一起平移，
    所以视图绝不会被撕开。

    流程：画框(辅助层) → 按框分组 → 整组平移 → 删框

    返回 {'placed': {name: (x,y)}, 'movedEntities': n}
    """
    if not boxed_views:
        return {'placed': {}, 'movedEntities': 0}

    # 1) 把框画到辅助图层（红色、不打印、锁定）
    if draw_boxes:
        rects = [(b['x0'], b['y0'], b['x1'], b['y1']) for b, _ in boxed_views]
        draw_guide_rects(doc, rects, LAYER_GUIDE, delete_after=False)

    targets = paper_targets(paper, landscape, title_h)

    # 2) 判断各视图归属：轴测图除外，其余按相对象限分配
    info = []
    for box, ents in boxed_views:
        w = box['x1'] - box['x0']
        h = box['y1'] - box['y0']
        cx = (box['x0'] + box['x1']) / 2.0
        cy = (box['y0'] + box['y1']) / 2.0
        nm = str(box.get('name') or '')
        is_iso = ('等轴测' in nm or '轴测' in nm or 'isometric' in nm.lower())
        info.append({'box': box, 'ents': ents, 'w': w, 'h': h,
                     'cx': cx, 'cy': cy, 'iso': is_iso})

    std = [t for t in info if not t['iso']]
    placed = {}
    used = set()

    if std:
        xs = [t['cx'] for t in std]
        ys = [t['cy'] for t in std]
        mx = (min(xs) + max(xs)) / 2.0
        my = (min(ys) + max(ys)) / 2.0
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
                for alt in ('top', 'right', 'front', 'isometric'):
                    if alt not in used:
                        key = alt
                        break
            used.add(key)
            tx, ty = targets[key]
            dx, dy = tx - t['cx'], ty - t['cy']
            for e in t['ents']:
                try:
                    e.Move(_vec((0, 0, 0)), _vec((dx, dy, 0)))
                except Exception:
                    pass
            placed[t['box'].get('name', key)] = (round(tx, 1), round(ty, 1))

    for t in info:
        if not t['iso']:
            continue
        tx, ty = targets['isometric']
        dx, dy = tx - t['cx'], ty - t['cy']
        for e in t['ents']:
            try:
                e.Move(_vec((0, 0, 0)), _vec((dx, dy, 0)))
            except Exception:
                pass
        placed[t['box'].get('name', 'isometric')] = (round(tx, 1), round(ty, 1))

    # 3) 删框
    if delete_boxes:
        try:
            ms = doc.ModelSpace
            doomed = []
            for i in range(ms.Count):
                try:
                    o = ms.Item(i)
                    if str(o.Layer) == LAYER_GUIDE:
                        doomed.append(o)
                except Exception:
                    pass
            for o in doomed:
                try: o.Delete()
                except Exception: pass
        except Exception:
            pass

    return {'placed': placed,
            'movedEntities': sum(len(t['ents']) for t in info)}


def merge_collinear_segments(doc, cluster, tol=0.15):
    """把同一视图里共线、相接或重叠的线段合并成一条。

    用户要求：视图移到 AutoCAD 后要「对每个视图合并线段」，移动时
    「把视图线段全选再移动」。

    合并有两个实际好处：
      1) SolidWorks 导出的 DWG 里，一条可见轮廓常被拆成好几段
         （投影、遮挡处断开），导致自动标注时同一长度被反复标
         （实测出现过两个 113.0 重复标注）
      2) 合并后每个视图的线段数大幅减少，整视图框选移动也更可靠

    做法：
      水平线按 y 分组（y 相同视为共线），区间排序后合并重叠/相接的；
      垂直线同理。合并后的线加到原图层，原线删除。

    返回 (合并前线段数, 合并后线段数)
    """
    ms = doc.ModelSpace
    horiz = {}   # y -> [(x0, x1, obj, layer)]
    vert = {}    # x -> [(y0, y1, obj, layer)]
    for ln in cluster.get('lines', []):
        x1, y1, x2, y2, obj = ln[0], ln[1], ln[2], ln[3], ln[4]
        try: layer = str(obj.Layer)
        except Exception: layer = '0'
        if abs(y1 - y2) <= tol:
            key = round((y1 + y2) / 2.0, 2)
            horiz.setdefault(key, []).append((min(x1, x2), max(x1, x2), obj, layer))
        elif abs(x1 - x2) <= tol:
            key = round((x1 + x2) / 2.0, 2)
            vert.setdefault(key, []).append((min(y1, y2), max(y1, y2), obj, layer))

    before = sum(len(v) for v in horiz.values()) + sum(len(v) for v in vert.values())
    after = 0

    def _merge(segments, is_horizontal, key):
        """segments: [(lo, hi, obj, layer)]，按 lo 排序后合并。"""
        segments.sort(key=lambda t: t[0])
        groups = []
        cur = [segments[0]]
        cur_hi = segments[0][1]
        for seg in segments[1:]:
            if seg[0] <= cur_hi + tol:      # 相接或重叠
                cur.append(seg)
                cur_hi = max(cur_hi, seg[1])
            else:
                groups.append((cur, cur_hi))
                cur = [seg]
                cur_hi = seg[1]
        groups.append((cur, cur_hi))

        for grp, hi in groups:
            lo = min(s[0] for s in grp)
            layer = grp[0][3]
            # 只有一段且本来就完整 → 不动
            if len(grp) == 1 and abs(grp[0][0] - lo) < tol and abs(grp[0][1] - hi) < tol:
                continue
            try:
                if is_horizontal:
                    new = ms.AddLine(_vec((lo, key, 0)), _vec((hi, key, 0)))
                else:
                    new = ms.AddLine(_vec((key, lo, 0)), _vec((key, hi, 0)))
                try: new.Layer = layer
                except Exception: pass
                for s in grp:
                    try: s[2].Delete()
                    except Exception: pass
            except Exception:
                pass

    for y, segs in horiz.items():
        if len(segs) >= 1:
            _merge(segs, True, y)
            after += 1
    for x, segs in vert.items():
        if len(segs) >= 1:
            _merge(segs, False, x)
            after += 1

    return before, after


def _dim_one_view(ms, doc, geo, bbox, stats, opts, paper='A3', text_h=3.5):
    """对单个视图做标注 —— 尺寸沿边「区间装箱」分层排布。

    这是用户提的核心思路的落地：
      「尺寸标注沿着边做一个包围框，调整到包围框不重合但紧凑，
        再把包围框删掉」

    实现上就是**一维区间装箱**（strip packing）：
      1) 把要标的尺寸按方向分成两组：
           水平尺寸（量 x 跨度）→ 放在视图上方/下方
           垂直尺寸（量 y 跨度）→ 放在视图左侧/右侧
      2) 每组用 pack_rows() 分「排」：同一排里的尺寸跨度不重叠，
         不同排依次向外偏移 → 既不重叠又紧凑
      3) 排号取反：总长/总高这种最长的尺寸排到最外层（国标习惯）
      4) 可选：把每一排的包围框画到 DSH_GUIDE 层上目视校验，然后删掉

    返回本视图落位信息（供调试）
    """
    xmin, ymin, xmax, ymax = bbox
    w = xmax - xmin
    h = ymax - ymin
    if w < 5.0 and h < 5.0:
        return {}

    base_gap = float(opts.get('offsetMm', 8.0))     # 第一排离轮廓的距离
    # 排距由字高推导（用户指出：用整体包围盒会分得太开）。
    # 真实碰撞的是「尺寸线+文字」条带，厚度≈字高×1.45；留 45% 空隙
    # → 排距≈字高×1.9。A3 的 3.5mm 字 → 6.65mm，且换图幅自动跟随。
    strip = dim_footprint(text_h)                   # 紧凑条带厚度
    row_gap = float(opts.get('rowGapMm') or strip * 1.32)
    tol = float(opts.get('tolMm', 0.5))
    # 线性尺寸上限（每个方向）。装配体只留关键几个：
    # 装配图本来就不该把每条轮廓边都标一遍（那是零件图的事），
    # 实测 3 视图 × 2 方向 × 7 个 = 35 个，挤在一起完全没法看。
    _default_max_linear = 2 if opts.get('assemblyMode') else 6
    max_linear = int(opts.get('maxLinear') or _default_max_linear)
    dim_circles = bool(opts.get('dimCircles', True))
    show_guide = bool(opts.get('guideBoxes', False))

    # ---------- 收集水平/垂直候选 ----------
    horiz = []    # (span_lo, span_hi, p1, p2)
    vert = []
    seen_len = set()
    for x1, y1, x2, y2, obj in geo.get('lines', []):
        length = math.hypot(x2 - x1, y2 - y1)
        if length < tol:
            continue
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        is_h = dy <= tol
        is_v = dx <= tol
        if not (is_h or is_v):
            continue
        # 按长度去重：同一长度的尺寸标一次就够（国标不重复标注）
        key = (is_h, round(length, 1))
        if key in seen_len:
            continue
        seen_len.add(key)
        if is_h:
            horiz.append((min(x1, x2), max(x1, x2), (x1, y1), (x2, y2)))
        else:
            vert.append((min(y1, y2), max(y1, y2), (x1, y1), (x2, y2)))

    # 总体尺寸：视图总长（水平）+ 总高（垂直），一定参与且排最外
    horiz.insert(0, (xmin, xmax, (xmin, ymin), (xmax, ymin)))
    vert.insert(0, (ymin, ymax, (xmin, ymin), (xmin, ymax)))

    # 限量：总长/总高 + 若干分尺寸
    horiz = horiz[:max_linear + 1]
    vert = vert[:max_linear + 1]

    placed = {}

    # ---------- 水平尺寸：放到视图下方 ----------
    if horiz:
        spans = [(s0, s1) for (s0, s1, _, _) in horiz]
        assign, nrows = pack_rows(spans, gap=float(opts.get('packGapMm', 3.0)))
        # 排号取反：最长的总体尺寸到最外层
        for i, (s0, s1, p1, p2) in enumerate(horiz):
            layer = nrows - 1 - assign[i]
            yy = ymin - base_gap - layer * row_gap
            try:
                d = ms.AddDimRotated(_vec((p1[0], p1[1], 0)),
                                     _vec((p2[0], p2[1], 0)),
                                     _vec(((p1[0] + p2[0]) / 2.0, yy, 0)), 0.0)
                try: d.Layer = LAYER_DIM
                except Exception: pass
                stats['overall' if i == 0 else 'linear'] += 1
                placed[f'h{i}'] = (s0, yy, s1, yy)
            except Exception:
                stats['skipped'] += 1

    # ---------- 垂直尺寸：放到视图左侧 ----------
    if vert:
        spans = [(s0, s1) for (s0, s1, _, _) in vert]
        assign, nrows = pack_rows(spans, gap=float(opts.get('packGapMm', 3.0)))
        for i, (s0, s1, p1, p2) in enumerate(vert):
            layer = nrows - 1 - assign[i]
            xx = xmin - base_gap - layer * row_gap
            try:
                d = ms.AddDimRotated(_vec((p1[0], p1[1], 0)),
                                     _vec((p2[0], p2[1], 0)),
                                     _vec((xx, (p1[1] + p2[1]) / 2.0, 0)),
                                     math.pi / 2.0)
                try: d.Layer = LAYER_DIM
                except Exception: pass
                stats['overall' if i == 0 else 'linear'] += 1
                placed[f'v{i}'] = (xx, s0, xx, s1)
            except Exception:
                stats['skipped'] += 1

    # ---------- 定形尺寸：圆→直径，弧→半径 ----------
    # ★ 两次踩过的坑（务必别改回去）：
    #   1) 旧代码 AddDimDiametric 硬编码 (cx-r,cy)->(cx+r,cy)，永远是水平线，
    #      同心圆的两个直径全叠在同一条线上，根本分不清谁是谁（用户实测提的）。
    #      必须用角度 ang 算弦点。
    #   2) 旧代码引线长度 = r*1.2+gap，小孔（如 φ6.5）的文字会落在外面大圆
    #      （如 φ56）的内部，看着像压在图面上。引线要按「本组最外圈半径」算，
    #      让所有文字都落在最外圆之外的同一圈上，互不遮挡。
    if dim_circles:
        vcx, vcy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        items = []
        for cx, cy, r, obj in geo.get('circles', []):
            if r > 0.5:
                items.append(('D', cx, cy, r))
        for cx, cy, r, a1, a2, obj in geo.get('arcs', []):
            if r > 0.5:
                items.append(('R', cx, cy, r))

        # 按圆心分组：同一圆心的多个圆/弧 = 同心要素，必须错开引线方向
        groups = {}
        for it in items:
            key = (round(it[1], 3), round(it[2], 3))
            groups.setdefault(key, []).append(it)

        # 文字要在最外圈之外留出的净距（mm）
        text_clear = max(8.0, text_h * 2.2)

        # ★ 直径去重 + 限数（用户实测：装配图上 62 个直径标注糊成一团黑）
        #   规则：
        #     1) 同直径（±0.05mm）的孔只标一处，前面加「N×」
        #     2) 总数封顶：零件 10 个、装配体 4 个；超出的不标（装配图本来
        #        就不该把每个孔都标出来，那是零件图的事）
        #     3) 优先标大孔（外形相关），小孔最后
        max_d = int(opts.get('maxDiameters')
                    or (4 if opts.get('assemblyMode') else 10))

        # 按半径归类：{半径key: [组, ...]}
        by_r = {}
        for gkey, members in groups.items():
            max_r0 = max(m[3] for m in members)
            rk = round(max_r0 * 20) / 20.0     # 0.05mm 一档
            by_r.setdefault(rk, []).append((gkey, members))

        keep = []          # [(gkey, members, count, prefix)]
        used = 0
        for rk in sorted(by_r, reverse=True):     # 大孔优先
            for gkey, members in by_r[rk]:
                if used >= max_d:
                    break
                cnt = len(by_r[rk])
                keep.append((gkey, members, cnt))
                used += 1
            if used >= max_d:
                break
        # 同一半径只保留第一组出标注，其余计数合并到它身上
        filtered = {}
        for gkey, members, cnt in keep:
            rk = round(max(m[3] for m in members) * 20) / 20.0
            if rk in filtered:
                filtered[rk][2] += cnt - 1
            else:
                filtered[rk] = [gkey, members, cnt]
        groups = {v[0]: v[1] for v in filtered.values()}
        dup_count = {v[0]: v[2] for v in filtered.values()}
        emit({'stage': 'acad_dim_dedup',
              'groupsTotal': len(by_r),
              'kept': len(groups),
              'maxDiameters': max_d,
              'assemblyMode': bool(opts.get('assemblyMode'))})

        # 组按相对视图中心的方位排序，不同圆心的组也朝不同象限散开
        ordered = sorted(
            groups.items(),
            key=lambda kv: math.atan2(kv[0][1] - vcy, kv[0][0] - vcx))
        gk = 0
        for (gcx, gcy), members in ordered:
            # 同组内半径从大到小：外圈先标，内圈再往外引
            members.sort(key=lambda t: -t[3])
            max_r = members[0][3]
            n = len(members)
            # 组的基础方向：四象限轮流
            base = (math.pi / 4.0) + (gk % 4) * (math.pi / 2.0)
            for j, (kind, cx, cy, r) in enumerate(members):
                if n > 1:
                    # 同心要素按 360/n 均分，保证至少 90°（n<=4 时）分开
                    ang = base + j * (2.0 * math.pi / n)
                else:
                    ang = base
                # 引线：把文字顶到「本组最外圆 + text_clear」那一圈
                lead_len = (max_r + text_clear) - r
                # 下限只兜「引线太短、文字贴到圆周」的情况；
                # 不要用 r 的比例当下限，否则大圆会被顶出很长的引线。
                if lead_len < max(4.0, text_h):
                    lead_len = max(4.0, text_h)
                ca, sa = math.cos(ang), math.sin(ang)
                p1 = (cx - r * ca, cy - r * sa, 0)
                p2 = (cx + r * ca, cy + r * sa, 0)
                chord = (cx + r * ca, cy + r * sa, 0)
                try:
                    if kind == 'D':
                        d = ms.AddDimDiametric(_vec(p1), _vec(p2),
                                               float(lead_len))
                    else:
                        d = ms.AddDimRadial(_vec((cx, cy, 0)), _vec(chord),
                                            float(lead_len))
                    try: d.Layer = LAYER_DIM
                    except Exception: pass
                    # 同直径多处：第一处标「4×φ3.40」这种，避免重复标好几十个
                    try:
                        cnt = int(dup_count.get((gcx, gcy), 1) or 1)
                        if cnt > 1 and j == 0:
                            d.TextOverride = f'{cnt}×<>'
                    except Exception:
                        pass
                    stats['diameters'] += 1
                except Exception:
                    stats['skipped'] += 1
            gk += 1

    # ---------- 角度尺寸 ----------
    # 用户要求：「如果有角度什么的也要考虑进去」。
    # 做法：找**共享端点**的两条直线，算夹角，夹在 10°~170° 之间才算真角度
    # （0°/180° 是共线，不标）。每个视图最多标 max_angles 个，避免糊成一片。
    if opts.get('dimAngles', True):
        max_angles = int(opts.get('maxAngles')
                         or (1 if opts.get('assemblyMode') else 2))
        segs = []
        for x1, y1, x2, y2, obj in geo.get('lines', []):
            if math.hypot(x2 - x1, y2 - y1) < max(2.0, text_h * 0.6):
                continue
            segs.append((x1, y1, x2, y2))
        joint_tol = max(0.4, tol * 1.5)
        made_angles = 0
        used_pts = []
        for i in range(len(segs)):
            if made_angles >= max_angles:
                break
            a = segs[i]
            for j in range(i + 1, len(segs)):
                if made_angles >= max_angles:
                    break
                b = segs[j]
                # 找共享端点（四种组合）
                pair = None
                for pa in ((a[0], a[1]), (a[2], a[3])):
                    for pb in ((b[0], b[1]), (b[2], b[3])):
                        if (abs(pa[0] - pb[0]) <= joint_tol
                                and abs(pa[1] - pb[1]) <= joint_tol):
                            pair = (pa, pb)
                            break
                    if pair:
                        break
                if not pair:
                    continue
                vx, vy = pair[0]
                # 两个方向（从顶点向外）
                da = (a[2] - a[0], a[3] - a[1])
                db = (b[2] - b[0], b[3] - b[1])
                if (abs(da[0]) < 1e-9 and abs(da[1]) < 1e-9):
                    continue
                if (abs(db[0]) < 1e-9 and abs(db[1]) < 1e-9):
                    continue
                # 让方向从顶点指出去
                if (da[0] * (vx - a[0]) + da[1] * (vy - a[1])) > 0:
                    da = (-da[0], -da[1])
                if (db[0] * (vx - b[0]) + db[1] * (vy - b[1])) > 0:
                    db = (-db[0], -db[1])
                ang = math.degrees(math.acos(max(-1.0, min(1.0,
                    (da[0] * db[0] + da[1] * db[1])
                    / (math.hypot(*da) * math.hypot(*db))))))
                if ang < 10.0 or ang > 170.0:
                    continue
                # 顶点附近已经标过就不再标
                if any(math.hypot(vx - px, vy - py) < text_h * 3
                       for px, py in used_pts):
                    continue
                la, lb = math.hypot(*da), math.hypot(*db)
                scale = max(text_h * 3.0, min(la, lb) * 0.6)
                p1 = (vx + da[0] / la * scale, vy + da[1] / la * scale, 0)
                p2 = (vx + db[0] / lb * scale, vy + db[1] / lb * scale, 0)
                # 文字放在角平分线外侧
                bx = (da[0] / la + db[0] / lb)
                by = (da[1] / la + db[1] / lb)
                bl = math.hypot(bx, by) or 1.0
                tp = (vx + bx / bl * (scale + text_h * 2.0),
                      vy + by / bl * (scale + text_h * 2.0), 0)
                try:
                    d = ms.AddDimAngular(_vec((vx, vy, 0)), _vec(p1), _vec(p2),
                                         _vec(tp))
                    try:
                        d.Layer = LAYER_DIM
                        d.color = 7
                        d.Lineweight = LW_THIN
                        d.TextHeight = text_h
                    except Exception:
                        pass
                    stats['angles'] = stats.get('angles', 0) + 1
                    made_angles += 1
                    used_pts.append((vx, vy))
                except Exception:
                    stats['skipped'] += 1
        if made_angles:
            emit({'stage': 'acad_angles', 'count': made_angles})

    # ---------- 辅助框：画出来校验排布，再删掉 ----------
    if show_guide and placed:
        # 辅助框只画「尺寸线+文字」的紧凑条带，不含尺寸界线。
        # 这样一眼能看出真实碰撞关系（尺寸界线交叉是允许的）。
        rects = []
        half = strip / 2.0
        for (x0, y0, x1, y1) in placed.values():
            rects.append((min(x0, x1), (y0 + y1) / 2.0 - half,
                          max(x0, x1), (y0 + y1) / 2.0 + half))
        cnt = draw_guide_rects(doc, rects, LAYER_GUIDE, delete_after=True)
        stats['guideBoxes'] = stats.get('guideBoxes', 0) + cnt
        stats['stripThickness'] = round(strip, 2)
        stats['rowGap'] = round(row_gap, 2)

    return {'bbox': bbox, 'placed': len(placed)}


def normalize_dimensions(doc, text_h=3.5, lineweight=9, layer=LAYER_DIM):
    """把已经创建好的尺寸统一校正到国标字号/线宽。

    ⚠ 实测坑：即使先 SetVariable('DIMTXT', 3.5)，AddDimRotated 创建出来的
    尺寸实体 TextHeight 仍是 0.18mm（几乎看不见）。原因是以图幅为基准的
    标注样式覆盖没有在创建时被继承。
    所以在创建完成后必须再遍历一遍，把 TextHeight/ArrowheadSize/线宽
    直接写到实体上——这是唯一可靠的方式。

    返回校正的尺寸数量
    """
    ms = doc.ModelSpace
    n = 0
    for i in range(ms.Count):
        try:
            e = ms.Item(i)
        except Exception:
            continue
        try:
            if 'Dimension' not in str(e.ObjectName):
                continue
            # 字号
            try: e.TextHeight = float(text_h)
            except Exception: pass
            # 箭头大小 ≈ 字高
            try: e.ArrowheadSize = float(text_h)
            except Exception: pass
            # 文字与尺寸线间隙
            try: e.TextGap = float(text_h) * 0.29
            except Exception: pass
            # 尺寸界线超出/偏移
            try: e.ExtensionLineExtend = float(text_h) * 0.57
            except Exception: pass
            try: e.ExtensionLineOffset = float(text_h) * 0.29
            except Exception: pass
            # 线宽（1/100 mm）
            try: e.LineWeight = int(lineweight)
            except Exception: pass
            # 归到标注专用层
            try: e.Layer = layer
            except Exception: pass
            n += 1
        except Exception:
            continue
    return n


def auto_dimension(doc, opts=None):
    """AutoCAD 侧自动标注主流程。

    按用户提的分阶段思路组织：

      阶段 A  收集几何（跳过图框/标题栏图层）
      阶段 B  聚类成「视图」
      阶段 C  （可选）把视图搬到图框内的国标固定位置
                —— 对应「在 AutoCAD 里一个个挪到图内固定位置」
      阶段 D  逐视图标注，尺寸沿边区间装箱分层
                —— 对应「包围框调紧凑再删掉」
      阶段 E  按图幅自动定标注字高

    返回统计 dict
    """
    opts = opts or {}
    paper = opts.get('paper', 'A3')
    landscape = bool(opts.get('landscape', True))
    max_views = int(opts.get('maxViews', 4))

    ms = doc.ModelSpace

    # ---- 阶段 0：图层隔离（辅助框不干扰正式图）----
    setup_layers(doc)

    # ---- 阶段 A：收集几何 ----
    paper_w, paper_h = opts.get('paperW'), opts.get('paperH')
    if not paper_w:
        paper_w, paper_h = paper_size_mm(paper, landscape)
    raw_geo = collect_entities(doc, skip_layers=set())
    raw = _bbox(raw_geo)
    if not paper_w and raw:
        paper_w = round((raw[2] - raw[0]) / 10.0) * 10 or raw[2]
        paper_h = round((raw[3] - raw[1]) / 10.0) * 10 or raw[3]

    geo = collect_entities(doc, paper=(paper_w, paper_h))

    # ---- 阶段 B：聚类成视图 ----
    clusters = cluster_views(geo, gap=float(opts.get('viewGapMm', 18.0)))
    emit({'stage': 'acad_views',
          'found': len(clusters),
          'sizes': [[round(c['bbox'][2] - c['bbox'][0], 1),
                     round(c['bbox'][3] - c['bbox'][1], 1)]
                    for c in clusters[:6]]})

    # ---- 阶段 B2：合并共线线段（用户要求）----
    # SolidWorks 导出的一条轮廓常被拆成多段，先合并，
    # 消掉重复标注、也让整视图选取更可靠。
    if opts.get('mergeSegments', True):
        total_before = total_after = 0
        merged_views = 0
        for c in clusters:
            if (c['bbox'][2] - c['bbox'][0]) < 8.0 or \
               (c['bbox'][3] - c['bbox'][1]) < 8.0:
                continue
            before, after = merge_collinear_segments(
                doc, c, tol=float(opts.get('mergeTolMm', 0.15)))
            total_before += before
            total_after += after
            merged_views += 1
        if merged_views:
            emit({'stage': 'acad_merge', 'views': merged_views,
                  'linesBefore': total_before, 'linesAfter': total_after})
            # 合并后几何变了，重新收集
            geo = collect_entities(doc, paper=(paper_w, paper_h))
            clusters = cluster_views(geo,
                                     gap=float(opts.get('viewGapMm', 18.0)))

    # ---- 阶段 C：分图层 + 按图层移动（用户方案）----
    # 「把各个视图都分图层，工程图框也一个图层，按图层来移动」
    # 一视图一图层后，图元归属不再需要猜，移动和标注都按图层做，
    # 彻底避免包围框重叠导致的错配和视图撕裂。
    layout_placed = {}
    view_layers = []
    view_boxes = opts.get('viewBoxes')
    if view_boxes and opts.get('layoutByLayer', True):
        try:
            # 若 dimension_dwg 已经在建标注样式**之前**分好图层，
            # 这里直接复用（用户要求：先分图层 14 → 再建样式 13）。
            pre = opts.get('preClassified')
            if pre:
                counts = pre
            else:
                counts = classify_into_layers(doc, view_boxes, paper_w, paper_h,
                                              bom_box=opts.get('bomBoxMm'))
                emit({'stage': 'acad_layers', 'counts': counts})

            # ⑬ 分完图层**之后**再建 GB 标注样式
            if opts.get('setupStyleInside'):
                try:
                    sn, vok = setup_gb_dimstyle(
                        doc, 'GB',
                        scale_den=opts.get('scaleDen'),
                        scale_num=opts.get('scaleNum') or 1)
                    emit({'stage': 'acad_style', 'style': sn, 'vars': vok})
                    opts = {**opts, 'styleName': sn}
                except Exception as exc:
                    emit({'stage': 'acad_style', 'error': str(exc)})

            view_layers = [n for n in counts if n.startswith('DSH_VIEW_')]
            if view_layers and opts.get('moveViews', True):
                layout_placed = move_views_by_layer(
                    doc, view_layers, paper, landscape,
                    title_h=float(opts.get('titleBlockMm', 55.0)),
                    reserved=opts.get('reservedRects') or [],
                    gap_mm=float(opts.get('viewGapMm', 6.0)))
                emit({'stage': 'acad_layout_by_layer',
                      'placed': layout_placed,
                      'reserved': opts.get('reservedRects') or []})
        except Exception as exc:
            emit({'stage': 'acad_layout_by_layer', 'error': str(exc)})

    # ---- 阶段 C2：补中心线（GB 细点画线）----
    # 必须在视图搬完之后画，否则中心线位置和圆对不上。
    if view_layers and opts.get('centerlines', True):
        try:
            cl = draw_centerlines(doc, view_layers,
                                  extend=float(opts.get('centerlineExtend', 3.0)))
            emit({'stage': 'acad_centerlines', **cl})
        except Exception as exc:
            emit({'stage': 'acad_centerlines', 'ok': False,
                  'error': f'{type(exc).__name__}: {exc}'})

    # ---- 阶段 C3：线宽归一到 GB 粗细搭配（2:1）----
    if view_layers and opts.get('normalizeLineweight', True):
        try:
            lw = normalize_lineweights(doc, list(view_layers) + [LAYER_CENTER])
            emit({'stage': 'acad_lineweight', **lw})
        except Exception as exc:
            emit({'stage': 'acad_lineweight', 'ok': False,
                  'error': f'{type(exc).__name__}: {exc}'})

    # 兜底：没有视图边界信息时退回几何聚类
    if not view_layers:
        geo = collect_entities(doc, paper=(paper_w, paper_h))
        clusters = cluster_views(geo, gap=float(opts.get('viewGapMm', 18.0)))

    # 分层模式下，把每个视图图层当成一「簇」，逐层标注
    # —— 这样标注也按图层走，归属确定，不用再聚类
    if view_layers:
        clusters = []
        for nm in view_layers:
            cc = {'lines': [], 'circles': [], 'arcs': [], 'layer': nm}
            xs, ys = [], []
            for i in range(ms.Count):
                try:
                    e = ms.Item(i)
                    if str(e.Layer) != nm:
                        continue
                    onm = str(e.ObjectName)
                except Exception:
                    continue
                try:
                    if onm == 'AcDbLine':
                        s0 = e.StartPoint; t0 = e.EndPoint
                        cc['lines'].append((float(s0[0]), float(s0[1]),
                                            float(t0[0]), float(t0[1]), e))
                        xs += [s0[0], t0[0]]; ys += [s0[1], t0[1]]
                    elif onm == 'AcDbCircle':
                        c0 = e.Center; r0 = float(e.Radius)
                        cc['circles'].append((float(c0[0]), float(c0[1]), r0, e))
                        xs += [c0[0] - r0, c0[0] + r0]
                        ys += [c0[1] - r0, c0[1] + r0]
                    elif onm == 'AcDbArc':
                        c0 = e.Center; r0 = float(e.Radius)
                        cc['arcs'].append((float(c0[0]), float(c0[1]), r0,
                                           float(e.StartAngle), float(e.EndAngle), e))
                        xs += [c0[0] - r0, c0[0] + r0]
                        ys += [c0[1] - r0, c0[1] + r0]
                except Exception:
                    continue
            if xs:
                cc['bbox'] = (min(xs), min(ys), max(xs), max(ys))
                cc['area'] = max(max(xs) - min(xs), 1.0) * max(max(ys) - min(ys), 1.0)
                clusters.append(cc)
        clusters.sort(key=lambda c: -c['area'])

    # ---- 阶段 E：按图幅定字高 ----
    # ---- 阶段 E：按图幅定字高 ----
    big = [c for c in clusters
           if (c['bbox'][2] - c['bbox'][0]) > 8.0
           and (c['bbox'][3] - c['bbox'][1]) > 8.0][:max_views]
    view_h = max([(c['bbox'][3] - c['bbox'][1]) for c in big], default=0)
    text_h, text_basis = text_height_for_paper(paper, view_h)
    try:
        doc.SetVariable('DIMTXT', text_h)
        # 箭头：GB/T 4458.4 规定箭头长度约 2~5mm（字高 3.5 时取 2.5），
        # 不是「等于字高」。原来写 text_h 会让箭头过大、尺寸线显得笨重。
        doc.SetVariable('DIMASZ', max(2.0, min(3.0, text_h * 0.71)))
        doc.SetVariable('DIMGAP', text_h * 0.29)
        doc.SetVariable('DIMEXE', text_h * 0.57)
        # 尺寸线/尺寸界线：细实线 b/2
        doc.SetVariable('DIMLWD', LW_THIN)
        doc.SetVariable('DIMLWE', LW_THIN)
        # 尺寸界线离开轮廓 ≥2mm
        doc.SetVariable('DIMEXO', max(2.0, text_h * 0.57))
    except Exception:
        pass
    emit({'stage': 'acad_text_height', 'height': text_h, 'basis': text_basis})

    stats = {'overall': 0, 'diameters': 0, 'linear': 0,
             'skipped': 0, 'views': 0, 'textHeight': text_h}

    # ---- 阶段 D：逐视图标注 ----
    for c in clusters:
        bbox = c['bbox']
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]

        if w < 8.0 or h < 8.0:
            continue
        if paper_w and paper_h:
            if w > paper_w * 0.9 or h > paper_h * 0.9:
                continue
            edge = 15.0
            if (bbox[0] < edge or bbox[1] < edge
                    or bbox[2] > paper_w - edge
                    or bbox[3] > paper_h - edge):
                continue
        if stats['views'] >= max_views:
            break

        _dim_one_view(ms, doc, c, bbox, stats, opts, paper, text_h)
        stats['views'] += 1

    # ★ 强制统一尺寸文字高度 ★
    # 实测踩坑：尺寸建好后 TextHeight 只有 **0.18mm**（不是 DIMTXT 的 3.5），
    # 打印出来数字完全看不见 —— 尺寸线、箭头都在，就是没有字，
    # 看图时极易误判成「没标注」。原因是 dim 实体在创建时把当时的
    # 标注样式高度烘进了自身属性，之后改 DIMTXT 不会回溯修改已有实体。
    # 所以这里逐个实体覆盖，保证和 text_h 一致。
    # ① 统一颜色/线宽/字高/小数位
    try:
        norm = normalize_dimension_style(doc, text_h=text_h)
        emit({'stage': 'acad_dim_style', **norm})
    except Exception as exc:
        emit({'stage': 'acad_dim_style', 'ok': False, 'error': str(exc)})

    # ② 遮挡检查 + 挪位置，**多轮**（用户要求「多次检查」）
    try:
        rounds = int(opts.get('overlapRounds', 3))
        ov = resolve_dimension_overlaps(doc, text_h=text_h, rounds=rounds)
        emit({'stage': 'acad_overlap', **ov})
        # 挪完再统一一次样式（挪动后个别属性可能被重置）
        normalize_dimension_style(doc, text_h=text_h)
    except Exception as exc:
        emit({'stage': 'acad_overlap', 'ok': False, 'error': str(exc)})

    # 收尾：清掉辅助层残留，保证正式图面干净
    try:
        cleanned = cleanup_guide_layer(doc)
        if cleanned:
            stats['guideCleaned'] = cleanned
    except Exception:
        pass

    return stats


def dimension_dwg(dwg_path, opts=None, visible=False, out_path=None, exe_hint=None):
    """打开 DWG，建国标样式，自动标注，另存。"""
    opts = opts or {}
    acad = connect_acad(visible, exe_hint=exe_hint or opts.get('autocadExe'))
    doc = open_doc(acad, dwg_path)
    try:
        # 自动标注要在模型空间做
        try:
            doc.ActiveSpace = 1   # acModelSpace
        except Exception:
            pass

        # 把图幅信息算出来（后面分图层和标注都要用）
        try:
            from engine import PAPER_SIZE_M
        except Exception:
            PAPER_SIZE_M = {}
        pw = opts.get('paperW')
        ph = opts.get('paperH')
        if not pw and opts.get('paper'):
            size = PAPER_SIZE_M.get(str(opts['paper']).upper())
            if size:
                pw, ph = size[0] * 1000.0, size[1] * 1000.0
        if pw:
            opts = {**opts, 'paperW': pw, 'paperH': ph}

        # 顺序（用户要求）：⑭ 先分图层 → ⑬ 再建标注样式 → ⑮ 再移动视图。
        # 注意：**分图层必须在 auto_dimension 内部做**，不能提前到打开文档后，
        # 否则图框/标题栏图元分类会退化（实测 DSH_FRAME 从 51 个掉到 4 个，
        # 图框全留在 0 层，最后打印出来是一张白图）。
        # 所以这里只把「比例」传进去，让 auto_dimension 在分完图层后建样式。
        scale_den = opts.get('scaleDen') or opts.get('scale_den')
        scale_num = opts.get('scaleNum') or opts.get('scale_num') or 1
        opts = {**opts,
                'setupStyleInside': True,
                'scaleDen': scale_den,
                'scaleNum': scale_num}

        stats = auto_dimension(doc, opts)
        emit({'stage': 'acad_dimensioned', **stats})
        style_name = stats.get('styleName') or 'GB'

        target = os.path.abspath(out_path or dwg_path)
        if os.path.abspath(dwg_path).lower() == target.lower():
            doc.Save()
        else:
            doc.SaveAs(target)

        # ★ 在同一会话里直接把加工后的结果导出成 DXF ★
        # 之前是在外面另开一次 AutoCAD 去 Open(dwg) 再 SaveAs(dxf)，
        # 但这时文档**已经打开**，拿到的是失效的 COM 对象，报
        # `<unknown>.SaveAs`、后面连 Count 都读不到。
        # 现在趁 doc 还活着直接另存，最后再存回 DWG 保住格式。
        dxf_out = opts.get('dxfPath')
        dxf_ok = None
        if dxf_out:
            dxf_out = os.path.abspath(dxf_out)
            try:
                doc.SaveAs(dxf_out, 61)      # 61 = ac2013_dxf
                dxf_ok = True
            except Exception:
                try:
                    doc.SaveAs(dxf_out)
                    dxf_ok = True
                except Exception as exc:
                    dxf_ok = False
                    emit({'stage': 'acad_redxf', 'ok': False,
                          'error': f'{type(exc).__name__}: {exc}'})
            # 文档关联名已被改成 .dxf，存回 DWG 免得下次打开是 DXF
            if dxf_ok:
                try:
                    doc.SaveAs(target)
                except Exception:
                    pass
                size = os.path.getsize(dxf_out) if os.path.isfile(dxf_out) else 0
                emit({'stage': 'acad_redxf', 'ok': bool(size),
                      'dxf': dxf_out, 'size': size})

        try:
            doc.Close(False)
        except Exception:
            pass
        return {'ok': True, 'stats': stats, 'style': style_name,
                'out': target, 'dxf': dxf_out, 'dxfOk': dxf_ok}
    finally:
        try:
            doc.Close(False)
        except Exception:
            pass
        # 收掉 AutoCAD，避免进程堆积
        try:
            import threading
            def _quit():
                try:
                    for _ in range(20):
                        if acad.Documents.Count == 0:
                            break
                        try:
                            acad.Documents.Item(0).Close(False)
                        except Exception:
                            break
                        time.sleep(0.2)
                except Exception:
                    pass
                try:
                    acad.Quit()
                except Exception:
                    pass
            th = threading.Thread(target=_quit, daemon=True)
            th.start()
            th.join(15)
        except Exception:
            pass


def run(payload):
    return dimension_dwg(
        payload['dwg'],
        payload.get('options') or {},
        payload.get('visible', False),
        payload.get('out'))


if __name__ == '__main__':
    payload = {}
    try:
        if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
            with open(sys.argv[1], 'r', encoding='utf-8-sig') as fh:
                payload = json.load(fh)
        else:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        result = run(payload)
    except Exception as exc:
        result = {'ok': False, 'error': str(exc),
                  'errorType': type(exc).__name__,
                  'trace': traceback.format_exc()[-2500:]}
    emit({'stage': 'done', **result})
    sys.exit(0 if result.get('ok') else 1)
