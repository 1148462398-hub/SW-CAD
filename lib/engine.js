/**
 * dsh-cad-draw / engine.js
 *
 * 把 Python 引擎（lib/engine.py）包成 Node 侧可调用的函数。
 * Python 用本机已装的 Python 3.11 + pywin32 驱动 SolidWorks COM。
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
export const ENGINE_PATH = path.join(HERE, 'engine.py')

/** 本机 Python 候选（按顺序探测）。换电脑时这里尽量多铺一些。 */
const PYTHON_CANDIDATES = [
  process.env.DSH_CAD_PYTHON,
  path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python311', 'python.exe'),
  path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe'),
  path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python310', 'python.exe'),
  'C:\\Python311\\python.exe',
  'C:\\Python312\\python.exe',
  'C:\\Python310\\python.exe',
  path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Python311', 'python.exe'),
  path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Python312', 'python.exe')
].filter(Boolean)

/** 扫 %LOCALAPPDATA%\Programs\Python\Python3*，兼容 3.13 之类的小版本 */
function scanLocalPython() {
  const base = path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python')
  try {
    for (const name of readdirSync(base)) {
      if (!/^Python3\d+$/i.test(name)) continue
      const exe = path.join(base, name, 'python.exe')
      if (existsSync(exe)) return exe
    }
  } catch {
    /* 目录不存在就算了 */
  }
  return ''
}

export function resolvePython() {
  for (const cand of PYTHON_CANDIDATES) {
    if (existsSync(cand)) return cand
  }
  const scanned = scanLocalPython()
  if (scanned) return scanned
  /* 最后交给 PATH 上的 python（用户可能装了但不在常见目录） */
  return 'python'
}

/**
 * 进度跟踪
 *
 * `/api/build` 是「跑完才返回」的，面板拿不到中途状态，
 * 于是引擎每吐一个 ###DSH_CAD### 阶段就更新这个内存对象，
 * 面板通过 GET /api/progress 轮询，用来画进度条。
 */
const STAGE_PERCENT = {
  start: 2,
  connected: 6,
  swept: 8,
  model_opened: 12,
  model_type: 14,
  neutral_import: 18,
  annotation_standard: 20,
  technical_requirements: 21,
  layout: 24,
  drawing_created: 28,
  views: 34,
  views_positioned: 38,
  hidden_lines: 40,
  view_boxes: 42,
  cavity_check: 45,
  section_view: 48,
  detail_view: 50,
  annotated: 54,
  deduped: 56,
  dimensions_classified: 58,
  dimensions_pruned: 60,
  dimension_arranged: 62,
  overflow_check: 64,
  tolerances: 66,
  surface_finish: 67,
  title_block: 68,
  saved: 72,
  acad_scale_factor: 74,
  acad_style: 75,
  acad_views: 77,
  acad_merge: 79,
  acad_layers: 81,
  acad_layout_by_layer: 84,
  acad_text_height: 85,
  acad_dimensioned: 90,
  acad_dimension: 92,
  acad_title_block: 94,
  preview: 96,
  cleanup: 98,
  done: 100,
  /* ── 整套图纸（drawset）──
     这些阶段的百分比不是固定的：drawset_begin / drawset_sheet_done
     会带 sheet/total，由 markStage 按「第几张/共几张」动态算，
     把 6%~90% 这段留给「装配总图 + 每张零件图」。 */
  drawset_collect: 3,
  drawset_plan: 5,
  drawset_begin: 6,
  drawset_sheet_done: 8,
  drawset_merge_start: 92,
  drawset_merge_done: 96
}

const STAGE_LABEL = {
  start: '准备任务',
  connected: '连接 SolidWorks',
  swept: '清理残留文档',
  model_opened: '打开模型',
  model_type: '识别模型类型',
  neutral_import: '导入 STEP/IGES',
  annotation_standard: '加载标注标准',
  technical_requirements: '写入技术要求',
  layout: '计算比例与布局',
  drawing_created: '创建工程图',
  views: '投影视图',
  views_positioned: '摆放视图',
  hidden_lines: '显示隐藏线',
  view_boxes: '记录视图边界',
  cavity_check: '判断剖视',
  section_view: '生成剖视图',
  detail_view: '局部放大图',
  annotated: '插入模型尺寸',
  deduped: '去重尺寸',
  dimensions_classified: '分类尺寸',
  dimensions_pruned: '精简尺寸',
  dimension_arranged: '排列尺寸',
  overflow_check: '检查图框溢出',
  tolerances: '标注公差',
  surface_finish: '标注粗糙度',
  title_block: '填写标题栏',
  saved: '保存并导出',
  acad_scale_factor: 'AutoCAD 比例换算',
  acad_style: '建立国标标注样式',
  acad_views: '识别视图',
  acad_merge: '合并碎线',
  acad_layers: '按视图分层',
  acad_layout_by_layer: '视图归位',
  acad_text_height: '确定字高',
  acad_dimensioned: 'AutoCAD 标注尺寸',
  acad_dimension: '标注完成',
  acad_title_block: 'AutoCAD 填标题栏',
  preview: '生成预览图',
  cleanup: '收尾关闭文档',
  done: '完成',
  drawset_collect: '解析装配体',
  drawset_plan: '规划图纸',
  drawset_begin: '开始出图',
  drawset_sheet_done: '本张完成',
  drawset_merge_start: '合并图纸',
  drawset_merge_done: '合并完成'
}

let currentProgress = {
  active: false,
  percent: 0,
  stage: '',
  label: '',
  startedAt: 0,
  updatedAt: 0,
  error: ''
}

/** 面板轮询用的进度快照 */
export function getProgress() {
  return { ...currentProgress }
}

function resetProgress() {
  currentProgress = {
    active: true,
    percent: 1,
    stage: 'start',
    label: STAGE_LABEL.start,
    startedAt: Date.now(),
    updatedAt: Date.now(),
    error: ''
  }
}

function markStage(stage, detail) {
  const d = detail || {}
  currentProgress.updatedAt = Date.now()
  currentProgress.stage = stage

  // 整套图纸：按「第几条/共几条」动态算百分比，
  // 6%~90% 这段留给装配总图 + 每张零件图
  if (stage === 'drawset_begin' || stage === 'drawset_sheet_done') {
    const total = Number(d.total) || 0
    const sheet = Number(d.sheet) || 0
    if (total > 0 && sheet > 0) {
      const done = stage === 'drawset_sheet_done' ? sheet : sheet - 1
      const pct = 6 + Math.round((done / total) * 84)
      currentProgress.percent = Math.max(currentProgress.percent, pct)
      const kind = d.kind === 'assembly' ? '装配总图' : '零件图'
      const nm = d.name ? String(d.name) : ''
      currentProgress.label =
        stage === 'drawset_sheet_done'
          ? `已完成 ${sheet}/${total}：${kind}${nm ? ' · ' + nm : ''}`
          : `正在出第 ${sheet}/${total} 张：${kind}${nm ? ' · ' + nm : ''}`
      currentProgress.sheet = sheet
      currentProgress.totalSheets = total
      return
    }
  }

  const pct = STAGE_PERCENT[stage]
  if (pct === undefined) return
  // 只前进不后退（有些阶段会重复出现，比如 overflow_check 重试）
  currentProgress.percent = Math.max(currentProgress.percent, pct)
  currentProgress.label = STAGE_LABEL[stage] || stage
  if (stage === 'done') currentProgress.active = false
}

/**
 * 跑一次引擎。
 * @param {object} payload 传给 engine.py 的任务（JSON）
 * @param {{timeoutMs?: number, signal?: AbortSignal}} opts
 * @returns {Promise<{result: object|null, stages: object[], raw: string, stderr: string, code: number}>}
 */
export function runEngine(payload, opts = {}) {
  const timeoutMs = opts.timeoutMs ?? 15 * 60 * 1000
  const python = resolvePython()

  resetProgress()

  // 任务写进临时 JSON 文件再传给 Python：
  // Windows 下用管道喂中文路径/反斜杠容易被编码破坏，文件方式最稳。
  const tmpDir = mkdtempSync(path.join(os.tmpdir(), 'dsh-cad-'))
  const payloadPath = path.join(tmpDir, 'payload.json')
  writeFileSync(payloadPath, JSON.stringify(payload), 'utf8')

  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-u', ENGINE_PATH, payloadPath], {
      cwd: path.dirname(ENGINE_PATH),
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
    })

    let stdout = ''
    let stderr = ''
    let settled = false
    const stages = []

    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      try {
        child.kill()
      } catch {
        /* 忽略 */
      }
      reject(new Error(`CAD 引擎超时（${Math.round(timeoutMs / 1000)}s）。SolidWorks 可能卡在对话框上。`))
    }, timeoutMs)

    const onAbort = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      try {
        child.kill()
      } catch {
        /* 忽略 */
      }
      reject(new Error('CAD 引擎已取消'))
    }
    opts.signal?.addEventListener('abort', onAbort, { once: true })

    child.stdout.setEncoding('utf8')
    let pending = ''
    child.stdout.on('data', (chunk) => {
      stdout += chunk
      // 按行切；未闭合的尾巴留在 pending，避免同一行被重复解析
      pending += chunk
      const lines = pending.split('\n')
      pending = lines.pop() ?? ''
      for (const line of lines) {
        const idx = line.indexOf('###DSH_CAD###')
        if (idx < 0) continue
        try {
          const st = JSON.parse(line.slice(idx + '###DSH_CAD###'.length))
          stages.push(st)
          // 同步推进进度条（面板靠 GET /api/progress 轮询这个值）
          if (st && st.stage) markStage(st.stage, st)
        } catch {
          /* 跳过坏行 */
        }
      }
    })

    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => {
      stderr += chunk
    })

    child.on('error', (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      opts.signal?.removeEventListener('abort', onAbort)
      cleanup()
      currentProgress.active = false
      currentProgress.error = error.message
      reject(new Error(`无法启动 Python（${python}）：${error.message}`))
    })

    child.on('close', (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      opts.signal?.removeEventListener('abort', onAbort)
      cleanup()

      // 取最后一个 done 阶段作为结果
      let result = null
      for (const st of stages) {
        if (st.stage === 'done') result = st
      }
      // 收尾：正常完成给 100%，异常结束就别停在半路让人以为还在跑
      if (result && result.ok) {
        currentProgress.percent = 100
        currentProgress.stage = 'done'
        currentProgress.label = STAGE_LABEL.done
      } else {
        currentProgress.error = result?.error || `引擎异常退出（exit=${code}）`
      }
      currentProgress.active = false
      currentProgress.updatedAt = Date.now()
      resolve({ result, stages, raw: stdout, stderr, code })
    })

    function cleanup() {
      try {
        rmSync(tmpDir, { recursive: true, force: true })
      } catch {
        /* 忽略 */
      }
    }
  })
}

/** 环境探测
 * hints: {solidworksExe?, autocadExe?} —— 面板/配置里填的启动路径，留空则自动探测
 */
export async function probe(hints = {}, opts = {}) {
  const payload = { action: 'probe' }
  if (hints?.solidworksExe) payload.solidworksExe = String(hints.solidworksExe)
  if (hints?.autocadExe) payload.autocadExe = String(hints.autocadExe)
  const { result, stderr, code } = await runEngine(payload, { timeoutMs: 120_000, ...opts })
  if (!result) {
    throw new Error(`探测失败（exit=${code}）${stderr ? '：' + stderr.slice(-800) : ''}`)
  }
  return result
}
