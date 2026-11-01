# -*- coding: utf-8 -*-
"""
stage_boxes.py —— 分阶段跑：SolidWorks 出图元 → AutoCAD 只画包围框 → 停。

用途（用户要求）：
    「先给我模拟下打开零件，做到视图在 AutoCAD 做包围框那步，然后暂停等我检查」

它做三件事：
    ① 调 engine.build(stopAfter='export')：SolidWorks 开模型 → 建图 → 插视图
       → 判断剖视 → 记包围框 → 解除对齐 → 摊到框外 → 导出 DWG（不标注）
    ② AutoCAD 打开 DWG，**只画框**：
         每个视图一个框 + 工程图图框 + 明细表框（装配图才有）
       框放在 DSH_BOX 图层（红色、虚线、不打印），方便肉眼核对
    ③ 保存 DWG + 出 PNG 预览 + 打印所有框坐标，然后**停**（不做分图层/移动/标注）

用法：
    python stage_boxes.py payload.json
payload 必备：model、outDir；可选 paperSize / views / sectionView 等。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'lib'))

import engine  # noqa: E402


LAYER_BOX = 'DSH_BOX'


def _vec(t):
    from win32com.client import VARIANT
    import pythoncom
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(t[0]), float(t[1]), float(t[2])))


def ensure_box_layer(doc):
    """DSH_BOX 图层：红色、虚线、不打印（只给人看）。"""
    try:
        try:
            lay = doc.Layers.Item(LAYER_BOX)
        except Exception:
            lay = doc.Layers.Add(LAYER_BOX)
        try:
            lay.color = 1          # 红
        except Exception:
            pass
        try:
            lay.Linetype = 'DASHED'
        except Exception:
            pass
        try:
            lay.Plottable = False  # 不打印
        except Exception:
            pass
        return lay
    except Exception:
        return None


def draw_box(ms, rect, label=''):
    """在模型空间画一个矩形框，返回是否成功。rect = (x0,y0,x1,y1) 毫米。"""
    x0, y0, x1, y1 = rect
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    ok = 0
    for i in range(4):
        a = pts[i]
        b = pts[(i + 1) % 4]
        try:
            ln = ms.AddLine(_vec((a[0], a[1], 0)), _vec((b[0], b[1], 0)))
            ln.Layer = LAYER_BOX
            ln.color = 1
            try:
                ln.Linetype = 'DASHED'
            except Exception:
                pass
            ok += 1
        except Exception:
            pass
    if label:
        try:
            t = ms.AddText(str(label), _vec((x0 + 1.0, y1 + 1.0, 0)), 2.5)
            t.Layer = LAYER_BOX
            t.color = 1
        except Exception:
            pass
    return ok == 4


def main():
    payload_path = sys.argv[1] if len(sys.argv) > 1 else ''
    if not payload_path or not os.path.isfile(payload_path):
        print('用法: python stage_boxes.py payload.json')
        return 2
    with open(payload_path, encoding='utf-8') as f:
        payload = json.load(f)

    payload = {**payload, 'action': 'build', 'stopAfter': 'export'}

    print('=== ① SolidWorks：出图元并导出 DWG（不标注）===')
    res = engine.build(payload)
    if not res.get('ok'):
        print('✘ SolidWorks 阶段失败:', res.get('error'))
        return 1
    dwg = res.get('dwg')
    boxes = res.get('viewBoxes') or []
    print(f'  DWG: {dwg}')
    print(f'  视图数: {len(boxes)}  图幅: {res.get("paper")}  装配体: {res.get("isAssembly")}')
    for i, b in enumerate(boxes, 1):
        print(f'    视图{i}: x {b["x0"]:.1f}~{b["x1"]:.1f}  y {b["y0"]:.1f}~{b["y1"]:.1f}'
              f'  ({b["x1"]-b["x0"]:.1f}×{b["y1"]-b["y0"]:.1f})')

    print()
    print('=== ② AutoCAD：只画包围框 ===')
    import win32com.client as win32
    import apps
    apps.ensure_running(apps.AUTOCAD, payload.get('autocadExe'), log=print)
    acad = win32.Dispatch('AutoCAD.Application')
    try:
        acad.Documents.Open(os.path.abspath(dwg))
    except Exception as exc:
        print('✘ 打开 DWG 失败:', exc)
        return 1
    import time
    time.sleep(2)
    doc = acad.ActiveDocument
    ensure_box_layer(doc)
    ms = doc.ModelSpace

    drawn = 0
    # 视图框
    for i, b in enumerate(boxes, 1):
        r = (float(b['x0']), float(b['y0']), float(b['x1']), float(b['y1']))
        if draw_box(ms, r, f'VIEW{i}'):
            drawn += 1
            print(f'  画视图框 {i}: {tuple(round(v,1) for v in r)}')
        else:
            print(f'  ✘ 视图框 {i} 画失败')
    # 工程图图框
    pw, ph = engine.PAPER_SIZE_M.get(str(res.get('paper')).upper(), (0.420, 0.297))
    if not res.get('landscape', True):
        pw, ph = ph, pw
    pw_mm, ph_mm = pw * 1000.0, ph * 1000.0
    if draw_box(ms, (0.0, 0.0, pw_mm, ph_mm), 'FRAME'):
        drawn += 1
        print(f'  画图框: (0, 0, {pw_mm:.0f}, {ph_mm:.0f})')
    # 明细表框（装配图）
    bom = res.get('bomBoxMm')
    if bom and len(bom) >= 4:
        r = (float(bom[0]), float(bom[1]), float(bom[2]), float(bom[3]))
        if draw_box(ms, r, 'BOM'):
            drawn += 1
            print(f'  画明细表框: {tuple(round(v,1) for v in r)}')
    else:
        print('  （零件图，无明细表框）')
    # 标题栏预留区
    for rr in (res.get('reservedRects') or []):
        if len(rr) >= 4:
            r = tuple(float(v) for v in rr[:4])
            if draw_box(ms, r, 'TITLE-RESERVED'):
                drawn += 1
                print(f'  画标题栏预留框: {tuple(round(v,1) for v in r)}')

    # ===== ④ 分图层（用刚画的框 + 图框 + 明细表框）=====
    # 用户要的是「画包围框 + 分图层」一起做完再看，所以这一步不能省。
    print()
    print('=== ④ 按包围框分图层 ===')
    import acad_dimension as ad
    try:
        counts = ad.classify_into_layers(
            doc, boxes, pw_mm, ph_mm,
            bom_box=(bom if (bom and len(bom) >= 4) else None))
        print('  分图层结果:', counts)
        # 把每个视图图层的实体数再打一遍，方便核对
        for nm in sorted(counts):
            if nm.startswith('DSH_VIEW_'):
                print(f'    {nm}: {counts[nm]} 个实体')
    except Exception as exc:
        print('  ✘ 分图层失败:', exc)
        counts = {}

    # ===== ⑤ 缩放全图（不然打开只看到图框，视图在上方看不见）=====
    # ⚠ AutoCAD 以「不可见」模式启动时，Application.ZoomExtents 会报
    #   「AutoCAD 应用程序不可见」（COM 异常），这里必须吞掉；
    #   用户自己打开 DWG 时界面会正常缩放。
    try:
        if getattr(doc.Application, 'Visible', False):
            doc.Application.ZoomExtents()
            print('  已 ZoomExtents')
        else:
            print('  AutoCAD 处于隐藏模式，跳过 ZoomExtents（用户打开时会自动适配）')
    except Exception as exc:
        print('  缩放跳过:', str(exc)[:80])

    try:
        doc.Save()
    except Exception as exc:
        print('  保存失败:', exc)

    print()
    print(f'=== ⑥ 完成：{drawn} 个框 + 分图层，已暂停 ===')
    print(f'  DWG: {dwg}')
    print('  下一步（等你确认）：建标注样式 → 移视图进框 → 对齐 → 标注 → 遮挡检查')
    out = {
        'ok': True,
        'stopped': 'boxes+layers',
        'dwg': dwg,
        'boxesDrawn': drawn,
        'layers': counts,
        'viewBoxes': boxes,
        'paper': res.get('paper'),
        'landscape': res.get('landscape'),
    }
    print('<<<STAGE_RESULT')
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print('STAGE_RESULT>>>')
    return 0


if __name__ == '__main__':
    sys.exit(main())
