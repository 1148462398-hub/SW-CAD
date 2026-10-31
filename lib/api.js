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

import { createWriteStream, existsSync, mkdirSync, statSync } from 'node:fs'
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

function sendJson(res, status, data) {
  const body = JSON.stringify(data)
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
  const handle = async (req, res) => {
    const url = new URL(String(req.url ?? '/'), 'http://127.0.0.1')
    const route = url.pathname.slice(API_PREFIX.length) || '/'

    try {
      if (req.method === 'GET' && route === '/api/probe') {
        // 面板可带 ?solidworksExe=...&autocadExe=... 覆盖自动探测
        const info = await probe({
          solidworksExe: url.searchParams.get('solidworksExe') || '',
          autocadExe: url.searchParams.get('autocadExe') || ''
        })
        return sendJson(res, 200, { ok: true, info })
      }

      /* 出图进度：面板每 ~800ms 轮询一次画进度条。
         /api/build 是跑完才返回的，没有这个接口用户只能干等。 */
      if (req.method === 'GET' && route === '/api/progress') {
        return sendJson(res, 200, { ok: true, progress: getProgress() })
      }

      /* 上传模型：浏览器里 File 对象**没有本地路径**（file.path 是 Electron 才有的），
         所以必须把字节传到后端落盘，再把绝对路径交给出图引擎。 */
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

      return sendJson(res, 404, { ok: false, error: '未知路由：' + route })
    } catch (error) {
      log('warn', `CAD API 出错：${error?.message ?? error}`)
      return sendJson(res, 500, { ok: false, error: String(error?.message ?? error) })
    }
  }

  /* webServer 可能还没就绪，轮询等待 */
  let dispose = null
  let timer = null
  let attempts = 0

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
