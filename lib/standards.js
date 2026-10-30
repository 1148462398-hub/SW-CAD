/**
 * 标注标准库 —— GB / ISO / ASME
 *
 * 设计意图：标注前必须先读一遍所选标准的规则，避免凭记忆乱标。
 * 每个标准一个 markdown 文件，正文就是给模型看的操作手册。
 */

import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const DIR = path.join(HERE, 'standards')

/** 可选标准清单（顺序即 UI 顺序） */
export const STANDARDS = [
  {
    id: 'GB',
    label: 'GB 中国国标',
    full: 'GB/T 4458.4-2003 尺寸注法',
    file: 'gb.md',
    diameterSymbol: 'φ',
    asciiCode: '%%C',
    projection: '第一角',
    units: 'mm（不注单位）',
    generalTolerance: 'GB/T 1804-m',
    diameterNote: '直径用 φ（希腊字母），AutoCAD 里写 %%C'
  },
  {
    id: 'ISO',
    label: 'ISO 国际标准',
    full: 'ISO 129-1:2018',
    file: 'iso.md',
    diameterSymbol: '⌀',
    asciiCode: '%%C',
    projection: '第一角',
    units: 'mm（不注单位）',
    generalTolerance: 'ISO 2768-m',
    diameterNote: '直径用 ⌀（U+2300）'
  },
  {
    id: 'ASME',
    label: 'ASME 美国标准',
    full: 'ASME Y14.5-2018',
    file: 'asme.md',
    diameterSymbol: '⌀',
    asciiCode: '%%C',
    projection: '第三角',
    units: 'INCH 或 mm（必须声明）',
    generalTolerance: '标题栏声明',
    diameterNote: '直径用 ⌀；第三角投影；理论正确尺寸加方框'
  }
]

const BY_ID = new Map(STANDARDS.map((s) => [s.id.toUpperCase(), s]))

/** 把用户输入归一化到合法标准 id，无法识别时回落到 GB */
export function normalizeStandard(value, fallback = 'GB') {
  if (!value) return fallback
  const key = String(value).trim().toUpperCase()
  if (BY_ID.has(key)) return key
  // 宽松识别
  if (key.includes('ISO')) return 'ISO'
  if (key.includes('ASME') || key.includes('ANSI') || key.includes('Y14')) return 'ASME'
  if (key.includes('GB') || key.includes('国标') || key.includes('中国')) return 'GB'
  return fallback
}

export function getStandardMeta(value) {
  return BY_ID.get(normalizeStandard(value)) ?? STANDARDS[0]
}

/** 读某个标准的完整规则正文 */
export function readStandard(value) {
  const meta = getStandardMeta(value)
  const file = path.join(DIR, meta.file)
  if (!existsSync(file)) {
    return {
      meta,
      ok: false,
      text: `标准文件缺失：${file}`,
      path: file
    }
  }
  let text = ''
  try {
    text = readFileSync(file, 'utf8')
  } catch (error) {
    return { meta, ok: false, text: `读取失败：${error?.message ?? error}`, path: file }
  }
  return { meta, ok: true, text, path: file, chars: text.length }
}

/** 清单（给 UI 和提示词用） */
export function listStandards() {
  return STANDARDS.map((s) => ({
    id: s.id,
    label: s.label,
    full: s.full,
    diameterSymbol: s.diameterSymbol,
    projection: s.projection,
    units: s.units
  }))
}

/** 生成"标注前必须读"的提示片段 */
export function standardChecklist(value) {
  const meta = getStandardMeta(value)
  return {
    id: meta.id,
    label: meta.label,
    full: meta.full,
    diameterSymbol: meta.diameterSymbol,
    projection: meta.projection,
    units: meta.units,
    generalTolerance: meta.generalTolerance,
    mustReadBeforeAnnotate: true,
    file: meta.file
  }
}
