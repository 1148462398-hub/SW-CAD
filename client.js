/**
 * dsh-cad-draw / client.js
 *
 * 浏览器侧：在左侧栏加一个「CAD 出图」图标，点开后是出图工作台。
 *  - 拖拽区：把 .sldprt / .sldasm 拖进来
 *  - 参数面板：图幅 / 视图 / 公差 / 粗糙度 / 图纸信息
 *  - 一键出图，显示进度与结果
 *
 * 通过 sidebar.panellist 注册图标，main(keyed) 注册主面板。
 */

window.__ModuleLoader__.load({
  id: 'dsh-cad-draw',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useState, useCallback, useRef, useEffect } = React

    const PANEL_ID = 'dsh-cad-draw'
    /**
     * API 基地址。
     *
     * ⚠ 结论（查了 DSH 桌面端 protocol.handle 的源码）：
     *   `dsh-app://app/*` 除前端资源外，**全部带鉴权 cookie 转发给宿主 HTTP**：
     *
     *     protocol.handle(SCHEME, (request) => {
     *       if (url.hostname === 'app') {
     *         if (...前端资源...) return serveWebDocument(...)
     *         return forwardWebRequest(request, hostUrl, hostCookie)   // ★
     *       }
     *     })
     *
     *   所以**相对路径才是对的**，桌面端和浏览器都能用。
     *
     * ⚠ 不要改成 `http://127.0.0.1:<端口>`：
     *   宿主 HTTP 要求鉴权（不带 cookie 直接访问返回 401），
     *   绝对地址会绕过桌面端的 cookie 注入，实测报 ERR_FAILED。
     *
     * 之前出现的 net::ERR_UNEXPECTED / 404 是两个别的原因：
     *   · 404  = 宿主还跑着旧 api.js，`/api/pick-file` 路由不存在 → 重启即可
     *   · ERR_UNEXPECTED = 宿主正忙于 SolidWorks/AutoCAD（Node 事件循环被占）
     */
    const API = '/dsh-cad-draw'

    /* ---------------- 样式（只用主题 token） ---------------- */
    const styles = {
      shell: {
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflow: 'hidden',
        background: 'var(--dsw-alias-bg-primary, #fff)',
        color: 'var(--dsw-alias-text-primary, #222)',
        fontFamily: 'inherit'
      },
      header: {
        padding: '14px 20px 10px',
        borderBottom: '1px solid var(--dsw-alias-border-secondary, #e5e5e5)',
        flexShrink: 0
      },
      title: { fontSize: 16, fontWeight: 600, margin: 0 },
      subtitle: {
        fontSize: 12,
        marginTop: 4,
        color: 'var(--dsw-alias-text-secondary, #888)'
      },
      body: { flex: 1, overflowY: 'auto', padding: '16px 20px 28px' },
      /* 启动路径下方的探测结果提示 */
      pathHintOk: {
        fontSize: 11,
        lineHeight: 1.5,
        marginTop: 3,
        color: 'var(--dsw-alias-text-secondary, #777)',
        wordBreak: 'break-all'
      },
      pathHintBad: {
        fontSize: 11,
        lineHeight: 1.5,
        marginTop: 3,
        marginBottom: 6,
        color: 'var(--dsw-alias-text-danger, #c0392b)',
        wordBreak: 'break-all'
      },
      /* 出图进度条 */
      progressWrap: { marginTop: 12 },
      progressHead: {
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        fontSize: 12,
        marginBottom: 5
      },
      progressTrack: {
        height: 7,
        borderRadius: 4,
        overflow: 'hidden',
        background: 'var(--dsw-alias-bg-secondary, #ececec)'
      },
      progressFill: (percent, failed) => ({
        height: '100%',
        width: Math.max(0, Math.min(100, Number(percent) || 0)) + '%',
        borderRadius: 4,
        transition: 'width .35s ease',
        background: failed
          ? '#c0392b'
          : 'linear-gradient(90deg, #1f6fb2, #38a3e0)'
      }),
      progressStage: {
        fontSize: 11,
        marginTop: 4,
        opacity: 0.65,
        fontFamily: 'ui-monospace, Consolas, monospace'
      },
      drop: (over) => ({
        border: '2px dashed ' + (over
          ? 'var(--dsw-alias-brand-primary, #247bbf)'
          : 'var(--dsw-alias-border-secondary, #d0d0d0)'),
        borderRadius: 10,
        padding: '32px 20px',
        textAlign: 'center',
        cursor: 'pointer',
        transition: 'border-color .15s, background .15s',
        background: over
          ? 'var(--dsw-alias-bg-secondary, #f2f7fc)'
          : 'transparent'
      }),
      dropTitle: { fontSize: 14, fontWeight: 500 },
      dropHint: {
        fontSize: 12,
        marginTop: 6,
        color: 'var(--dsw-alias-text-secondary, #888)'
      },
      sectionTitle: {
        fontSize: 13,
        fontWeight: 600,
        margin: '20px 0 10px'
      },
      row: {
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        marginBottom: 10
      },
      label: {
        width: 96,
        fontSize: 13,
        color: 'var(--dsw-alias-text-secondary, #666)',
        flexShrink: 0
      },
      input: {
        flex: 1,
        minWidth: 0,
        padding: '6px 9px',
        fontSize: 13,
        borderRadius: 6,
        border: '1px solid var(--dsw-alias-border-secondary, #d0d0d0)',
        background: 'var(--dsw-alias-bg-primary, #fff)',
        color: 'inherit',
        outline: 'none'
      },
      select: {
        flex: 1,
        padding: '6px 9px',
        fontSize: 13,
        borderRadius: 6,
        border: '1px solid var(--dsw-alias-border-secondary, #d0d0d0)',
        background: 'var(--dsw-alias-bg-primary, #fff)',
        color: 'inherit'
      },
      check: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 },
      // 复选框：显式 accent-color，避免主题色太浅看不清
      cb: { accentColor: '#1f6fb2', width: 15, height: 15, cursor: 'pointer' },
      btn: (primary, disabled) => ({
        padding: '9px 18px',
        fontSize: 13,
        fontWeight: 600,
        borderRadius: 7,
        border: primary
          ? '1px solid #1a5680'
          : '1px solid var(--dsw-alias-border-secondary, #d0d0d0)',
        cursor: disabled ? 'not-allowed' : 'pointer',
        // 主按钮不要依赖主题品牌色：某些主题里 --dsw-alias-brand-primary 很浅，
        // 白字压在上面完全看不清（实测踩过）。这里用固定深蓝 + 显式白字。
        background: primary
          ? (disabled ? '#b6c4d0' : '#1f6fb2')
          : 'transparent',
        color: primary
          ? (disabled ? '#4a5763' : '#ffffff')
          : 'inherit',
        textShadow: 'none',
        whiteSpace: 'nowrap'
      }),
      /* 小号次要按钮（「恢复默认」「检测路径」这类） */
      btnGhost: {
        padding: '4px 10px',
        fontSize: 12,
        fontWeight: 500,
        borderRadius: 6,
        border: '1px solid var(--dsw-alias-border-secondary, #d0d0d0)',
        background: 'transparent',
        color: 'inherit',
        cursor: 'pointer',
        whiteSpace: 'nowrap'
      },
      fileChip: {
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        padding: '5px 10px',
        borderRadius: 6,
        fontSize: 12,
        background: 'var(--dsw-alias-bg-secondary, #f0f0f0)',
        maxWidth: '100%'
      },
      log: {
        marginTop: 16,
        padding: 12,
        borderRadius: 8,
        fontSize: 12,
        lineHeight: 1.6,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
        background: 'var(--dsw-alias-bg-secondary, #f7f7f7)',
        maxHeight: 260,
        overflowY: 'auto',
        fontFamily: 'ui-monospace, Menlo, Consolas, monospace'
      },
      err: { color: '#c0392b' }
    }

    /* ---------------- 表单 ---------------- */
    // A5-A1（含 A0 可选）：图幅由用户自己选，插件按所选图幅自动算比例
    const PAPER_SIZES = ['A5', 'A4', 'A3', 'A2', 'A1', 'A0']
    // 标注标准：切换后直径符号、投影法、一般公差都随之变化
    const STANDARD_OPTIONS = [
      ['GB', 'GB 中国国标（φ / 第一角）'],
      ['ISO', 'ISO 国际标准（⌀ / 第一角）'],
      ['ASME', 'ASME 美国标准（⌀ / 第三角）']
    ]
    const VIEW_OPTIONS = [
      ['front', '前视'],
      ['top', '上视'],
      ['right', '右视'],
      ['isometric', '轴测'],
      ['left', '左视'],
      ['back', '后视'],
      ['bottom', '下视']
    ]

    function defaultForm() {
      return {
        paperSize: 'A3',
        annotationStandard: 'GB',
        // ── 软件启动路径（留空 = 自动探测）──
        solidworksExe: '',
        autocadExe: '',
        landscape: true,
        views: ['front', 'top', 'right', 'isometric'],
        sectionMode: 'auto',
        detailScale: '',
        toleranceOn: true,
        toleranceType: 'symmetric',
        tolerancePlus: '0.05',
        toleranceOnlyOver: '10',
        roughnessOn: true,
        roughnessValue: '3.2',
        // ── 技术要求（按需填写，留空则不写进图）──
        generalToleranceLevel: 'm',   // GB/T 1804-m
        unnotedFillet: 'R1',          // 未注圆角
        unnotedChamfer: 'C0.5',       // 未注倒角
        unnotedRoughness: '',         // 未注粗糙度
        surfaceTreatment: '',         // 表面处理：阳极氧化/镀锌/发黑…
        // ── 配合公差（如 Ø5.6 的孔标 H7）──
        fitTarget: '',                // 配合尺寸，如 φ5.6
        fitCode: '',                  // 公差代号，如 H7
        // ── 夹具中心线 ──
        fixtureCenterline: false,
        fixtureNote: '',              // 中心线说明，如 基准A 中心线
        // ── 附加技术要求（自由文本，一行一条）──
        techNotes: '',
        material: '',
        drawingTitle: '',
        drawingNumber: '',
        revision: 'A',
        exportDxf: true,
        exportDwg: true,
        // AutoCAD 后处理：把视图搬进图框 + 国标标注 + 标题栏
        acadDimension: true,
        // 装配体：整套图纸（总图 + 每个零件图 + 合并 DXF）
        drawset: true,
        bom: true,
        balloons: true,
      }
    }

    /** 拖进来的是不是装配体（决定要不要显示「整套图纸」区块） */
    function isAssemblyFile(f) {
      if (!f) return false
      const s = String(f.name || f.path || '')
      return /\.sldasm$/i.test(s)
    }

    function Field(props) {
      return h('div', { style: styles.row },
        h('div', { style: styles.label }, props.label),
        props.children
      )
    }

    /**
     * 启动路径输入框下方的一行状态提示。
     * item 是 /api/probe 返回的 paths.solidworks / paths.autocad。
     */
    function pathHint(item, manualValue) {
      if (!item) {
        return h('div', { style: styles.pathHintOk },
          '正在自动探测…（也可以直接填完整路径）')
      }
      if (!item.exists) {
        return h('div', { style: styles.pathHintBad },
          '✗ 没找到：' + (item.exe || item.source || '未知') +
          (manualValue ? '' : '　→ 请在上面填完整路径'))
      }
      const parts = ['✓ ' + item.source]
      if (item.running) parts.push('运行中')
      else parts.push('未运行（出图时自动启动）')
      return h('div', { style: styles.pathHintOk },
        parts.join(' · '),
        h('div', { style: { opacity: 0.75 } }, item.exe)
      )
    }

    /* ---------------- 主面板 ---------------- */
    /* 表单持久化：插件热更新 / 页面刷新 / 组件重挂载 都不能把用户填的路径弄丢。
       用户报过「2 个软件启动路径我添加了也加不进去」——本质就是 state 没存住。 */
    const LS_FORM = 'dsh-cad-draw.form'
    const LS_PROBE = 'dsh-cad-draw.probe'

    function lsGet(key, fallback) {
      try {
        const s = window.localStorage.getItem(key)
        if (!s) return fallback
        const v = JSON.parse(s)
        return v && typeof v === 'object' ? v : fallback
      } catch {
        return fallback
      }
    }

    function lsSet(key, value) {
      try {
        window.localStorage.setItem(key, JSON.stringify(value))
      } catch {
        /* 隐私模式等存不了，忽略 */
      }
    }

    function CadPanel() {
      /* ⚠ defaultForm 是**函数**（useState(defaultForm) 走的是 lazy initializer）。
         所以任何地方要它的值都必须 `defaultForm()` 调用。
         本鱼以前写成 `{ ...defaultForm, ...rawForm }` —— 展开一个函数得到 {}，
         所有默认字段（含 views）全丢，面板直接崩/输入框回填失败。
         统一用这个常量，别再直接展开 defaultForm。 */
      const DEFAULTS = defaultForm()

      const [rawForm, setForm] = useState(() => {
        const saved = lsGet(LS_FORM, {})
        const merged = { ...defaultForm(), ...saved }
        /* 数组类字段要保证是数组，别被坏数据带崩 */
        if (!Array.isArray(merged.views) || !merged.views.length) {
          merged.views = defaultForm().views
        }
        return merged
      })
      /* ★ 每次渲染都合并一次默认值。
         原因：插件热更新（或改了 defaultForm 加了新字段）时，
         useState 里保存的还是**旧对象**，里面没有新字段。
         任何 form.newField.xxx 都会 undefined 崩掉整个面板（黑屏）。
         这里统一兜底，新增字段再也不会炸。 */
      const form = { ...DEFAULTS, ...rawForm }
      const [file, setFile] = useState(null)
      /* 用原生对话框选出来的**原始绝对路径**。
         装配体必须走这条路：拖拽只能拿到字节，复制到 uploads 后
         同目录零件就找不到了（实测明细表只剩 2 行、视图空白）。 */
      const [pickedPath, setPickedPath] = useState('')
      const [over, setOver] = useState(false)
      const [busy, setBusy] = useState(false)
      const [pdfBusy, setPdfBusy] = useState(false)
      const [probeBusy, setProbeBusy] = useState(false)
      // 出图进度：{active, percent, label, stage, error}
      const [progress, setProgress] = useState(null)
      // 自动探测到的启动路径（打开面板就跑一次，直接显示在输入框下面）
      // 结果也缓存，重挂载时先用缓存渲染，避免「一直是空白」的错觉
      const [detected, setDetected] = useState(() => {
        const c = lsGet(LS_PROBE, null)
        return c || { solidworks: null, autocad: null, python: '', revision: '', error: '' }
      })
      const [pdfPath, setPdfPath] = useState('')
      const [cmdText, setCmdText] = useState('')
      const [log, setLog] = useState('')
      const [result, setResult] = useState(null)
      const inputRef = useRef(null)

      /* 表单变化就落盘（防抖 300ms，避免每敲一个字母写一次） */
      useEffect(() => {
        const t = setTimeout(() => lsSet(LS_FORM, rawForm), 300)
        return () => clearTimeout(t)
      }, [rawForm])

      const set = useCallback((key, value) => {
        setForm((prev) => ({ ...prev, [key]: value }))
      }, [])

      /** 弹 Windows 原生「选择文件夹」对话框（资源管理器那种） */
      const pickOutDir = useCallback(async () => {
        try {
          setLog('正在弹出选择目录对话框（可能在任务栏里闪烁）…')
          const r = await fetch(API + '/api/pick-folder', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ initial: '' })
          })
          const j = await r.json().catch(() => ({}))
          if (!j?.ok) {
            setLog('选择目录失败：' + (j?.error || '后端没有响应'))
            return
          }
          if (j.canceled || !j.path) {
            setLog('已取消选择目录')
            return
          }
          setForm((prev) => ({ ...prev, outDir: j.path }))
          setLog('输出目录已设为：' + j.path)
        } catch (e) {
          setLog('选择目录失败：' + (e?.message || e))
        }
      }, [])

      const toggleView = useCallback((key) => {
        setForm((prev) => {
          const has = prev.views.includes(key)
          return {
            ...prev,
            views: has
              ? prev.views.filter((v) => v !== key)
              : [...prev.views, key]
          }
        })
      }, [])

      const acceptFile = useCallback((f) => {
        if (!f) return
        const name = String(f.name || '')
        const low = name.toLowerCase()
        if (!low.endsWith('.sldprt') && !low.endsWith('.sldasm')) {
          setLog('只接受 SolidWorks 原生零件：.sldprt / .sldasm\n' +
            'STEP/IGES 请先在 SolidWorks 里另存为 .sldprt。')
          return
        }
        // 拖拽来的文件没有真实路径，清掉之前用对话框选过的路径
        setPickedPath('')
        setFile(f)
        setLog('')
        setResult(null)
        // 图纸名称默认取文件名
        if (!form.drawingTitle) {
          set('drawingTitle', name.replace(/\.(sldprt|sldasm)$/i, ''))
        }
      }, [form.drawingTitle, set])

      /** 用原生对话框选软件启动程序（SLDWORKS.exe / acad.exe） */
      const pickExe = useCallback(async (field, detectedPath) => {
        try {
          setLog('正在弹出选择程序对话框（可能在任务栏里闪烁）…')
          const initial = String(detectedPath || '').replace(/[\\/][^\\/]*$/, '')
          const r = await fetch(API + '/api/pick-file', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ kind: 'exe', initial })
          })
          const j = await r.json().catch(() => ({}))
          if (!j?.ok) {
            setLog('选择程序失败：' + (j?.error || '后端没有响应'))
            return
          }
          if (j.canceled || !j.path) {
            setLog('已取消选择程序')
            return
          }
          set(field, String(j.path))
          setLog(`${field === 'solidworksExe' ? 'SolidWorks' : 'AutoCAD'} 路径已设为：${j.path}`)
        } catch (e) {
          setLog('选择程序失败：' + (e?.message || e))
        }
      }, [set])

      /** 用原生对话框选模型 —— 装配体唯一正确的用法（保住零件引用） */
      const pickModelFile = useCallback(async () => {
        try {
          setLog('正在弹出选择文件对话框（可能在任务栏里闪烁）…')
          const r = await fetch(API + '/api/pick-file', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ initial: '' })
          })
          const j = await r.json().catch(() => ({}))
          if (!j?.ok) {
            setLog('选择文件失败：' + (j?.error || '后端没有响应'))
            return
          }
          if (j.canceled || !j.path) {
            setLog('已取消选择文件')
            return
          }
          const full = String(j.path)
          const nm = full.replace(/^.*[\\/]/, '')
          // 用一个「假 File」显示文件名，真实路径存在 pickedPath 里
          setFile({ name: nm, path: full, size: 0, picked: true })
          setPickedPath(full)
          setResult(null)
          setLog('已选择模型（原始路径）：' + full)
          if (!form.drawingTitle) {
            set('drawingTitle', nm.replace(/\.(sldprt|sldasm)$/i, ''))
          }
        } catch (e) {
          setLog('选择文件失败：' + (e?.message || e))
        }
      }, [form.drawingTitle, set])

      const onDrop = useCallback((e) => {
        e.preventDefault()
        setOver(false)
        const f = e.dataTransfer?.files?.[0]
        acceptFile(f)
      }, [acceptFile])

      const build = useCallback(async () => {
        if (!file) return
        setBusy(true)
        setResult(null)
        setProgress({ active: true, percent: 0, label: '准备任务', stage: 'start' })
        /* /api/build 跑完才返回，中途状态靠轮询 /api/progress 拿 */
        const poller = setInterval(async () => {
          try {
            const r = await fetch(API + '/api/progress')
            const j = await r.json()
            if (j?.progress) setProgress(j.progress)
          } catch {
            /* 忽略轮询失败 */
          }
        }, 800)
        try {
          /* 三种来源，优先级从高到低：
             1) pickedPath —— 原生对话框选的**原始路径**（装配体必须用这个，
                因为它依赖同目录零件；复制到 uploads 会丢引用）
             2) file.path —— Electron/桌面端才有
             3) 上传字节 —— 纯浏览器拖拽，落盘成副本 */
          let modelPath = pickedPath || file.path || ''
          if (modelPath && pickedPath) {
            setLog(`使用原始路径：${modelPath}\n\n正在调用 SolidWorks 出图，单件约 1–4 分钟，请勿重复点击…\n`)
          } else if (!modelPath) {
            setLog('正在上传模型到本机…\n')
            const up = await fetch(
              API + '/api/upload?name=' + encodeURIComponent(file.name),
              { method: 'POST', body: file }
            )
            const upJson = await up.json()
            if (!upJson?.ok || !upJson.path) {
              throw new Error('上传失败：' + (upJson?.error || '后端没有返回路径'))
            }
            modelPath = upJson.path
            setLog(`模型已存到：${modelPath}\n\n正在调用 SolidWorks 出图，单件约 1–4 分钟，请勿重复点击…\n`)
          } else {
            setLog('正在调用 SolidWorks 出图，单件约 1–4 分钟，请勿重复点击…\n')
          }

          /* 输出目录：用户没填就自动建一个「出图_<模型名>」，
             并把最终路径写进日志，避免「不知道存哪去了」。 */
          let outDir = (form.outDir || '').trim()
          if (!outDir) {
            const dir = modelPath.replace(/[\\/][^\\/]+$/, '')
            const base = String(file.name || 'model').replace(/\.[^.]+$/, '')
            outDir = dir + '\\出图_' + base
          }
          setLog((prev) => prev + `输出目录：${outDir}\n\n`)

          const payload = {
            model: modelPath,
            outDir,
            paperSize: form.paperSize,
            annotationStandard: form.annotationStandard,
            solidworksExe: form.solidworksExe || '',
            autocadExe: form.autocadExe || '',
            landscape: form.landscape,
            views: form.views,
            sectionView: form.sectionMode === 'auto'
              ? 'auto'
              : form.sectionMode === 'on',
            detailView: form.detailScale
              ? { scale: Number(form.detailScale) || 2, label: 'A' }
              : null,
            tolerance: form.toleranceOn
              ? {
                  enabled: true,
                  type: form.toleranceType,
                  plus: Number(form.tolerancePlus) || 0.05,
                  onlyOver: Number(form.toleranceOnlyOver) || 0
                }
              : { enabled: false },
            surfaceFinish: form.roughnessOn
              ? { enabled: true, value: form.roughnessValue }
              : { enabled: false },
            /* 技术要求：用户按需填，留空的项由引擎跳过 */
            technicalRequirements: {
              generalToleranceLevel: form.generalToleranceLevel,
              unnotedFillet: form.unnotedFillet,
              unnotedChamfer: form.unnotedChamfer,
              unnotedRoughness: form.unnotedRoughness,
              surfaceTreatment: form.surfaceTreatment,
              extraNotes: form.techNotes
            },
            /* 配合公差，如 φ5.6 H7 */
            fits: form.fitTarget && form.fitCode
              ? { target: form.fitTarget, code: form.fitCode }
              : null,
            /* 夹具/基准中心线 */
            fixture: form.fixtureCenterline
              ? { enabled: true, note: form.fixtureNote }
              : { enabled: false },
            titleBlock: {
              material: form.material,
              title: form.drawingTitle,
              number: form.drawingNumber,
              revision: form.revision
            },
            exportDxf: form.exportDxf,
            exportDwg: form.exportDwg,
            acadDimension: form.acadDimension,
            drawset: form.drawset,
            bom: form.bom,
            balloons: form.balloons,
            preview: true
          }
          const res = await fetch(API + '/api/build', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(payload)
          })
          const json = await res.json()
          setLog(json.log || (json.ok ? '完成' : '失败：' + (json.error || '')))
          setResult(json.result ?? null)
        } catch (error) {
          setLog('出图失败：' + (error?.message || String(error)))
        } finally {
          clearInterval(poller)
          // 最后再同步一次，确保进度条停在真实状态（成功 100% / 失败停在出错阶段）
          try {
            const r = await fetch(API + '/api/progress')
            const j = await r.json()
            if (j?.progress) setProgress(j.progress)
          } catch {
            /* 忽略 */
          }
          setBusy(false)
        }
      }, [file, form])
      /* 检测启动路径：按当前填的路径探测，显示实际找到的 exe 和是否在运行。
         detectOnly=true 时（打开面板自动跑一次）不写日志、不覆盖用户正在看的输出，
         只更新 detected 状态把结果展示在输入框下面。 */
      const probePaths = useCallback(async (detectOnly = false) => {
        if (!detectOnly) setProbeBusy(true)
        if (!detectOnly) setLog('正在检测 SolidWorks / AutoCAD 启动路径…\n')
        try {
          const qs = new URLSearchParams()
          if (form.solidworksExe) qs.set('solidworksExe', form.solidworksExe)
          if (form.autocadExe) qs.set('autocadExe', form.autocadExe)
          const res = await fetch(API + '/api/probe?' + qs.toString())
          const json = await res.json()
          const p = json?.info?.paths || {}
          if (!json?.info || json.info.error) {
            throw new Error(json?.info?.error || json?.error || '接口返回异常')
          }
          const next = {
            solidworks: p.solidworks || null,
            autocad: p.autocad || null,
            python: json?.info?.python || '',
            revision: json?.info?.solidworks?.revision || '',
            error: ''
          }
          setDetected(next)
          lsSet(LS_PROBE, next)
          if (!detectOnly) {
            const lines = []
            for (const [k, label] of [['solidworks', 'SolidWorks'], ['autocad', 'AutoCAD']]) {
              const it = p[k] || {}
              lines.push(
                `${label}: ${it.exe || '未找到'}` +
                `\n  来源=${it.source || '?'}  存在=${it.exists ? '是' : '否'}  运行中=${it.running ? '是' : '否'}`
              )
            }
            const sw = json?.info?.solidworks
            if (sw?.error) lines.push(`SolidWorks 连接错误: ${sw.error}`)
            else if (sw?.revision) lines.push(`SolidWorks 版本: ${sw.revision}`)
            setLog(lines.join('\n\n'))
          }
        } catch (error) {
          setDetected((d) => ({ ...d, error: error?.message || String(error) }))
          if (!detectOnly) setLog('检测失败：' + (error?.message || String(error)))
        } finally {
          if (!detectOnly) setProbeBusy(false)
        }
      }, [form.solidworksExe, form.autocadExe])


      /* 生成一条「给 AI 的指令」。
         桌面端 HTTP 接口不可靠（dsh-app:// 下插件接口会被挂住），
         但**工具调用这条路是稳的**。所以面板退一步：
         收集参数 → 生成指令 → 用户粘进对话框 → AI 调工具完成。
         这样出图完全不依赖插件自己的 HTTP 通道。 */
      const buildCommand = useCallback(() => {
        const model = pickedPath || file?.path || '<先拖入模型或用「选择模型文件」选>'
        const lines = [`model: "${model}"`]
        if (form.outDir) lines.push(`outDir: "${form.outDir}"`)
        lines.push(`paperSize: "${form.paperSize}"`)
        lines.push(`annotationStandard: "${form.annotationStandard}"`)
        lines.push(`views: [${(form.views || []).map((v) => `"${v}"`).join(', ')}]`)
        lines.push(`landscape: ${form.landscape !== false}`)
        const isAsm = /\.sldasm$/i.test(String(model))
        if (isAsm) {
          lines.push(`bom: ${form.bom !== false}`)
          lines.push(`balloons: ${form.balloons !== false}`)
          if (form.drawset !== false) lines.push('drawset: true')
        }
        if (form.toleranceOn) {
          lines.push(
            `tolerance: { type: "${form.toleranceType}", plus: ${Number(form.tolerancePlus) || 0.05}, onlyOver: ${Number(form.toleranceOnlyOver) || 0} }`
          )
        }
        if (form.roughnessOn) {
          lines.push(`surfaceFinish: { value: "${form.roughnessValue || '3.2'}" }`)
        }
        const cmd = '用 cad_draw_build 出图：\n' + lines.map((l) => '  ' + l).join('\n')
        setCmdText(cmd)
        return cmd
      }, [pickedPath, file, form, set])

      /* 一键交给 AI：生成指令 → 直接提交给当前会话 → AI 自己调工具出图。
         用户要求「生成指令自动去做，不用我来点」。 */
      const dispatchToAgent = useCallback(async () => {
        const cmd = buildCommand()
        if (!conversationService || typeof conversationService.send !== 'function') {
          setLog(
            '当前会话不支持自动发送（conversation 服务不可用）。\n' +
            '指令已生成并复制，请手动粘贴到对话框发送。'
          )
          try {
            navigator?.clipboard?.writeText(cmd)
          } catch {
            /* 忽略 */
          }
          return
        }
        try {
          setBusy(true)
          setLog('已把出图指令发给本鱼，正在调用插件工具，请稍候…\n')
          await conversationService.send(cmd)
          setLog('指令已提交 ✓ 本鱼会在对话里接着执行，进度看上面的对话区。')
        } catch (e) {
          setLog('自动发送失败：' + (e?.message || e) + '\n指令已生成，可手动粘贴。')
        } finally {
          setBusy(false)
        }
      }, [buildCommand, setLog])

      /* 打开面板就自动探测一次启动路径（只做一次，不打扰日志） */
      const autoDetectRef = useRef(false)
      useEffect(() => {
        if (autoDetectRef.current) return
        autoDetectRef.current = true
        probePaths(true)
      }, [probePaths])

      /* 一键打印 PDF：打印范围自动取图纸里最外层的包围框；黑白输出，DWG 颜色保留 */
      const printPdf = useCallback(async () => {
        const dwg = result?.dwg
        if (!dwg) {
          setLog('还没出图。先点「开始出图」，拿到 DWG 之后再打印 PDF。')
          return
        }
        setPdfBusy(true)
        setPdfPath('')
        setLog('正在打印 PDF…\n范围＝图纸最外层包围框，黑白输出，约 20–30 秒，请勿重复点击。\n')
        try {
          const res = await fetch(API + '/api/pdf', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({
              dwg,
              monochrome: true,
              autocadExe: form.autocadExe || ''
            })
          })
          const json = await res.json()
          setLog(json.log || (json.ok ? '打印完成' : '打印失败：' + (json.error || '')))
          if (json.ok && json.result?.pdf) setPdfPath(json.result.pdf)
        } catch (error) {
          setLog('打印失败：' + (error?.message || String(error)))
        } finally {
          setPdfBusy(false)
        }
      }, [result])

      return h('div', { style: styles.shell },
        h('div', { style: styles.header },
          h('h1', { style: styles.title }, 'CAD 自动出图'),
          h('div', { style: styles.subtitle },
            'SolidWorks 模型 → GB 工程图 → DXF / DWG')
        ),
        h('div', { style: styles.body },

          /* 拖拽区 */
          h('div', {
            style: styles.drop(over),
            onDragOver: (e) => { e.preventDefault(); setOver(true) },
            onDragLeave: () => setOver(false),
            onDrop,
            onClick: () => inputRef.current?.click()
          },
            h('div', { style: styles.dropTitle },
              file ? '已选择文件' : '把 .sldprt / .sldasm 拖到这里'),
            h('div', { style: styles.dropHint },
              file
                ? file.name
                : '或点击选择文件（STEP/IGES 需先另存为 .sldprt）'),
            h('input', {
              ref: inputRef,
              type: 'file',
              accept: '.sldprt,.sldasm',
              style: { display: 'none' },
              onChange: (e) => acceptFile(e.target.files?.[0])
            })
          ),

          /* 装配体必须用「原始路径」：拖拽只会把 .SLDASM 本身复制到 uploads，
             同目录的零件引用全断，开出来是空装配体。 */
          h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 } },
            h('button', {
              type: 'button',
              style: { ...styles.btnGhost, padding: '6px 14px' },
              onClick: () => pickModelFile()
            }, '选择模型文件（装配体请用这个）')
          ),
          (() => {
            const isAsm = file && /\.sldasm$/i.test(String(file.name || ''))
            if (!isAsm) return null
            return h('div', {
              style: {
                marginTop: 8, padding: '8px 12px', borderRadius: 6,
                border: '1px solid #e0b100', background: 'rgba(224,177,0,0.12)',
                fontSize: 11.5, lineHeight: 1.6
              }
            },
              '⚠ 装配体不能靠「拖拽」上传：拖进来的只是 .SLDASM 一个文件，',
              '同目录的零件找不到，SolidWorks 会开成空装配体（明细表只剩几行、视图空白）。',
              h('br'),
              '请点上面的『选择模型文件』，选 D:\\5寸机\\装配体1.SLDASM 这个**原始位置**的文件。'
            )
          })(),

          /* 软件启动路径 */
          h('div', { style: styles.sectionTitle }, '软件启动路径（留空自动探测）'),
          h('div', { style: { display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 6 } },
            h('span', { style: { fontSize: 12, opacity: 0.75, flex: '0 0 84px', paddingTop: 7 } }, 'SolidWorks'),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('input', {
                style: { ...styles.input, width: '100%', boxSizing: 'border-box' },
                placeholder: detected.solidworks?.exe || 'SLDWORKS.exe 路径（会自动探测）',
                value: form.solidworksExe || '',
                onChange: (e) => set('solidworksExe', e.target.value)
              }),
              pathHint(detected.solidworks, form.solidworksExe)
            ),
            h('button', {
              type: 'button',
              style: { ...styles.btnGhost, padding: '5px 12px', marginTop: 1 },
              onClick: () => pickExe('solidworksExe', detected.solidworks?.exe)
            }, '浏览…')
          ),
          h('div', { style: { display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 6 } },
            h('span', { style: { fontSize: 12, opacity: 0.75, flex: '0 0 84px', paddingTop: 7 } }, 'AutoCAD'),
            h('div', { style: { flex: 1, minWidth: 0 } },
              h('input', {
                style: { ...styles.input, width: '100%', boxSizing: 'border-box' },
                placeholder: detected.autocad?.exe || 'acad.exe 路径，也可只填安装目录',
                value: form.autocadExe || '',
                onChange: (e) => set('autocadExe', e.target.value)
              }),
              pathHint(detected.autocad, form.autocadExe)
            ),
            h('button', {
              type: 'button',
              style: { ...styles.btnGhost, padding: '5px 12px', marginTop: 1 },
              onClick: () => pickExe('autocadExe', detected.autocad?.exe)
            }, '浏览…')
          ),
          detected.error
            ? h('div', { style: styles.pathHintBad }, '✗ 探测失败：' + detected.error)
            : null,
          h('div', { style: { display: 'flex', gap: 10, marginBottom: 8 } },
            h('button', {
              style: styles.btn(false, busy || probeBusy),
              disabled: busy || probeBusy,
              onClick: () => probePaths(false),
              title: '按当前填的路径去探测 SolidWorks / AutoCAD，并显示找到的实际路径'
            }, probeBusy ? '检测中…' : '重新检测')
          ),

          /* 图幅与视图 */
          h('div', { style: styles.sectionTitle }, '图幅与视图'),
          h(Field, { label: '图幅' },
            h('select', {
              style: styles.select,
              value: form.paperSize,
              onChange: (e) => set('paperSize', e.target.value)
            }, PAPER_SIZES.map((p) => h('option', { key: p, value: p }, p)))
          ),
          h(Field, { label: '标注标准' },
            h('select', {
              style: styles.select,
              value: form.annotationStandard,
              onChange: (e) => set('annotationStandard', e.target.value)
            }, STANDARD_OPTIONS.map(([v, label]) => h('option', { key: v, value: v }, label)))
          ),
          h(Field, { label: '方向' },
            h('select', {
              style: styles.select,
              value: form.landscape ? 'l' : 'p',
              onChange: (e) => set('landscape', e.target.value === 'l')
            },
              h('option', { value: 'l' }, '横向'),
              h('option', { value: 'p' }, '纵向')
            )
          ),
          h(Field, { label: '视图' },
            h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 12 } },
              VIEW_OPTIONS.map(([key, label]) =>
                h('label', { key, style: styles.check },
                  h('input', {
                    type: 'checkbox',
                    style: styles.cb,
                    checked: form.views.includes(key),
                    onChange: () => toggleView(key)
                  }),
                  label
                )
              )
            )
          ),
          h(Field, { label: '剖视图' },
            h('select', {
              style: styles.select,
              value: form.sectionMode,
              onChange: (e) => set('sectionMode', e.target.value)
            },
              h('option', { value: 'auto' }, '自动判断（有内腔才加）'),
              h('option', { value: 'on' }, '强制添加'),
              h('option', { value: 'off' }, '不加')
            )
          ),
          h(Field, { label: '放大图倍数' },
            h('input', {
              style: styles.input,
              placeholder: '留空则不加局部放大图，如 2',
              value: form.detailScale,
              onChange: (e) => set('detailScale', e.target.value)
            })
          ),

          /* 公差 */
          h('div', { style: styles.sectionTitle }, '尺寸公差'),
          h(Field, { label: '启用' },
            h('label', { style: styles.check },
              h('input', {
                type: 'checkbox',
                style: styles.cb,
                checked: form.toleranceOn,
                onChange: (e) => set('toleranceOn', e.target.checked)
              }), '加尺寸公差')
          ),
          h(Field, { label: '类型' },
            h('select', {
              style: styles.select,
              value: form.toleranceType,
              onChange: (e) => set('toleranceType', e.target.value)
            },
              h('option', { value: 'symmetric' }, '对称（±）'),
              h('option', { value: 'bilateral' }, '双向'),
              h('option', { value: 'limit' }, '极限')
            )
          ),
          h(Field, { label: '上偏差' },
            h('input', {
              style: styles.input,
              value: form.tolerancePlus,
              onChange: (e) => set('tolerancePlus', e.target.value)
            })
          ),
          h(Field, { label: '只标≥(mm)' },
            h('input', {
              style: styles.input,
              value: form.toleranceOnlyOver,
              onChange: (e) => set('toleranceOnlyOver', e.target.value)
            })
          ),

          /* 粗糙度 */
          h('div', { style: styles.sectionTitle }, '表面粗糙度'),
          h(Field, { label: '启用' },
            h('label', { style: styles.check },
              h('input', {
                type: 'checkbox',
                style: styles.cb,
                checked: form.roughnessOn,
                onChange: (e) => set('roughnessOn', e.target.checked)
              }), '加粗糙度符号')
          ),
          h(Field, { label: 'Ra 值' },
            h('input', {
              style: styles.input,
              value: form.roughnessValue,
              onChange: (e) => set('roughnessValue', e.target.value)
            })
          ),

          /* 技术要求 */
          h('div', { style: styles.sectionTitle }, '技术要求（按需填，留空不写）'),
          h(Field, { label: '未注公差' },
            h('select', {
              style: styles.select,
              value: form.generalToleranceLevel,
              onChange: (e) => set('generalToleranceLevel', e.target.value)
            },
              h('option', { value: 'f' }, 'GB/T 1804-f（精密）'),
              h('option', { value: 'm' }, 'GB/T 1804-m（中等）'),
              h('option', { value: 'c' }, 'GB/T 1804-c（粗糙）'),
              h('option', { value: 'v' }, 'GB/T 1804-v（最粗）'),
              h('option', { value: '' }, '不写')
            )
          ),
          h(Field, { label: '未注圆角' },
            h('input', {
              style: styles.input,
              placeholder: '如 R1，留空不写',
              value: form.unnotedFillet,
              onChange: (e) => set('unnotedFillet', e.target.value)
            })
          ),
          h(Field, { label: '未注倒角' },
            h('input', {
              style: styles.input,
              placeholder: '如 C0.5，留空不写',
              value: form.unnotedChamfer,
              onChange: (e) => set('unnotedChamfer', e.target.value)
            })
          ),
          h(Field, { label: '未注粗糙度' },
            h('input', {
              style: styles.input,
              placeholder: '如 Ra3.2，留空不写',
              value: form.unnotedRoughness,
              onChange: (e) => set('unnotedRoughness', e.target.value)
            })
          ),
          h(Field, { label: '表面处理' },
            h('input', {
              style: styles.input,
              placeholder: '如 阳极氧化 / 镀锌 / 发黑，留空不写',
              value: form.surfaceTreatment,
              onChange: (e) => set('surfaceTreatment', e.target.value)
            })
          ),

          /* 配合公差 */
          h('div', { style: styles.sectionTitle }, '配合公差（如孔标 H7）'),
          h(Field, { label: '配合尺寸' },
            h('input', {
              style: styles.input,
              placeholder: '如 φ5.6，留空则不标配合',
              value: form.fitTarget,
              onChange: (e) => set('fitTarget', e.target.value)
            })
          ),
          h(Field, { label: '公差代号' },
            h('input', {
              style: styles.input,
              placeholder: '如 H7 / h6 / H8',
              value: form.fitCode,
              onChange: (e) => set('fitCode', e.target.value)
            })
          ),

          /* 夹具中心线 */
          h('div', { style: styles.sectionTitle }, '夹具中心线'),
          h(Field, { label: '启用' },
            h('label', { style: styles.check },
              h('input', {
                type: 'checkbox',
                style: styles.cb,
                checked: form.fixtureCenterline,
                onChange: (e) => set('fixtureCenterline', e.target.checked)
              }), '在视图里画夹具/基准中心线')
          ),
          h(Field, { label: '说明' },
            h('input', {
              style: styles.input,
              placeholder: '如 基准A中心线，留空则不注',
              value: form.fixtureNote,
              onChange: (e) => set('fixtureNote', e.target.value)
            })
          ),

          /* 附加技术要求 */
          h(Field, { label: '附加条目' },
            h('textarea', {
              style: { ...styles.input, minHeight: 64, resize: 'vertical' },
              placeholder: '一行一条，会追加到技术要求末尾\n如：去毛刺、锐边倒钝',
              value: form.techNotes,
              onChange: (e) => set('techNotes', e.target.value)
            })
          ),

          /* 图纸信息 */
          h('div', { style: styles.sectionTitle }, '图纸信息'),
          h(Field, { label: '材质' },
            h('input', {
              style: styles.input,
              placeholder: '如 6061-T6',
              value: form.material,
              onChange: (e) => set('material', e.target.value)
            })
          ),
          h(Field, { label: '图纸名称' },
            h('input', {
              style: styles.input,
              value: form.drawingTitle,
              onChange: (e) => set('drawingTitle', e.target.value)
            })
          ),
          h(Field, { label: '图纸编号' },
            h('input', {
              style: styles.input,
              placeholder: '如 DW-2025-001',
              value: form.drawingNumber,
              onChange: (e) => set('drawingNumber', e.target.value)
            })
          ),
          h(Field, { label: '版本' },
            h('input', {
              style: styles.input,
              value: form.revision,
              onChange: (e) => set('revision', e.target.value)
            })
          ),

          /* 输出 */
          /* 保存位置 */
          h('div', { style: styles.sectionTitle }, '保存位置'),
          h('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 } },
            h('span', { style: { fontSize: 12, opacity: 0.75, flex: '0 0 76px' } }, '保存到'),
            h('input', {
              style: { ...styles.input, flex: 1, minWidth: 0 },
              placeholder: '点右边「浏览…」选文件夹；留空 = 模型旁边',
              value: form.outDir || '',
              onChange: (e) => set('outDir', e.target.value)
            }),
            h('button', {
              type: 'button',
              style: { ...styles.btnGhost, padding: '5px 12px' },
              onClick: () => pickOutDir()
            }, '浏览…')
          ),
          h('div', { style: { display: 'flex', gap: 10, alignItems: 'center', margin: '0 0 4px 84px' } },
            h('button', {
              type: 'button',
              style: { ...styles.btnGhost, padding: '3px 10px', fontSize: 12 },
              onClick: () => set('outDir', '')
            }, '恢复默认（模型旁）'),
            // ⚠ 这里所有 form.xxx 都要兜底：面板热更新时旧的 useState
            //   还带着「没有新字段」的 form，直接 .trim() 会抛
            //   TypeError，把整个面板渲染崩成黑屏（踩过一次）。
            file && h('span', { style: { fontSize: 11, opacity: 0.7 } },
              '会存到：' + (
                String(form.outDir || '').trim()
                  ? String(form.outDir || '').trim()
                  : '(模型所在目录)\\出图_' + String(file.name || 'model').replace(/\.[^.]+$/, '')
              ))
          ),

          h('div', { style: styles.sectionTitle }, '输出格式'),
          h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 16, marginBottom: 4 } },
            h('label', { style: styles.check },
              h('input', {
                type: 'checkbox',
                style: styles.cb,
                checked: form.exportDxf,
                onChange: (e) => set('exportDxf', e.target.checked)
              }), 'DXF'),
            h('label', { style: styles.check },
              h('input', {
                type: 'checkbox',
                style: styles.cb,
                checked: form.exportDwg,
                onChange: (e) => set('exportDwg', e.target.checked)
              }), 'DWG'),
            h('label', { style: styles.check, title: '用 AutoCAD 把视图搬进图框、加国标尺寸标注、填标题栏。关掉后视图会直接在 SolidWorks 里摆好，但不会有 AutoCAD 标注' },
              h('input', {
                type: 'checkbox',
                style: styles.cb,
                checked: form.acadDimension,
                onChange: (e) => set('acadDimension', e.target.checked)
              }), 'AutoCAD 后处理')
          ),
          h('div', { style: { fontSize: 11, opacity: 0.7, marginBottom: 8 } },
            form.acadDimension
              ? '视图会先摊到框外，再由 AutoCAD 搬回图框内并标注（推荐）'
              : '不开 AutoCAD：视图直接在 SolidWorks 里摆进图框，但没有国标标注')
          ,

          /* 装配体：整套图纸 */
          isAssemblyFile(file) && h('div', null,
            h('div', { style: styles.sectionTitle }, '装配体（整套图纸）'),
            h('label', { style: styles.check },
              h('input', {
                type: 'checkbox',
                style: styles.cb,
                checked: form.drawset,
                onChange: (e) => set('drawset', e.target.checked)
              }), '整套出图：装配总图 + 每个零件图 + 合并成一个 DXF'),
            h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 16, margin: '8px 0 4px 22px' } },
              h('label', { style: styles.check },
                h('input', {
                  type: 'checkbox',
                  style: styles.cb,
                  checked: form.bom,
                  onChange: (e) => set('bom', e.target.checked)
                }), '明细表 BOM'),
              h('label', { style: styles.check },
                h('input', {
                  type: 'checkbox',
                  style: styles.cb,
                  checked: form.balloons,
                  onChange: (e) => set('balloons', e.target.checked)
                }), '零件序号气泡')
            ),
            h('div', { style: { fontSize: 11, opacity: 0.7, marginBottom: 4 } },
              form.drawset
                ? '会先出装配总图，再逐件出零件图，最后合并成一个 DXF（多图框并排）。件多时较慢，看下方进度条'
                : '只出装配总图（含 BOM + 气泡），不拆零件图')
          ),

          /* 推荐用法：生成指令交给 AI 出图（不依赖插件 HTTP 通道） */
          h('div', { style: styles.sectionTitle }, '推荐：交给 AI 出图'),
          h('div', { style: { fontSize: 11.5, opacity: 0.75, marginBottom: 8, lineHeight: 1.7 } },
            '桌面应用的页面来源是 ',
            h('code', null, 'dsh-app://'),
            '，插件自己的 HTTP 接口在这里会被挂住，所以下面的「开始出图」按钮可能没反应。',
            h('br'),
            '更稳的做法：填好参数后点下面这个按钮，把生成的指令粘贴到对话框发给本鱼，',
            '本鱼用插件工具出图（这条路今晚已验证多次）。'
          ),
          h('div', { style: { display: 'flex', gap: 10, marginBottom: 8 } },
            h('button', {
              type: 'button',
              style: styles.btn(true, busy),
              disabled: busy,
              onClick: dispatchToAgent
            }, busy ? '发送中…' : '交给本鱼出图（自动发送）'),
            h('button', {
              type: 'button',
              style: styles.btn(false, false),
              onClick: () => { buildCommand(); setLog('指令已生成（未发送），可手动复制。') }
            }, '只生成指令')
          ),
          cmdText
            ? h('textarea', {
                readOnly: true,
                value: cmdText,
                style: {
                  ...styles.input, width: '100%', boxSizing: 'border-box',
                  minHeight: 120, resize: 'vertical', fontFamily: 'monospace',
                  fontSize: 11.5, marginBottom: 8
                },
                onFocus: (e) => e.target.select()
              })
            : null,

          /* 操作 */
          h('div', { style: styles.sectionTitle }, '或者：直接在本面板出图（HTTP 通道）'),
          h('div', { style: { marginTop: 10, display: 'flex', gap: 10 } },
            h('button', {
              style: styles.btn(true, busy || pdfBusy || !file),
              disabled: busy || pdfBusy || !file,
              onClick: build
            }, busy ? '出图中…' : '开始出图'),
            h('button', {
              style: styles.btn(false, busy || pdfBusy || !result?.dwg),
              disabled: busy || pdfBusy || !result?.dwg,
              onClick: printPdf,
              title: '按图纸最外层包围框取范围，黑白打印成 PDF'
            }, pdfBusy ? '打印中…' : '打印 PDF'),
            h('button', {
              style: styles.btn(false, busy || pdfBusy),
              disabled: busy || pdfBusy,
              onClick: () => { setLog(''); setResult(null); setPdfPath('') }
            }, '清空日志')
          ),

          /* 进度条：按引擎阶段推进，跑完 100%，失败停在出错那一步 */
          (progress || busy) && h('div', { style: styles.progressWrap },
            h('div', { style: styles.progressHead },
              h('span', null,
                progress?.error
                  ? '✗ 出图失败'
                  : (progress?.percent >= 100 ? '✓ 出图完成' : (progress?.label || '准备中'))),
              h('span', { style: { fontWeight: 600 } },
                `${Math.round(progress?.percent ?? 0)}%`)
            ),
            h('div', { style: styles.progressTrack },
              h('div', {
                style: styles.progressFill(
                  progress?.percent ?? 0,
                  Boolean(progress?.error))
              })
            ),
            progress?.stage && h('div', { style: styles.progressStage },
              `阶段：${progress.stage}`)
          ),

          /* 结果 */
          result && h('div', { style: { marginTop: 16 } },
            h('div', { style: styles.sectionTitle }, '输出文件'),
            h('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 } },
              ['drawing', 'dxf', 'dwg', 'preview']
                .filter((k) => result[k])
                .map((k) => h('div', { key: k, style: styles.fileChip },
                  h('span', { style: { opacity: 0.6 } }, k.toUpperCase()),
                  h('span', null, result[k])
                ))
            ),
            h('div', { style: styles.log },
              '尺寸 ' + (result.dimensions ?? 0) +
              ' 个｜去重 ' + (result.dedupeRemoved ?? 0) +
              '｜公差 ' + (result.tolerancesApplied ?? 0) + ' 处' +
              '\n溢出图框：' + (result.overflowOk ? '否' : '是') +
              '\n剖视图：' + (result.sectionView ? '已加' : '未加') +
              (result.sectionReason ? '（' + result.sectionReason + '）' : '')
            )
          ),

          /* PDF 结果 */
          pdfPath && h('div', { style: { marginTop: 14 } },
            h('div', { style: styles.sectionTitle }, 'PDF'),
            h('div', { style: styles.fileChip },
              h('span', { style: { opacity: 0.6 } }, 'PDF'),
              h('span', null, pdfPath)
            )
          ),

          /* 日志 */
          log && h('div', { style: { ...styles.log, marginTop: 14 } }, log)
        )
      )
    }

    /* ---------------- 侧边栏图标 ---------------- */
    function CadIcon() {
      return h('svg', {
        viewBox: '0 0 24 24',
        width: 20,
        height: 20,
        fill: 'none',
        stroke: 'currentColor',
        strokeWidth: 1.6,
        strokeLinejoin: 'round',
        strokeLinecap: 'round',
        'aria-hidden': true
      },
        // 图纸 + 尺寸标注的简化造型
        h('rect', { x: 3, y: 4, width: 18, height: 16, rx: 1.5 }),
        h('path', { d: 'M3 10h18' }),
        h('path', { d: 'M9 10v10' }),
        h('path', { d: 'M6 17.5h6' })
      )
    }

    /* DSH 客户端的会话发送服务。
       `ctx.conversation.send(text)` 会把 text 当作一条用户消息提交给当前会话，
       AI 收到后会自己去调插件工具 —— 这就是「插件自动调用 AI」的入口。
       （源码里就是 scopedSession('send').prompt([{type:'text',text}], 'queue')） */
    let conversationService = null

    return {
      inject: ['slots', 'conversation'],
      apply(ctx) {
        // 抓住会话发送服务，面板里的「交给 AI 出图」用它提交指令
        conversationService = ctx.conversation || null

        // 侧边栏图标
        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
          name: 'sidebar.panellist',
          id: PANEL_ID,
          order: 60,
          label: 'CAD 出图'
        }, CadIcon))

        // 主面板
        ctx.slots.inject('main', () => ctx.slots.register({
          name: 'main',
          key: PANEL_ID
        }, CadPanel))
      }
    }
  }
})
