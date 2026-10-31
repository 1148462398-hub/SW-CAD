# -*- coding: utf-8 -*-
"""
drawset.py —— 拖一个装配体进去，出一整套图纸

用户需求：
    「装配图也要像零件图那样去绘制和标注吧，先装配再一起出一个装配总图，
      然后再出每个零件的零件图，装配总图里有装配尺寸的标注还有 BOM。
      多个 CAD 图在一个 DXF 文件里就可以了，只是多放几个工程图图框那种」

流程：
    ① 打开装配体 → 取去重后的零件清单（assembly_components）
    ② 装配总图：视图 + 明细表(BOM) + 零件序号(气泡) + 装配尺寸   → xxx.dwg
    ③ 每个零件：视图 + 尺寸 + 公差 + 粗糙度                       → 零件A.dwg ...
    ④ 合并：所有 dwg 并进一个 DXF（acad_merge.merge_dwgs）

为什么每张图独立出 DWG 再合并：
    SolidWorks 的 IDrawingDoc::NewSheet2/3/4 的 `TemplateIn` 参数在这台机器上
    始终报「类型不匹配 (None, 3)」（空串 / 模板路径 / pythoncom.Empty 都试过），
    所以不用 SolidWorks 多图纸，改用 AutoCAD 合并，反而更可控。
"""

import os
import time

import engine


def collect_parts(asm_path, emit=None, exe_hint=None):
    """打开装配体，返回去重后的零件清单。"""
    import win32com.client as win32

    sw = engine.connect(False, exe_hint=exe_hint,
                        log=(lambda m: emit({'stage': 'drawset_log',
                                             'msg': m})) if emit else None)
    doc, err, warn = engine.open_model(sw, asm_path)
    if doc is None:
        return {'ok': False, 'error': f'打开装配体失败 err={err}', 'parts': []}
    try:
        info = engine.assembly_components(doc)
    finally:
        try:
            sw.CloseDoc(doc.GetTitle)
        except Exception:
            pass
    return info


def build_set(payload, emit):
    """出一整套图纸。返回汇总结果。"""
    asm = payload.get('model') or ''
    if not asm:
        return {'ok': False, 'error': '缺少 model 参数'}
    if not os.path.isfile(asm):
        return {'ok': False, 'error': f'装配体不存在: {asm}'}
    if os.path.splitext(asm)[1].lower() != '.sldasm':
        return {'ok': False, 'error': 'drawset 需要 .SLDASM 装配体'}

    out_dir = payload.get('outDir') or os.path.dirname(asm)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(asm))[0]
    exe_hint = payload.get('solidworksExe')

    # ── ① 零件清单 ──
    emit({'stage': 'drawset_collect'})
    comps = collect_parts(asm, emit=emit, exe_hint=exe_hint)
    if not comps.get('ok'):
        return {'ok': False, 'error': comps.get('error', '取零件清单失败')}

    parts = comps.get('parts', [])
    sheets_total = 1 + len(parts)
    emit({'stage': 'drawset_plan',
          'assembly': os.path.basename(asm),
          'parts': [os.path.basename(p['path']) for p in parts],
          'sheets': sheets_total})

    # ── ② 装配总图 ──
    emit({'stage': 'drawset_begin', 'sheet': 1, 'total': sheets_total,
          'kind': 'assembly', 'name': os.path.basename(asm)})
    asm_payload = {**payload,
                   'action': 'build',
                   'model': asm,
                   'outDir': out_dir,
                   'bom': True,
                   'balloons': True}
    asm_res = engine.build(asm_payload)
    sheet_files = []
    if asm_res.get('dwg'):
        sheet_files.append(('装配总图', asm_res['dwg']))
    emit({'stage': 'drawset_sheet_done', 'sheet': 1, 'total': sheets_total,
          'kind': 'assembly', 'ok': bool(asm_res.get('dwg'))})

    # ── ③ 每个零件的零件图 ──
    part_results = []
    for idx, p in enumerate(parts, start=1):
        pname = os.path.basename(p['path'])
        emit({'stage': 'drawset_begin', 'sheet': idx + 1, 'total': sheets_total,
              'kind': 'part', 'name': pname})

        # 零件图：不需要明细表和气泡，但要 AutoCAD 后处理标注
        part_payload = {**payload,
                        'action': 'build',
                        'model': p['path'],
                        'outDir': out_dir,
                        'bom': False,
                        'balloons': False,
                        'acadDimension': payload.get('partDimension', True)}
        try:
            r = engine.build(part_payload)
        except Exception as exc:
            r = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        part_results.append({'part': pname, 'ok': bool(r.get('ok')),
                             'dwg': r.get('dwg'), 'error': r.get('error')})
        if r.get('dwg'):
            sheet_files.append((os.path.splitext(pname)[0], r['dwg']))
        emit({'stage': 'drawset_sheet_done', 'sheet': idx + 1,
              'total': sheets_total, 'kind': 'part', 'name': pname,
              'ok': bool(r.get('ok'))})

    # ── ④ 合并成一个 DXF ──
    merged = None
    if payload.get('mergeSheets', True) and len(sheet_files) > 0:
        emit({'stage': 'drawset_merge_start', 'sheets': len(sheet_files)})
        try:
            import acad_merge
            # 按图幅宽度决定间隔，A3 横向 420mm，留 20mm 缝
            paper = str(payload.get('paperSize') or 'A3').upper()
            width = engine.PAPER_SIZE_M.get(paper, (0.420, 0.297))[0] * 1000.0
            out_dxf = os.path.join(out_dir, base + '_整套图纸.dxf')
            merged = acad_merge.merge_dwgs(
                [f for _n, f in sheet_files], out_dxf,
                gap_mm=float(payload.get('sheetGapMm', 20.0)),
                sheet_w_mm=width,
                exe_hint=payload.get('autocadExe'),
                log=(lambda m: emit({'stage': 'drawset_log', 'msg': m})))
            emit({'stage': 'drawset_merge_done', **{
                k: v for k, v in merged.items() if k != 'detail'}})
        except Exception as exc:
            merged = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
            emit({'stage': 'drawset_merge_error', 'error': merged['error']})

    return {
        'ok': bool(sheet_files),
        'assembly': asm,
        'outDir': out_dir,
        'parts': [os.path.basename(p['path']) for p in parts],
        'sheets': [{'name': n, 'path': f} for n, f in sheet_files],
        'sheetCount': len(sheet_files),
        'partResults': part_results,
        'merged': merged,
        'assemblyDrawing': {
            'drw': asm_res.get('drawing'),
            'dxf': asm_res.get('dxf'),
            'dwg': asm_res.get('dwg'),
            'bom': asm_res.get('bom'),
            'balloons': asm_res.get('balloons'),
        },
    }
