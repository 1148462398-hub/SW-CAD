/**
 * dsh-cad-draw / methods.js
 *
 * 方法存档：把「验证过能出图的过程」沉淀成可复用的操作手册，
 * **存进插件目录**（随仓库一起发布 → 换设备也带着），
 * 出图时由插件把方法提示词注入给 AI。
 *
 * 为什么放在插件目录而不是用户目录：
 *   用户要求「方法存档内置到插件中，换设备也不影响」。
 *   插件目录会跟着 GitHub 仓库走，所以换电脑装插件就自带这套经验。
 *
 * 写入失败时（比如装在只读的 node_modules 里）自动回退到
 *   ~/.dsh/dsh-cad-draw-methods.json
 * 保证永远能存下来。
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
export const METHOD_FILE = path.join(HERE, 'methods.json')

/** 回退路径（插件目录不可写时用） */
function fallbackFile() {
  return path.join(os.homedir(), '.dsh', 'dsh-cad-draw-methods.json')
}

function readJson(file, dflt) {
  try {
    const raw = fs.readFileSync(file, 'utf8')
    const v = JSON.parse(raw)
    if (v && typeof v === 'object') return v
  } catch {
    /* 不存在或损坏 */
  }
  return dflt
}

/** 读全部方法 */
export function loadMethods() {
  const a = readJson(METHOD_FILE, null)
  if (a) return a
  const b = readJson(fallbackFile(), null)
  if (b) return b
  return { version: 1, methods: [] }
}

/** 写回；先试插件目录，失败回退用户目录 */
export function storeMethods(data) {
  const payload = JSON.stringify(data, null, 2) + '\n'
  const tries = [METHOD_FILE, fallbackFile()]
  let lastErr = null
  for (const file of tries) {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true })
      fs.writeFileSync(file, payload, 'utf8')
      return { ok: true, file }
    } catch (e) {
      lastErr = e
    }
  }
  return { ok: false, error: String(lastErr?.message ?? lastErr) }
}

function slug(s) {
  return String(s || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fa5]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40) || 'method-' + Date.now()
}

/**
 * 新增/更新一条方法。
 * m: { name, title, whenToUse, steps: string[]|string, notes, verified, source }
 *
 * ★ 更新规则（用户要求）：
 *   如果后来找到「更好/更快」的实现方式，**必须更新同一条方法**（同 name 覆盖），
 *   不能并列留两条。覆盖时会把上一版的关键信息压进 history，便于回溯。
 */
export function upsertMethod(m) {
  const data = loadMethods()
  const methods = Array.isArray(data.methods) ? data.methods : []
  const name = m.name || slug(m.title)
  const steps = Array.isArray(m.steps)
    ? m.steps
    : String(m.steps || '')
        .split('\n')
        .map((s) => s.replace(/^\s*(?:\d+[.)、]|[-*])\s*/, '').trim())
        .filter(Boolean)
  const now = new Date().toISOString().slice(0, 10)
  const idx = methods.findIndex((x) => x.name === name)
  const old = idx >= 0 ? methods[idx] : null
  const rec = {
    name,
    title: m.title || old?.title || name,
    whenToUse: m.whenToUse || old?.whenToUse || '',
    verified: m.verified !== false,
    verifiedAt: m.verifiedAt || now,
    updatedAt: now,
    version: (old?.version || 0) + 1,
    steps,
    notes: m.notes || '',
    improvement: m.improvement || '',
    tags: Array.isArray(m.tags) ? m.tags : (old?.tags || [])
  }
  if (old) {
    // 把上一版的关键信息压进 history（只留最近 5 版，避免文件膨胀）
    const hist = Array.isArray(old.history) ? old.history.slice() : []
    hist.push({
      version: old.version || 1,
      updatedAt: old.updatedAt || old.verifiedAt || '',
      steps: (old.steps || []).length,
      firstStep: (old.steps || [])[0] || '',
      notes: String(old.notes || '').slice(0, 300),
      improvement: old.improvement || ''
    })
    rec.history = hist.slice(-5)
    methods[idx] = rec
  } else {
    methods.push(rec)
  }
  data.version = 1
  data.methods = methods
  const w = storeMethods(data)
  return { ...w, method: rec, total: methods.length, updated: Boolean(old) }
}

export function deleteMethod(name) {
  const data = loadMethods()
  const before = Array.isArray(data.methods) ? data.methods.length : 0
  data.methods = (data.methods || []).filter((x) => x.name !== name)
  const w = storeMethods(data)
  return { ...w, removed: before - data.methods.length, total: data.methods.length }
}

export function getMethod(name) {
  const data = loadMethods()
  return (data.methods || []).find((x) => x.name === name) || null
}

/** 渲染成给 AI 看的提示词 */
export function renderMethods({ maxChars = 6000, onlyVerified = false } = {}) {
  const data = loadMethods()
  let list = Array.isArray(data.methods) ? data.methods.slice() : []
  if (onlyVerified) list = list.filter((m) => m.verified !== false)
  if (!list.length) return ''
  // 已验证的排前面
  list.sort((a, b) => Number(b.verified !== false) - Number(a.verified !== false))
  const out = []
  let used = 0
  for (const m of list) {
    const block = [
      `#### ${m.title}（${m.name}）${m.verified === false ? '· 未验证' : '· 已验证'}`,
      m.whenToUse ? `适用：${m.whenToUse}` : '',
      ...(m.steps || []).map((s, i) => `${i + 1}. ${s}`),
      m.notes ? `注意：${m.notes}` : ''
    ]
      .filter(Boolean)
      .join('\n')
    if (used + block.length > maxChars) break
    out.push(block)
    used += block.length
  }
  return out.join('\n\n')
}
