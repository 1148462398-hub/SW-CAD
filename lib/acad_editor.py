# -*- coding: utf-8 -*-
"""
dsh-cad-draw / acad_editor.py

用 AutoCAD COM 打开 SolidWorks 导出的 DWG，改写标题栏文字，另存。

═══════════ 本机实测结论（AutoCAD 2018 简体中文）═══════════

1) AutoCAD COM 可用：Dispatch('AutoCAD.Application') → Documents.Open(dwg)
   模型空间 doc.ModelSpace（实际图元都在 Model 布局里，Layout1/Layout2 是空的）

2) SolidWorks 导出的 DWG 里，GB 图框标题栏是**按坐标定位的普通 TEXT 对象**，
   不是属性块、也没有字段链接。因此最可靠的改法是**按插入点坐标匹配**：
     材质   (336.2, 60.0)  原值 '材质 <未指定>'
     图样名称 (385.5, 41.3) 原值 '“图样名称”'
     图样代号 (385.5, 26.5) 原值 '“图样代号”'
     比例   (354.0, 35.4)  原值 '1:1'
     重量   (341.9, 35.4)  原值 '0.00'
     版本   (340.1, 16.7)  → 是标签，不是值
   注意这些坐标是 **A3 图幅**下的；换图幅要按比例换算。

3) 匹配用「文字的原始占位文本」更稳，坐标只做兜底——因为换图幅坐标会变。

4) 图层：0 / FORMAT / 10 / SLD-0 / 5；标题栏在 FORMAT 上。
"""

import json
import os
import sys
import time
import traceback

# 标题栏字段的「原始占位文字」——按这个匹配最稳
PLACEHOLDER_HINTS = {
    'material': ['材质 <未指定>', '材质<未指定>', '材质'],
    'title': ['“图样名称”', '"图样名称"', '图样名称'],
    'number': ['“图样代号”', '"图样代号"', '图样代号'],
    'weight': ['0.00'],
    'scale': ['1:1'],
    'revision': ['版本'],
}

# A3 图幅下的标题栏关键坐标（作为兜底匹配）
A3_COORDS = {
    'material': (336.2, 60.0),
    'title': (385.5, 41.3),
    'number': (385.5, 26.5),
    'scale': (354.0, 35.4),
    'weight': (341.9, 35.4),
}


def emit(obj):
    sys.stdout.write('###DSH_CAD###' + json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def connect_acad(visible=False, timeout=120, exe_hint=None):
    """连到 AutoCAD；没有运行就启动一个（默认隐藏，不打扰前端）。

    exe_hint：面板里配的 acad.exe 路径；没在跑就按这个路径拉起来。
    """
    import win32com.client as win32

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
            break
        except Exception:
            acad = None
            time.sleep(2)
    if acad is None:
        raise RuntimeError('连不上 AutoCAD。请确认 AutoCAD 已安装且能正常启动。')
    return acad


def quit_acad_if_idle(acad, timeout=15):
    """改完标题栏后把 AutoCAD 收掉，避免进程越积越多。

    实测问题：每次出图都用 /Automation -Embedding 拉起一个 acad.exe，
    之前只 Close 文档不退出进程，跑几次就挂一堆 477MB 的进程。

    做法：先关掉所有文档，再 Quit()。加超时兜底，避免 AutoCAD 弹
    "是否保存"之类的模态框把我们卡死。
    """
    import threading

    def _quit():
        try:
            for _ in range(20):
                docs = acad.Documents
                if docs.Count == 0:
                    break
                try:
                    docs.Item(0).Close(False)
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
    th.join(timeout)
    return not th.is_alive()


def collect_texts(doc):
    """收集模型空间里所有文字对象：[(obj, x, y, text)]"""
    out = []
    ms = doc.ModelSpace
    for i in range(ms.Count):
        try:
            o = ms.Item(i)
            if o.EntityName not in ('AcDbText', 'AcDbMText'):
                continue
            p = o.InsertionPoint
            out.append((o, float(p[0]), float(p[1]), str(o.TextString)))
        except Exception:
            continue
    return out


def match_field(text, x, y, paper_scale=1.0):
    """判断这段文字对应哪个标题栏字段。先按占位符原文，再按坐标兜底。"""
    t = text.strip()

    for field, hints in PLACEHOLDER_HINTS.items():
        for h in hints:
            if t == h:
                return field

    # 坐标兜底（按图幅缩放）
    for field, (cx, cy) in A3_COORDS.items():
        if abs(x - cx * paper_scale) < 1.0 and abs(y - cy * paper_scale) < 1.0:
            return field
    return None


def edit_title_block(dwg_path, fields, out_path=None, visible=False, keep_open=False,
                     exe_hint=None):
    """改写 DWG 标题栏。

    fields: {'material': '6061-T6', 'title': 'xxx', 'number': 'DW-001', ...}
    keep_open: False = 改完退出 AutoCAD（默认，避免进程堆积）
    exe_hint: 面板里配的 acad.exe 路径
    返回 {ok, changed: [{field, old, new}], skipped: [field], out, quit}
    """
    acad = connect_acad(visible, exe_hint=exe_hint)
    doc = acad.Documents.Open(os.path.abspath(dwg_path))
    quit_ok = None
    try:
        texts = collect_texts(doc)
        emit({'stage': 'acad_opened', 'textCount': len(texts)})

        # 一张图幅可能只有一个标题栏，先做「字段 -> 文字对象」映射
        assigned = {}
        for obj, x, y, t in texts:
            f = match_field(t, x, y)
            if f and f not in assigned and f in fields and fields[f] is not None:
                assigned[f] = (obj, t)

        changed = []
        skipped = []
        for field, value in fields.items():
            if value is None or str(value).strip() == '':
                continue
            if field not in assigned:
                skipped.append(field)
                continue
            obj, old = assigned[field]
            try:
                obj.TextString = str(value)
                changed.append({'field': field, 'old': old, 'new': str(value)})
            except Exception as exc:
                skipped.append(field)
                emit({'stage': 'write_error', 'field': field, 'error': str(exc)})

        target = os.path.abspath(out_path or dwg_path)
        if os.path.abspath(dwg_path).lower() == target.lower():
            doc.Save()
        else:
            doc.SaveAs(target)

        try:
            doc.Close(False)
        except Exception:
            pass

        return {'ok': True, 'changed': changed, 'skipped': skipped, 'out': target}
    finally:
        try:
            doc.Close(False)
        except Exception:
            pass
        if not keep_open:
            quit_ok = quit_acad_if_idle(acad)
            emit({'stage': 'acad_quit', 'ok': quit_ok})


def list_title_block(dwg_path, visible=False):
    """列出 DWG 里所有文字及其坐标，方便后续自定义模板对齐。"""
    acad = connect_acad(visible)
    doc = acad.Documents.Open(os.path.abspath(dwg_path))
    try:
        texts = collect_texts(doc)
        return {
            'ok': True,
            'count': len(texts),
            'texts': [{'x': round(x, 1), 'y': round(y, 1), 'text': t}
                      for _o, x, y, t in texts],
        }
    finally:
        try:
            doc.Close(False)
        except Exception:
            pass


def run(payload):
    action = payload.get('action', 'edit_title_block')
    if action == 'edit_title_block':
        return edit_title_block(
            payload['dwg'],
            payload.get('fields') or {},
            payload.get('out'),
            payload.get('visible', False))
    if action == 'list_title_block':
        return list_title_block(payload['dwg'], payload.get('visible', False))
    return {'ok': False, 'error': f'未知 action: {action}'}


def main():
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
        result = {
            'ok': False,
            'error': str(exc),
            'errorType': type(exc).__name__,
            'trace': traceback.format_exc()[-2500:],
        }
    emit({'stage': 'done', **result})
    sys.exit(0 if result.get('ok') else 1)


if __name__ == '__main__':
    main()
