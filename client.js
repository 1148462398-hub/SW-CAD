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
      btn: (primary, disabled) => ({
        padding: '9px 18px',
        fontSize: 13,
        fontWeight: 500,
        borderRadius: 7,
        border: primary ? 'none' : '1px solid var(--dsw-alias-border-secondary, #d0d0d0)',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.55 : 1,
        background: primary
          ? 'var(--dsw-alias-brand-primary, #247bbf)'
          : 'transparent',
        color: primary ? '#fff' : 'inherit'
      }),
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
        exportDwg: true
      }
    }

    function Field(props) {
      return h('div', { style: styles.row },
        h('div', { style: styles.label }, props.label),
        props.children
      )
    }

    /* ---------------- 主面板 ---------------- */
    function CadPanel() {
      const [form, setForm] = useState(defaultForm)
      const [file, setFile] = useState(null)
      const [over, setOver] = useState(false)
      const [busy, setBusy] = useState(false)
      const [pdfBusy, setPdfBusy] = useState(false)
      const [pdfPath, setPdfPath] = useState('')
      const [log, setLog] = useState('')
      const [result, setResult] = useState(null)
      const inputRef = useRef(null)

      const set = useCallback((key, value) => {
        setForm((prev) => ({ ...prev, [key]: value }))
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
        setFile(f)
        setLog('')
        setResult(null)
        // 图纸名称默认取文件名
        if (!form.drawingTitle) {
          set('drawingTitle', name.replace(/\.(sldprt|sldasm)$/i, ''))
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
        setLog('正在调用 SolidWorks 出图，单件约 1–4 分钟，请勿重复点击…\n')
        setResult(null)
        try {
          const payload = {
            model: file.path || file.name,
            outDir: '',
            paperSize: form.paperSize,
            annotationStandard: form.annotationStandard,
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
          setBusy(false)
        }
      }, [file, form])

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
            body: JSON.stringify({ dwg, monochrome: true })
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
          h('div', { style: styles.sectionTitle }, '输出格式'),
          h('div', { style: { display: 'flex', gap: 16, marginBottom: 4 } },
            h('label', { style: styles.check },
              h('input', {
                type: 'checkbox',
                checked: form.exportDxf,
                onChange: (e) => set('exportDxf', e.target.checked)
              }), 'DXF'),
            h('label', { style: styles.check },
              h('input', {
                type: 'checkbox',
                checked: form.exportDwg,
                onChange: (e) => set('exportDwg', e.target.checked)
              }), 'DWG')
          ),

          /* 操作 */
          h('div', { style: { marginTop: 20, display: 'flex', gap: 10 } },
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

    return {
      inject: ['slots'],
      apply(ctx) {
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
