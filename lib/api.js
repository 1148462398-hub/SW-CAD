/**
 * dsh-cad-draw / api.js
 *
 * 在宿主自带的 HTTP 端口上挂 /dsh-cad-draw/api/*，
 * 让浏览器侧的拖拽面板能真正触发出图。
 *
 * 路由：
 *   POST /dsh-cad-draw/api/build   出图（body 为 build payload）
 *   GET  /dsh-cad-draw/api/probe   环境探测
 *
 * 注意：webServer 可能在插件 apply 之后才就绪，所以要轮询等待。
 */

import { probe, runEngine } from './engine.js'

export const API_PREFIX = '/dsh-cad-draw'

const MAX_BODY = 1 << 20 // 1MB 足够

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
    overflow: (s) => `溢出重试 #${s.attempt}：${JSON.stringify(s.detail)}`
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
        const info = await probe()
        return sendJson(res, 200, { ok: true, info })
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
