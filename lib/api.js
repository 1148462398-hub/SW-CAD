/**
 * dsh-cad-draw / api.js
 *
 * 在宿主自带的 HTTP 端口上挂 /dsh-cad-draw/api/*，
 * 让浏览器侧的拖拽面板能真正触发出图。
 *
 * 路由：
 *   POST /dsh-cad-draw/api/upload  上传模型文件（浏览器拿不到本地路径，只能传字节）
 *   POST /dsh-cad-draw/api/build   出图（body 为 build payload）
 *   GET  /dsh-cad-draw/api/probe   环境探测
 *
 * 注意：webServer 可能在插件 apply 之后才就绪，所以要轮询等待。
 */

import { appendFileSync, createWriteStream, existsSync, mkdirSync, statSync } from 'node:fs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import os from 'node:os'
import { probe, runEngine, getProgress } from './engine.js'

export const API_PREFIX = '/dsh-cad-draw'

const MAX_BODY = 1 << 20 // 1MB：普通 JSON payload 足够
const MAX_UPLOAD = 512 << 20 // 512MB：SolidWorks 模型可能很大

/** 上传文件落盘目录：Documents\dsh-cad-draw\uploads（用户找得到） */
function uploadDir() {
  const base = path.join(os.homedir(), 'Documents', 'dsh-cad-draw', 'uploads')
  mkdirSync(base, { recursive: true })
  return base
}

/** 同名的加时间戳，避免覆盖用户上一次拖进来的文件 */
function uniquePath(dir, name) {
  const safe = String(name || 'model.sldprt').replace(/[\\/:*?"<>|]/g, '_')
  const ext = path.extname(safe)
  const stem = path.basename(safe, ext)
  let target = path.join(dir, safe)
  if (!existsSync(target)) return target
  const stamp = new Date()
    .toISOString()
    .replace(/[-:T]/g, '')
    .slice(0, 14)
  target = path.join(dir, `${stem}_${stamp}${ext}`)
  let n = 1
  while (existsSync(target)) target = path.join(dir, `${stem}_${stamp}_${n++}${ext}`)
  return target
}

/**
 * 弹出 Windows 原生「选择文件夹」对话框（资源管理器那种）。
 *
 * 用户要求：「出图的保存路径不要自己填，直接按资源管理器那种方式选择」。
 *
 * 实现：宿主进程 spawn 一个 PowerShell，用 System.Windows.Forms 的
 * FolderBrowserDialog。要点：
 *   1. 必须给它一个 TopMost 的 owner 窗口，否则对话框会被浏览器盖住、
 *      看起来像卡死（用户点了没反应）。
 *   2. 必须在 STA 线程里跑（PowerShell 默认就是 STA）。
 *   3. stdout 只留最终路径，其它杂音一律丢弃。
 *
 * 返回 { ok, path }；用户点取消则 path 为空。
 */
function pickFolder(initial) {
  const ps = [
    '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8',
    'Add-Type -AssemblyName System.Windows.Forms | Out-Null',
    '$owner = New-Object System.Windows.Forms.Form',
    '$owner.TopMost = $true',
    '$owner.ShowInTaskbar = $false',
    '$owner.WindowState = "Minimized"',
    '$dlg = New-Object System.Windows.Forms.FolderBrowserDialog',
    '$dlg.Description = "选择工程图保存目录"',
    '$dlg.ShowNewFolderButton = $true',
    initial
      ? '$dlg.SelectedPath = ' + JSON.stringify(initial)
      : '',
    '$owner.Activate() | Out-Null',
    'if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {',
    '  [Console]::Out.Write($dlg.SelectedPath)',
    '}',
    '$owner.Dispose()'
  ].filter(Boolean).join('\n')

  return runDialog(ps)
}

/** 跑一个 PowerShell 对话框脚本，返回 { ok, path, canceled } */
function runDialog(ps) {
  return new Promise((resolve) => {
    /* ⚠ 必须用 -EncodedCommand（UTF-16LE + Base64），不能用 -Command。
       -Command 按控制台代码页解码参数，脚本里的中文（对话框标题、文件筛选器）
       会被解成乱码，PowerShell 直接报
         「表达式或语句中包含意外的标记"鍙墽琛岀▼搴弢"」
         「字符串缺少终止符」
       实测踩过。EncodedCommand 是 UTF-16LE，与代码页无关，稳。 */
    const encoded = Buffer.from(ps, 'utf16le').toString('base64')
    const child = spawn(
      'powershell.exe',
      ['-NoProfile', '-STA', '-WindowStyle', 'Hidden',
       '-EncodedCommand', encoded],
      { windowsHide: true }
    )
    let out = ''
    let err = ''
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* 忽略 */
      }
      resolve({ ok: false, error: '对话框超时（120 秒）' })
    }, 120000)

    child.stdout.on('data', (b) => {
      out += b.toString('utf8')
    })
    child.stderr.on('data', (b) => {
      err += b.toString('utf8')
    })
    child.on('error', (e) => {
      clearTimeout(timer)
      resolve({ ok: false, error: '无法弹出对话框：' + e.message })
    })
    child.on('close', () => {
      clearTimeout(timer)
      const p = out.trim()
      if (!p) {
        return resolve({ ok: true, path: '', canceled: true })
      }
      resolve({ ok: true, path: p })
    })
  })
}

/**
 * 弹出 Windows 原生「选择模型文件」对话框。
 *
 * ⚠ 这个功能是**装配体的唯一正确用法**：
 *   浏览器拖拽只能拿到文件字节，落到 uploads 目录的是一个**副本**。
 *   .SLDASM 依赖同目录下的 .SLDPRT 零件，副本脱离了原目录就找不到引用，
 *   SolidWorks 打开后装配体是空的（实测：21 个实例只剩 2 行明细表、
 *   视图层全空、标注 0 个）。
 *   所以装配体必须用「原始路径」出图 —— 就是让用户直接选文件。
 */
function pickFile(initial, kind) {
  const filters = {
    model:
      '"SolidWorks 模型|*.sldprt;*.sldasm|零件|*.sldprt|装配体|*.sldasm|STEP|*.step;*.stp|IGES|*.igs;*.iges|所有文件|*.*"',
    exe: '"可执行程序|*.exe|所有文件|*.*"',
    all: '"所有文件|*.*"'
  }
  const titles = {
    model: '选择 SolidWorks 模型（装配体必须选原始文件）',
    exe: '选择软件启动程序（SLDWORKS.exe / acad.exe）',
    all: '选择文件'
  }
  const k = filters[kind] ? kind : 'model'
  const ps = [
    '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8',
    'Add-Type -AssemblyName System.Windows.Forms | Out-Null',
    '$owner = New-Object System.Windows.Forms.Form',
    '$owner.TopMost = $true',
    '$owner.ShowInTaskbar = $false',
    '$owner.WindowState = "Minimized"',
    '$dlg = New-Object System.Windows.Forms.OpenFileDialog',
    '$dlg.Title = ' + JSON.stringify(titles[k]),
    '$dlg.Filter = ' + filters[k],
    '$dlg.Multiselect = $false',
    initial
      ? '$dlg.InitialDirectory = ' + JSON.stringify(initial)
      : '',
    '$owner.Activate() | Out-Null',
    'if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {',
    '  [Console]::Out.Write($dlg.FileName)',
    '}',
    '$owner.Dispose()'
  ].filter(Boolean).join('\n')

  return runDialog(ps)
}

function sendJson(res, status, data) {
  try {
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store'
    })
    res.end(body)
  } catch {
    /* 连接可能已断 */
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY) {
        reject(new Error('请求体过大'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8')
      if (!raw.trim()) return resolve({})
      try {
        resolve(JSON.parse(raw))
      } catch (error) {
        reject(new Error('请求体不是合法 JSON：' + error.message))
      }
    })
    req.on('error', reject)
  })
}

/** 把引擎的阶段上报拼成可读日志（同名阶段只留最后一次） */
function stagesToLog(stages, result) {
  const lines = []
  const seen = new Map()
  const label = {
    connected: (s) => `已连接 SolidWorks ${s.revision ?? ''}`,
    swept: (s) => `清理残留图纸 ${s.closed} 个`,
    model_opened: () => '模型已打开',
    model_type: (s) => {
      const mark = s.kind === 'imported' ? '⚠ ' : ''
      return mark + String(s.note ?? '')
    },
    layout: (s) => `模型 ${(s.modelSizeMm || []).join('×')}mm，比例 ${s.scale ?? ''}`,
    drawing_created: (s) => `工程图已创建（${s.paper} ${s.landscape ? '横向' : '纵向'}，比例 ${s.scale}）`,
    views: (s) => '视图：' + Object.entries(s.result || {})
      .filter(([k]) => !k.startsWith('_'))
      .map(([k, v]) => `${k}=${v ? '✓' : '✗'}`).join(' '),
    section_view: (s) => s.ok
      ? `剖视图：已加${s.reason ? '（' + s.reason + '）' : ''}`
      : `剖视图：未加${s.reason ? '（' + s.reason + '）' : ''}`,
    detail_view: (s) => `局部放大图：${s.ok ? '已加' : '未加'}`,
    annotated: (s) => `插入标注 ${s.objects} 个，尺寸 ${s.dimensions} 个`,
    deduped: (s) => `去重：删掉 ${s.removed} 个重复标注，保留 ${s.kept}`,
    dimension_arranged: () => '尺寸已分层排列',
    overflow_check: (s) => s.ok ? '图框溢出检查：通过' : `图框溢出：${JSON.stringify(s.detail)}`,
    tolerances: (s) => `公差已加 ${s.applied} 处`,
    surface_finish: (s) => `粗糙度符号：${s.ok ? '已加' : '未加'}`,
    title_block: () => '标题栏属性已写入',
    annotated_skipped: (s) => `标注：交给 AutoCAD 做（${s.reason || ''}）`,
    acad_dim_dedup: (s) => `AutoCAD 直径去重：${s.groupsTotal} 组 → 保留 ${s.kept} 处`,
    acad_lineweight: (s) => `AutoCAD 线宽归一：改了 ${s.changed} 条（粗 ${s.thick} / 细 ${s.thin}）`,
    saved: (s) => `已保存：SLDDRW ${s.drw ? '✓' : '✗'}｜DXF ${s.dxf ? '✓' : '✗'}｜DWG ${s.dwg ? '✓' : '✗'}`,
    acad_title_block: (s) => s.ok
      ? `AutoCAD 标题栏：已填 ${(s.changed || []).map((c) => c.field).join('、') || '无'}`
      : `AutoCAD 标题栏：失败 ${s.error ?? ''}`,
    preview: () => '预览图已生成',
    dims_pruned_note: () => '',
    dimensions_pruned: (s) => `尺寸筛选：删掉 ${s.removed} 个冗余尺寸，保留 ${s.kept}`,
    prune_error: (s) => `尺寸筛选失败：${s.error ?? ''}`,
    cleanup: (s) => `收尾：关闭图纸 ${s.closed} 个`,
    acad_quit: (s) => `AutoCAD 已${s.ok ? '退出' : '仍在后台'}`,
    overflow: (s) => `溢出重试 #${s.attempt}：${JSON.stringify(s.detail)}`,

    /* ── 装配体 ── */
    assembly: (s) => `装配体：${s.components} 个零件实例`,
    paper_escalated: (s) => `图幅自动升级：${s.from} → ${s.to}（${s.note}）`,
    bom: (s) => s.ok
      ? `明细表：已插入 ${s.rows ?? '?'} 行 @ ${JSON.stringify(s.atMm ?? '')}`
      : `明细表：失败 ${s.error ?? ''}`,
    bom_reserved: (s) => s.error
      ? `明细表避让框计算失败：${s.error}`
      : `明细表占位（视图会避开）：${JSON.stringify(s.rectMm ?? [])}`,
    balloons: (s) => s.ok
      ? `零件序号：生成 ${s.created ?? '?'} 个（选中 ${s.selected ?? '?'} 个视图）`
      : `零件序号：失败 ${s.error ?? ''}`,

    /* ── AutoCAD 后处理（用户要能看见这一段！）── */
    acad_style: (s) => `AutoCAD 标注样式：${s.style ?? 'GB'}，设了 ${s.vars ?? '?'} 个变量`,
    acad_views: (s) => `AutoCAD 识别视图：${s.found ?? 0} 个`,
    acad_merge: (s) => `AutoCAD 合并线段：${s.merged ?? 0} 条（${s.views ?? 0} 个视图）`,
    acad_layers: (s) => {
      const c = s.counts || {}
      return 'AutoCAD 分图层：' +
        Object.entries(c).map(([k, v]) => `${k.replace('DSH_', '')}=${v}`).join(' ')
    },
    acad_layout_by_layer: (s) => {
      if (s.error) return `AutoCAD 布局失败：${s.error}`
      const p = s.placed || {}
      return 'AutoCAD 摆视图：' +
        Object.entries(p).map(([k, v]) =>
          `${k.replace('DSH_VIEW_', '视图')}(${v.target})`).join(' ')
    },
    acad_centerlines: (s) => s.ok
      ? `AutoCAD 中心线：${s.centers} 个圆心，画了 ${s.lines} 条点画线`
      : `AutoCAD 中心线：失败 ${s.error ?? ''}`,
    acad_text_height: (s) => `AutoCAD 字高：${s.height}mm（${s.basis ?? ''}）`,
    acad_text_height_fixed: (s) => `AutoCAD 修正尺寸字高：${s.count} 个 → ${s.height}mm`,
    acad_dimensioned: (s) =>
      `AutoCAD 标注：外形 ${s.overall ?? 0}、直径 ${s.diameters ?? 0}、线性 ${s.linear ?? 0}`,
    acad_redxf: (s) => s.ok
      ? `AutoCAD 重新导出 DXF：✓ ${Math.round((s.size || 0) / 1024)} KB`
      : `AutoCAD 导出 DXF：失败 ${s.error ?? ''}`,
    acad_post_error: (s) => `AutoCAD 后处理出错：${s.error ?? ''}`,
    acad_closed: (s) => s.closed ? 'AutoCAD 文档已关闭' : `AutoCAD 关闭：${s.note ?? ''}`,

    /* ── 整套图纸 ── */
    drawset_collect: () => '整套图纸：解析装配体…',
    drawset_plan: (s) => `整套图纸：共 ${s.sheets} 张（总图 + ${s.parts?.length ?? 0} 个零件）`,
    drawset_begin: (s) => `整套图纸：开始第 ${s.sheet}/${s.total} 张 ${s.kind === 'assembly' ? '装配总图' : '零件图'} ${s.name ?? ''}`,
    drawset_sheet_done: (s) => `整套图纸：第 ${s.sheet}/${s.total} 张${s.ok ? '完成' : '失败'}`,
    drawset_merge_start: (s) => `整套图纸：合并 ${s.sheets} 张…`,
    drawset_merge_done: (s) => s.ok
      ? `整套图纸：已合并 ${s.sheets} 张 → 宽 ${s.widthMm}mm`
      : `整套图纸：合并失败 ${s.error ?? ''}`,
    drawset_merge_error: (s) => `整套图纸：合并失败 ${s.error ?? ''}`
  }
  for (const st of stages || []) {
    // 同名阶段只写一次（用最新一条的内容覆盖）
    const fn = label[st.stage]
    if (!fn) continue
    try {
      seen.set(st.stage, '· ' + fn(st))
    } catch {
      /* 忽略格式错误 */
    }
  }
  for (const line of seen.values()) lines.push(line)
  if (result && !result.ok && result.error) {
    lines.push('· 失败：' + result.error)
  }
  return lines.join('\n')
}

export function registerCadApi(ctx, { log = () => {} } = {}) {
  /* 诊断日志：记「进入 → 命中哪条 → 卡在哪个 await → 有没有回包」。
     2026-11-01 排查：日志证明请求进了处理器，但界面一直挂着，
     所以必须细分到每一步才能定位。 */
  const dbgPath = path.join(os.tmpdir(), 'dsh-cad-draw-api.log')
  const dbg = (msg) => {
    try {
      appendFileSync(dbgPath, `${new Date().toISOString()} ${msg}\n`)
    } catch {
      /* 忽略 */
    }
  }

  const handle = async (req, res) => {
    const t0 = Date.now()
    dbg(`ENTER ${req.method} ${req.url}`)

    /* 看门狗：25 秒还没回包就强制回一个 504。
       这样界面至少能拿到明确错误，而不是无限转圈。 */
    const watchdog = setTimeout(() => {
      try {
        if (!res.headersSent) {
          sendJson(res, 504, {
            ok: false,
            error: '接口 25 秒未返回（已超时）。请查看 %TEMP%\\dsh-cad-draw-api.log'
          })
          dbg(`WATCHDOG 强制回 504 ${req.method} ${req.url}`)
        }
      } catch (e) {
        dbg(`WATCHDOG 失败 ${e?.message ?? e}`)
      }
    }, 25000)

    const url = new URL(String(req.url ?? '/'), 'http://127.0.0.1')
    const route = url.pathname.slice(API_PREFIX.length) || '/'

    try {
      if (req.method === 'GET' && route === '/api/probe') {
        dbg(`  probe: 命中，开始探测`)
        const info = await probe({
          solidworksExe: url.searchParams.get('solidworksExe') || '',
          autocadExe: url.searchParams.get('autocadExe') || ''
        })
        dbg(`  probe: 探测完成 ${Date.now() - t0}ms，回包`)
        return sendJson(res, 200, { ok: true, info })
      }

      /* 出图进度：面板每 ~800ms 轮询一次画进度条。
         /api/build 是跑完才返回的，没有这个接口用户只能干等。 */
      if (req.method === 'GET' && route === '/api/progress') {
        return sendJson(res, 200, { ok: true, progress: getProgress() })
      }

      /* 上传模型：浏览器里 File 对象**没有本地路径**（file.path 是 Electron 才有的），
         所以必须把字节传到后端落盘，再把绝对路径交给出图引擎。 */
      // 原生「选择模型文件」对话框：装配体必须走这个（保住零件引用）
      if (req.method === 'POST' && route === '/api/pick-file') {
        let body = {}
        try {
          body = await readBody(req)
        } catch {
          /* 允许空 body */
        }
        log('info', `正在弹出选择文件对话框（kind=${body?.kind || 'model'}）…`)
        const picked = await pickFile(body?.initial || '', body?.kind || 'model')
        if (!picked.ok) {
          return sendJson(res, 500, { ok: false, error: picked.error })
        }
        return sendJson(res, 200, {
          ok: true,
          path: picked.path,
          canceled: Boolean(picked.canceled)
        })
      }

      // 原生「选择文件夹」对话框：面板上「浏览…」按钮调它
      if (req.method === 'POST' && route === '/api/pick-folder') {
        let body = {}
        try {
          body = await readBody(req)
        } catch {
          /* 允许空 body */
        }
        log('info', '正在弹出选择目录对话框…')
        const picked = await pickFolder(body?.initial || '')
        if (!picked.ok) {
          return sendJson(res, 500, { ok: false, error: picked.error })
        }
        return sendJson(res, 200, {
          ok: true,
          path: picked.path,
          canceled: Boolean(picked.canceled)
        })
      }

      if (req.method === 'POST' && route === '/api/upload') {
        const rawName = url.searchParams.get('name') || 'model.sldprt'
        const target = uniquePath(uploadDir(), rawName)
        await new Promise((resolve, reject) => {
          let size = 0
          const out = createWriteStream(target)
          req.on('data', (chunk) => {
            size += chunk.length
            if (size > MAX_UPLOAD) {
              out.destroy()
              reject(new Error(`文件过大（>${Math.round(MAX_UPLOAD / 1024 / 1024)}MB）`))
            }
          })
          req.on('error', reject)
          out.on('error', reject)
          out.on('finish', resolve)
          req.pipe(out)
        })
        let size = 0
        try {
          size = statSync(target).size
        } catch {
          /* 忽略 */
        }
        log('info', `模型已上传：${target}（${(size / 1024 / 1024).toFixed(1)}MB）`)
        return sendJson(res, 200, { ok: true, path: target, size })
      }

      if (req.method === 'POST' && route === '/api/build') {
        const payload = await readBody(req)
        if (!payload?.model) {
          return sendJson(res, 400, { ok: false, error: '缺少 model 参数' })
        }
        log('info', `CAD 出图请求：${payload.model}`)
        const { result, stages, stderr, code } = await runEngine(
          { action: 'build', ...payload },
          { timeoutMs: 20 * 60 * 1000 }
        )
        if (!result) {
          return sendJson(res, 500, {
            ok: false,
            log: `引擎未返回结果（exit=${code}）\n` + (stderr || '').slice(-1200)
          })
        }
        return sendJson(res, 200, {
          ok: Boolean(result.ok),
          log: stagesToLog(stages, result),
          result
        })
      }

      if (req.method === 'POST' && route === '/api/pdf') {
        const payload = await readBody(req)
        if (!payload?.dwg) {
          return sendJson(res, 400, { ok: false, error: '缺少 dwg 参数' })
        }
        log('info', `CAD 打印 PDF：${payload.dwg}`)
        const { result, stages, stderr, code } = await runEngine(
          { action: 'pdf', ...payload },
          { timeoutMs: 6 * 60 * 1000 }
        )
        if (!result) {
          return sendJson(res, 500, {
            ok: false,
            log: `打印引擎未返回结果（exit=${code}）\n` + (stderr || '').slice(-1200)
          })
        }
        const lines = []
        for (const st of stages) {
          if (st?.stage === 'pdf_log' && st.msg) lines.push(st.msg)
        }
        if (result.ok) {
          const mb = result.size ? (result.size / 1024).toFixed(1) : '?'
          const pg = Array.isArray(result.page_mm)
            ? `${result.page_mm[0]} × ${result.page_mm[1]} mm`
            : '未知'
          lines.push(`✅ PDF 已生成：${mb} KB，页面 ${pg}，耗时 ${result.seconds ?? '?'}s`)
          lines.push(`文件：${result.pdf}`)
        } else {
          lines.push('❌ 打印失败：' + (result.error ?? '未知错误'))
        }
        return sendJson(res, 200, {
          ok: Boolean(result.ok),
          log: lines.join('\n'),
          result
        })
      }

      dbg(`404 未知路由 ${route}`)
      return sendJson(res, 404, { ok: false, error: '未知路由：' + route })
    } catch (error) {
      dbg(`CATCH ${error?.name}: ${error?.message ?? error}`)
      log('warn', `CAD API 出错：${error?.message ?? error}`)
      return sendJson(res, 500, { ok: false, error: String(error?.message ?? error) })
    } finally {
      clearTimeout(watchdog)
      dbg(`EXIT ${req.method} ${route} ${Date.now() - t0}ms`)
    }
  }

  /* webServer 可能还没就绪，轮询等待 */
  let dispose = null
  let timer = null
  let attempts = 0

  /* ⚠ 用 DSH 官方的 ctx.inject(['webServer'], ...) 注册，不要用
     ctx.get('webServer') 轮询。

     实测（用户报「探测半天没结果 / 浏览 404」）：轮询版本在桌面 profile 下
     取不到 webServer，路由永远没挂上，请求被转发到宿主后**没有任何响应**，
     浏览器表现就是 net::ERR_UNEXPECTED / 请求一直挂着。
     而同一时刻插件工具（cad_draw_probe）完全正常 —— 说明宿主没事，只是路由没注册。

     DSH 自己的插件（client-modules / client-connection）都是这个写法：
       ctx.inject(['webServer'], (webCtx) => {
         webCtx.effect(() => webCtx.webServer.register({ kind:'prefix', path, handler }))
       })
     这样才保证「webServer 就绪后才注册」，且随插件卸载自动注销。 */
  const mount = (webCtx) => {
    const routes = [
      { path: API_PREFIX, label: '主路径' },
      // 兜底：DSH 自身在 /api 下挂了桥接路由，万一桌面端只转发 /api/*，
      // 这个别名能保证接口仍然可达。
      { path: '/api' + API_PREFIX, label: '别名 /api 前缀' }
    ]
    for (const r of routes) {
      try {
        webCtx.effect(
          () => webCtx.webServer.register({ kind: 'prefix', path: r.path, handler: handle }),
          `dsh-cad-draw: ${r.path}`
        )
        log('info', `CAD 出图 API 已挂载（${r.label}）：${r.path}/api/build`)
      } catch (error) {
        log('warn', `CAD API 挂载失败 ${r.path}：${error?.message ?? error}`)
      }
    }
  }

  try {
    if (typeof ctx.inject === 'function') {
      ctx.inject(['webServer'], mount)
      return () => {}
    }
  } catch (error) {
    log('warn', `ctx.inject 失败，退回轮询：${error?.message ?? error}`)
  }

  /* ── 老 profile 的兜底：ctx.get 轮询（上面 inject 不可用时才走）── */
  const getServer = () => {
    try {
      return ctx.get('webServer')
    } catch {
      return undefined
    }
  }

  const stopRetry = () => {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  const tryRegister = () => {
    if (dispose) return
    const server = getServer()
    if (!server || typeof server.register !== 'function') {
      attempts += 1
      if (attempts > 180) {
        log('warn', 'webServer 180 秒仍不可用，放弃注册 CAD 出图 API')
        stopRetry()
      }
      return
    }
    try {
      dispose = server.register({ kind: 'prefix', path: API_PREFIX, handler: handle })
      stopRetry()
      log('info', `CAD 出图 API 已挂载：${API_PREFIX}/api/build`)
    } catch (error) {
      log('warn', `CAD API 注册失败：${error?.message ?? error}`)
      stopRetry()
    }
  }

  tryRegister()
  if (!dispose) timer = setInterval(tryRegister, 1000)

  return () => {
    stopRetry()
    if (typeof dispose === 'function') {
      try {
        dispose()
      } catch {
        /* 忽略 */
      }
    }
    dispose = null
  }
}
