// 完全模拟面板：先 /api/upload 上传字节，再 /api/build
const fs = require('fs')
const path = require('path')
const BASE = process.argv[2]
const MODEL = process.argv[3]
const OUTDIR = process.argv[4]
const DRAWSET = process.argv[5] === '1'

async function main() {
  const name = path.basename(MODEL)
  console.log('① 上传（模拟拖拽）:', name)
  const buf = fs.readFileSync(MODEL)
  const up = await fetch(
    `${BASE}/dsh-cad-draw/api/upload?name=${encodeURIComponent(name)}`,
    { method: 'POST', body: buf }
  )
  const upJson = await up.json()
  if (!upJson.ok) {
    console.log('  上传失败:', upJson.error)
    process.exit(1)
  }
  console.log('  落盘:', upJson.path, `(${(upJson.size / 1024 / 1024).toFixed(1)}MB)`)

  console.log('② 出图…')
  const payload = {
    model: upJson.path,
    outDir: OUTDIR,
    paperSize: 'A3',
    annotationStandard: 'GB',
    landscape: true,
    views: ['front', 'top', 'right'],
    exportDxf: true,
    exportDwg: true,
    acadDimension: true,
    bom: true,
    balloons: true,
    drawset: DRAWSET,
    preview: false,
    visible: false
  }
  const r = await fetch(`${BASE}/dsh-cad-draw/api/build`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload)
  })
  const j = await r.json()
  console.log('  HTTP', r.status, ' ok =', j.ok)
  if (j.error) console.log('  错误:', j.error)
  console.log('')
  console.log('=== 面板上会看到的日志 ===')
  console.log(j.log || '(空)')
  const res = j.result || {}
  console.log('')
  console.log('=== 产物 ===')
  for (const k of ['drawing', 'dxf', 'dwg', 'preview', 'merged']) {
    const v = res[k]
    if (!v) continue
    if (typeof v === 'string') console.log(`  ${k}: ${v}`)
    else console.log(`  ${k}: ${JSON.stringify(v)}`)
  }
}

main().catch((e) => {
  console.log('失败:', e.message)
  process.exit(1)
})
