/**
 * dsh-cad-draw / engine.js
 *
 * 把 Python 引擎（lib/engine.py）包成 Node 侧可调用的函数。
 * Python 用本机已装的 Python 3.11 + pywin32 驱动 SolidWorks COM。
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
export const ENGINE_PATH = path.join(HERE, 'engine.py')

/** 本机 Python 候选（按顺序探测） */
const PYTHON_CANDIDATES = [
  process.env.DSH_CAD_PYTHON,
  path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python311', 'python.exe'),
  'C:\\Python311\\python.exe',
  'python'
].filter(Boolean)

export function resolvePython() {
  for (const cand of PYTHON_CANDIDATES) {
    if (cand === 'python') return cand
    if (existsSync(cand)) return cand
  }
  return 'python'
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
          stages.push(JSON.parse(line.slice(idx + '###DSH_CAD###'.length)))
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
