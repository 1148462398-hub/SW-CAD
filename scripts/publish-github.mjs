#!/usr/bin/env node
/**
 * scripts/publish-github.mjs —— 把插件发布到 GitHub（不依赖 git）
 *
 * 背景：本机 `github.com` 的 release 下载被墙、`git` 装不上，
 * 但 `api.github.com` 能通，所以直接用 GitHub REST API 提交文件。
 *
 * 用法：
 *   set GH_TOKEN=github_pat_xxx        （PowerShell: $env:GH_TOKEN="..."）
 *   node scripts/publish-github.mjs
 *
 *   可选参数：
 *   --repo owner/name   默认 1148462398-hub/dsh-cad-draw
 *   --token-file path   从文件读 token（不想用环境变量时）
 *   --branch main       默认 main
 *   --message "..."     提交信息
 *   --dry-run           只列出将上传的文件，不实际写入
 *   --private           创建私有仓库（默认公开）
 *
 * token 需要什么权限：
 *   - 经典 PAT：勾 `repo`（要建仓库还需要 `repo` 就够）
 *   - 细粒度 PAT：Repository permissions →
 *       Contents: Read and write
 *       Administration: Read and write（用于创建仓库）
 *     并授权给目标仓库（或 All repositories）
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')

const API = 'https://api.github.com'

/* ---------------- 参数 ---------------- */
const argv = process.argv.slice(2)
function arg(name, def = null) {
  const i = argv.indexOf(`--${name}`)
  if (i === -1) return def
  const next = argv[i + 1]
  return next && !next.startsWith('--') ? next : true
}
const REPO = String(arg('repo', '1148462398-hub/SW-CAD'))
const BRANCH = String(arg('branch', 'main'))
const MESSAGE = String(arg('message', 'chore: publish dsh-cad-draw'))
const TOKEN_FILE = arg('token-file', null)
const DRY = Boolean(arg('dry-run', false))
const PRIVATE = Boolean(arg('private', false))

/* ---------------- token ---------------- */
function readToken() {
  const env = process.env.GH_TOKEN || process.env.GITHUB_TOKEN
  if (env && String(env).trim()) return String(env).trim()
  if (TOKEN_FILE) {
    const p = path.resolve(String(TOKEN_FILE))
    if (fs.existsSync(p)) return fs.readFileSync(p, 'utf8').trim()
  }
  // 兜底：常见缓存位置
  for (const p of [
    path.join(process.env.TEMP || '', '_ghtok'),
    path.join(process.env.USERPROFILE || '', '.dsh', 'gh-token.txt')
  ]) {
    try {
      if (p && fs.existsSync(p)) {
        const v = fs.readFileSync(p, 'utf8').trim()
        if (v) return v
      }
    } catch {
      /* ignore */
    }
  }
  return ''
}

const TOKEN = readToken()
if (!TOKEN) {
  console.error('✘ 没有 token。请设置环境变量 GH_TOKEN，或用 --token-file 指定文件。')
  process.exit(2)
}

/* ---------------- 要发布哪些文件 ---------------- */
// 白名单式：只发插件本体，避免把出图产物、缓存发上去
const INCLUDE_DIRS = ['lib', 'dsh', 'scripts']
const INCLUDE_FILES = ['client.js', 'package.json', 'README.md', 'LICENSE', '.gitignore']
const SKIP_DIRS = new Set(['node_modules', '__pycache__', 'out', '.git', 'assets'])
const SKIP_EXT = new Set(['.pyc', '.pyo', '.bak', '.dwg', '.dxf', '.pdf', '.png', '.jpg', '.jpeg'])

function collect() {
  const files = []
  const push = (abs) => {
    const rel = path.relative(ROOT, abs).split(path.sep).join('/')
    const ext = path.extname(abs).toLowerCase()
    if (SKIP_EXT.has(ext)) return
    const buf = fs.readFileSync(abs)
    // 只发文本文件（这几类一定是文本）
    const textExt = new Set(['.js', '.mjs', '.cjs', '.json', '.md', '.yml', '.yaml',
      '.py', '.ps1', '.cmd', '.html', '.css', '.txt'])
    if (!textExt.has(ext) && path.basename(abs) !== '.gitignore' && path.basename(abs) !== 'LICENSE') {
      console.log(`  跳过（非文本）: ${rel}`)
      return
    }
    files.push({ rel, abs, buf })
  }

  for (const f of INCLUDE_FILES) {
    const abs = path.join(ROOT, f)
    if (fs.existsSync(abs)) push(abs)
  }
  const walk = (dir) => {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const abs = path.join(dir, ent.name)
      if (ent.isDirectory()) {
        if (SKIP_DIRS.has(ent.name)) continue
        walk(abs)
      } else if (ent.isFile()) {
        push(abs)
      }
    }
  }
  for (const d of INCLUDE_DIRS) {
    const abs = path.join(ROOT, d)
    if (fs.existsSync(abs)) walk(abs)
  }
  return files
}

/* ---------------- GitHub API ---------------- */
async function gh(method, urlPath, body) {
  const res = await fetch(API + urlPath, {
    method,
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'dsh-cad-draw-publisher',
      'X-GitHub-Api-Version': '2022-11-28',
      ...(body ? { 'Content-Type': 'application/json' } : {})
    },
    body: body ? JSON.stringify(body) : undefined
  })
  const text = await res.text()
  let json = null
  try {
    json = text ? JSON.parse(text) : null
  } catch {
    json = null
  }
  if (!res.ok) {
    const err = new Error(`${method} ${urlPath} -> HTTP ${res.status}: ${json?.message || text.slice(0, 200)}`)
    err.status = res.status
    err.payload = json
    throw err
  }
  return json
}

async function ensureRepo() {
  const [owner, name] = REPO.split('/')
  // 先看当前登录用户，决定是 /user/repos 还是 /orgs/{org}/repos
  let me = null
  try {
    me = await gh('GET', '/user')
  } catch (e) {
    if (e.status === 401) throw new Error('token 无效（401）。请换一个有效 token。')
    throw e
  }
  console.log(`  登录身份: ${me.login}`)

  try {
    const r = await gh('GET', `/repos/${REPO}`)
    console.log(`  仓库已存在: ${r.full_name}  default_branch=${r.default_branch}`)
    return r.default_branch || BRANCH
  } catch (e) {
    if (e.status !== 404) throw e
  }

  console.log(`  仓库不存在，正在创建 ${REPO} …`)
  const isMine = owner.toLowerCase() === String(me.login).toLowerCase()
  const payload = {
    name,
    private: PRIVATE,
    auto_init: true,          // 带一个初始提交，便于后续用 Contents API
    description: 'SolidWorks → GB/ISO 工程图自动化插件（DSH plugin）：三视图+剖视、隐藏线、国标标注、一键打印 PDF'
  }
  if (name !== REPO.split('/')[1]) payload.name = name
  const created = isMine
    ? await gh('POST', '/user/repos', payload)
    : await gh('POST', `/orgs/${owner}/repos`, payload)
  console.log(`  ✔ 已创建: ${created.full_name}`)
  // 等一下让初始提交落定
  await new Promise((r) => setTimeout(r, 1500))
  return created.default_branch || BRANCH
}

async function currentSha(rel, branch) {
  try {
    const r = await gh('GET', `/repos/${REPO}/contents/${encodeURI(rel)}?ref=${encodeURIComponent(branch)}`)
    if (Array.isArray(r)) return null
    return r.sha || null
  } catch (e) {
    if (e.status === 404) return null
    throw e
  }
}

async function main() {
  console.log(`发布目标: ${REPO}  branch=${BRANCH}`)
  const files = collect()
  console.log(`\n待上传 ${files.length} 个文件:`)
  let total = 0
  for (const f of files) {
    total += f.buf.length
    console.log(`  ${f.rel}  (${(f.buf.length / 1024).toFixed(1)} KB)`)
  }
  console.log(`合计 ${(total / 1024).toFixed(1)} KB`)

  if (DRY) {
    console.log('\n--dry-run：不实际写入。')
    return
  }

  const branch = await ensureRepo()
  console.log(`\n开始上传（branch=${branch}）…`)
  let ok = 0
  let fail = 0
  for (const f of files) {
    try {
      const sha = await currentSha(f.rel, branch)
      const body = {
        message: MESSAGE,
        content: f.buf.toString('base64'),
        branch
      }
      if (sha) body.sha = sha
      await gh('PUT', `/repos/${REPO}/contents/${encodeURI(f.rel)}`, body)
      ok += 1
      console.log(`  ✔ ${f.rel}${sha ? '（更新）' : '（新增）'}`)
    } catch (e) {
      fail += 1
      console.error(`  ✘ ${f.rel}: ${e.message}`)
      if (e.status === 403 || e.status === 404) {
        console.error('     → token 缺少 repo 写权限（需要 Contents: Read and write）')
      }
    }
  }
  console.log(`\n完成：成功 ${ok}，失败 ${fail}`)
  if (fail === 0) {
    console.log(`仓库地址: https://github.com/${REPO}`)
  } else {
    process.exitCode = 1
  }
}

main().catch((e) => {
  console.error('发布失败:', e.message)
  if (e.payload) console.error(JSON.stringify(e.payload).slice(0, 500))
  process.exit(1)
})
