/**
 * dsh-cad-draw —— SolidWorks 自动出工程图（Host 侧插件）
 *
 * 能力：模型 → GB 工程图 → DXF/DWG
 *   - 套 GB 图框（A4/A3/A2/A1/A0）
 *   - 投标准三视图 + 轴测图
 *   - 自动插入模型已有尺寸
 *   - 批量加尺寸公差（对称/双向/极限…）
 *   - 加 GB 粗糙度符号
 *   - 导出 DXF（默认）与 DWG
 *
 * 依赖本机：SOLIDWORKS + Python3.11 + pywin32
 */

import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { probe, resolvePython, runEngine } from './engine.js'
import { pickFile, pickFolder, registerCadApi } from './api.js'
import {
  deleteMethod,
  getMethod,
  loadMethods,
  renderMethods,
  upsertMethod
} from './methods.js'
import { listStandards, normalizeStandard, readStandard, standardChecklist } from './standards.js'

export const name = 'dsh-cad-draw'
export const inject = ['tools', 'systemPrompt']

const HERE = path.dirname(fileURLToPath(import.meta.url))

const DEFAULT_CONFIG = {
  paperSize: 'A3',
  landscape: true,
  exportDxf: true,
  exportDwg: false,
  views: ['front', 'top', 'right', 'isometric'],
  annotationStandard: 'GB',
  // 启动路径：留空 = 自动探测（注册表 → 常见安装位置）
  solidworksExe: '',
  autocadExe: '',
  preferredPython: ''
}

function fence(value) {
  return '<<<CAD_RESULT\n' + JSON.stringify(value, null, 2) + '\nCAD_RESULT>>>'
}

function textOut(text) {
  return { content: [{ type: 'text', text: String(text ?? '') }] }
}

function makeDef(def) {
  return {
    name: def.name,
    description: def.description,
    parameters: def.parameters,
    output: {
      schema: { type: 'object', properties: {}, additionalProperties: true },
      render(_args, value) {
        const content = value?.content
        if (typeof content === 'string') return [{ type: 'text', text: content }]
        if (Array.isArray(content)) {
          return content
            .filter((b) => b && b.type === 'text')
            .map((b) => ({ type: 'text', text: b.text }))
        }
        return [{ type: 'text', text: '' }]
      }
    },
    timeoutMs: def.timeoutMs ?? 20 * 60 * 1000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      if (exec?.signal?.aborted) throw new Error(`${def.name} 已取消`)
      return textOut(await def.run(args ?? {}, exec))
    }
  }
}

function buildTools(config) {
  const tools = []

  tools.push(makeDef({
    name: 'cad_draw_probe',
    description:
      '检查本机 CAD 出图环境：SolidWorks / AutoCAD 的**启动路径**是否找到、能否连接，GB 模板与图框是否存在、Python 是否就绪。' +
      '在真正出图前先跑一次可避免长时间等待。会用插件配置里的 solidworksExe / autocadExe（留空则自动探测）。',
    parameters: {
      type: 'object',
      properties: {
        solidworksExe: {
          type: 'string',
          description: 'SLDWORKS.exe 路径；留空用插件配置，再留空则自动探测'
        },
        autocadExe: {
          type: 'string',
          description: 'acad.exe 路径；留空用插件配置，再留空则自动探测'
        }
      },
      additionalProperties: false
    },
    timeoutMs: 3 * 60 * 1000,
    run: async (args) => {
      const info = await probe({
        solidworksExe: args.solidworksExe || config.solidworksExe || '',
        autocadExe: args.autocadExe || config.autocadExe || ''
      })
      info.pythonPath = resolvePython()
      return '环境探测结果：\n' + fence(info)
    }
  }))

  tools.push(makeDef({
    name: 'cad_draw_standard',
    description:
      '读取标注标准（GB / ISO / ASME）的完整规则正文。**做任何尺寸标注之前必须先调用本工具读一遍**，' +
      '按里面的硬性禁令和自查清单执行，不要凭记忆标注。不传 standard 时读插件配置里的默认标准。' +
      '返回内容包括：单位规则、直径符号、对称结构标法、封闭尺寸链禁令、尺寸布置要求、标注后自查清单。',
    parameters: {
      type: 'object',
      properties: {
        standard: {
          type: 'string',
          description: '标准：GB（中国国标）/ ISO（国际）/ ASME（美国）。默认取插件配置，未配置为 GB'
        },
        listOnly: {
          type: 'boolean',
          description: '只列可选标准清单、不返回规则正文，默认 false'
        }
      },
      additionalProperties: false
    },
    timeoutMs: 30 * 1000,
    run: async (args) => {
      if (args.listOnly) {
        return '可选标注标准：\n' + fence(listStandards())
      }
      const id = normalizeStandard(args.standard ?? config.annotationStandard)
      const got = readStandard(id)
      const head = fence({
        standard: got.meta.id,
        full: got.meta.full,
        diameterSymbol: got.meta.diameterSymbol,
        projection: got.meta.projection,
        units: got.meta.units,
        generalTolerance: got.meta.generalTolerance,
        file: got.path,
        chars: got.chars ?? 0,
        ok: got.ok
      })
      if (!got.ok) return `读取标准失败：\n${head}\n${got.text}`
      return (
        `已读取【${got.meta.label}】（${got.meta.full}）。\n` +
        '**以下规则在本次标注中必须逐条遵守，标注完成后按第 12 节自查清单逐项核对。**\n\n' +
        head + '\n\n---\n\n' + got.text
      )
    }
  }))

  tools.push(makeDef({
    name: 'cad_draw_build',
    description:
      '把一个 SolidWorks 模型(.sldprt/.sldasm)自动做成 GB 国标工程图，并导出 DXF。' +
      '会依次：套 GB 图框 → 投标准三视图+轴测图 → 自动插入模型尺寸 → 加公差 → 加粗糙度 → 导出 DXF。' +
      '注意：STEP/IGES 等中立格式不支持，需先另存为 .sldprt。',
    parameters: {
      type: 'object',
      properties: {
        model: {
          type: 'string',
          description: '模型文件的绝对路径（.sldprt 或 .sldasm）'
        },
        outDir: {
          type: 'string',
          description: '输出目录；留空则与模型同目录'
        },
        paperSize: {
          type: 'string',
          description: '图幅：A5 / A4 / A3 / A2 / A1 / A0，默认取插件配置。插件会按所选图幅自动计算比例'
        },
        landscape: {
          type: 'boolean',
          description: '是否横向，默认 true'
        },
        annotationStandard: {
          type: 'string',
          description:
            '标注标准：GB（中国国标，默认）/ ISO（国际）/ ASME（美国，第三角投影）。' +
            '标注前会用 cad_draw_standard 读取对应规则，直径符号、一般公差、投影法都随之切换'
        },
        solidworksExe: {
          type: 'string',
          description:
            'SLDWORKS.exe 的绝对路径；留空用插件配置，再留空则自动探测（注册表 → 常见安装位置）。' +
            '多版本共存或装在非默认盘时建议显式指定'
        },
        autocadExe: {
          type: 'string',
          description:
            'acad.exe 的绝对路径；留空用插件配置，再留空则自动探测。' +
            '可只给安装目录，插件会自动补 acad.exe'
        },
        views: {
          type: 'array',
          items: { type: 'string' },
          description:
            '要投的视图。可选 front/top/right/left/back/bottom/isometric。' +
            '默认 ["front","top","right","isometric"]（三视图+轴测，符合国标表达习惯）'
        },
        tolerance: {
          type: 'object',
          properties: {
            enabled: { type: 'boolean', description: '是否加公差' },
            type: {
              type: 'string',
              description: '公差类型：symmetric(对称) / bilateral(双向) / limit(极限) / none'
            },
            plus: { type: 'number', description: '上偏差(mm)，如 0.05' },
            minus: { type: 'number', description: '下偏差(mm)，填正数会自动取负' },
            onlyOver: {
              type: 'number',
              description: '只给线性值 >= 该毫米数的尺寸加公差，默认 0(全部)'
            }
          },
          additionalProperties: false,
          description: '尺寸公差设置'
        },
        surfaceFinish: {
          type: 'object',
          properties: {
            enabled: { type: 'boolean', description: '是否加粗糙度符号' },
            value: { type: 'string', description: '粗糙度 Ra 值，如 "3.2"、"1.6"' },
            x: { type: 'number', description: '放置位置 X(米)，默认 0.15' },
            y: { type: 'number', description: '放置位置 Y(米)，默认 0.06' }
          },
          additionalProperties: false,
          description: 'GB 表面粗糙度符号'
        },
        technicalRequirements: {
          type: 'object',
          properties: {
            generalToleranceLevel: {
              type: 'string',
              description: '未注公差等级：f/m/c/v（对应 GB/T 1804-f/m/c/v），空字符串则不写'
            },
            unnotedFillet: { type: 'string', description: '未注圆角，如 "R1"；留空不写' },
            unnotedChamfer: { type: 'string', description: '未注倒角，如 "C0.5"；留空不写' },
            unnotedRoughness: { type: 'string', description: '未注粗糙度，如 "Ra3.2"；留空不写' },
            surfaceTreatment: {
              type: 'string',
              description: '表面处理，如 阳极氧化 / 镀锌 / 发黑；留空不写'
            },
            extraNotes: {
              type: 'string',
              description: '附加技术要求，一行一条，追加到末尾'
            }
          },
          additionalProperties: false,
          description: '技术要求条目。用户按需填，留空的项自动跳过'
        },
        fits: {
          type: 'object',
          properties: {
            target: { type: 'string', description: '配合尺寸，如 "φ5.6"' },
            code: { type: 'string', description: '公差代号，如 "H7"、"h6"' }
          },
          additionalProperties: false,
          description: '配合公差：给某个尺寸加公差代号，如 Ø5.6 的孔标 H7。不传则不标'
        },
        fixture: {
          type: 'object',
          properties: {
            enabled: { type: 'boolean', description: '是否画夹具/基准中心线' },
            note: { type: 'string', description: '中心线说明，如 "基准A中心线"' }
          },
          additionalProperties: false,
          description: '夹具/基准中心线。用户有需求时才开'
        },
        exportDxf: { type: 'boolean', description: '是否导出 DXF，默认 true' },
        exportDwg: { type: 'boolean', description: '是否同时导出 DWG，默认 false' },
        acadDimension: {
          type: 'boolean',
          description:
            '是否用 AutoCAD 后处理（把视图搬回图框内 + 国标尺寸标注 + 填标题栏），默认 true。' +
            '关掉后视图会直接在 SolidWorks 里摆进图框，但没有 AutoCAD 那套标注'
        },
        drawset: {
          type: 'boolean',
          description:
            '【仅装配体 .SLDASM 有效】整套图纸模式：先出装配总图（视图+明细表+零件序号），' +
            '再为每个零件出一张零件图，最后合并成一个 DXF（多个图框并排）。' +
            '件多时很慢（每张 1~4 分钟），建议先确认进度条在走'
        },
        bom: {
          type: 'boolean',
          description: '装配图是否自动插入明细表（BOM），默认 true'
        },
        balloons: {
          type: 'boolean',
          description: '装配图是否自动加零件序号（气泡），默认 true'
        },
        partDimension: {
          type: 'boolean',
          description: '整套图纸模式下，每张零件图是否也走 AutoCAD 标注，默认 true'
        },
        mergeSheets: {
          type: 'boolean',
          description: '整套图纸模式下，是否把各张图合并成一个 DXF，默认 true'
        },
        preview: { type: 'boolean', description: '是否导出 PNG 预览图，默认 true' },
        visible: { type: 'boolean', description: '是否显示 SolidWorks 界面，默认 true' }
      },
      required: ['model'],
      additionalProperties: false
    },
    run: async (args) => {
      const standard = normalizeStandard(args.annotationStandard ?? config.annotationStandard)
      // 整套图纸：只有装配体才走 drawset（零件没有「每个零件」可拆）
      const isAsm = /\.sldasm$/i.test(String(args.model || ''))
      const wantSet = Boolean(args.drawset) && isAsm
      const payload = {
        action: wantSet ? 'drawset' : 'build',
        model: args.model,
        outDir: args.outDir,
        paperSize: args.paperSize ?? config.paperSize,
        landscape: args.landscape ?? config.landscape,
        views: args.views ?? config.views,
        annotationStandard: standard,
        solidworksExe: args.solidworksExe || config.solidworksExe || '',
        autocadExe: args.autocadExe || config.autocadExe || '',
        tolerance: args.tolerance,
        surfaceFinish: args.surfaceFinish,
        technicalRequirements: args.technicalRequirements,
        fits: args.fits,
        fixture: args.fixture,
        exportDxf: args.exportDxf ?? config.exportDxf,
        exportDwg: args.exportDwg ?? config.exportDwg,
        acadDimension: args.acadDimension ?? true,
        preview: args.preview ?? true,
        visible: args.visible ?? true,
        // 装配体相关
        bom: args.bom ?? true,
        balloons: args.balloons ?? true,
        // 整套图纸：每张零件图是否也走 AutoCAD 标注
        partDimension: args.partDimension ?? true,
        mergeSheets: args.mergeSheets ?? true
      }

      const { result, stages, stderr, code } = await runEngine(payload)

      if (!result) {
        const tail = (stderr || '').slice(-1500)
        return `CAD 出图失败（引擎未返回结果，exit=${code}）\n${tail ? '错误输出：\n' + tail : ''}`
      }
      if (!result.ok) {
        return `CAD 出图失败：${result.error ?? '未知错误'}\n阶段：\n` +
          fence(stages.slice(-6))
      }
      return 'CAD 出图完成：\n' + fence({
        model: result.model,
        annotationStandard: standard,
        standardMeta: standardChecklist(standard),
        drawing: result.drawing,
        dxf: result.dxf,
        dwg: result.dwg,
        preview: result.preview,
        dimensions: result.dimensions,
        tolerancesApplied: result.tolerancesApplied,
        surfaceFinish: result.surfaceFinish,
        views: result.views,
        sizes: result.saved
      })
    }
  }))

  tools.push(makeDef({
    name: 'cad_draw_method',
    description:
      '方法存档：插件内置的「已验证出图流程」手册，随插件一起发布（换设备也带着）。\n' +
      '**出图前先 action:"read" 读一遍当前最佳方法，照着做**；\n' +
      '**一次出图成功后，必须用 action:"save" 把这次真正跑通的步骤总结成一条方法**——\n' +
      '把踩到的坑（API 顺序、参数、避让规则）写进 notes，下次直接复用。\n' +
      'action: list=列出全部 / read=读某条或当前最佳 / save=新增或更新 / delete=删除。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['list', 'read', 'save', 'delete'],
          description: 'list / read / save / delete'
        },
        name: {
          type: 'string',
          description: '方法标识（read/delete 用；save 留空会自动生成）'
        },
        title: { type: 'string', description: '一句话标题（save 用）' },
        whenToUse: { type: 'string', description: '什么时候用这条方法' },
        steps: {
          type: 'string',
          description: '步骤，一行一条（save 用）。要写成可复现的操作序列'
        },
        notes: {
          type: 'string',
          description: '易错点/踩坑记录（save 用），这是最值钱的部分'
        },
        improvement: {
          type: 'string',
          description:
            '本次相对上一版的改进点（更新已有方法时必填），例如「标注并行走流水线，13 张图从 6 分钟降到 3 分钟」'
        },
        verified: {
          type: 'boolean',
          description: '是否已验证跑通过（save 用，默认 true）'
        },
        tags: {
          type: 'array',
          items: { type: 'string' },
          description: '标签，便于检索'
        }
      },
      required: ['action']
    },
    async run(args) {
      const action = String(args.action || 'list')
      if (action === 'list') {
        const data = loadMethods()
        const list = (data.methods || []).map((m, i) =>
          `${i + 1}. ${m.title}（${m.name}）` +
          `${m.verified === false ? ' · 未验证' : ' · 已验证'}` +
          ` · ${(m.steps || []).length} 步` +
          (m.verifiedAt ? ` · ${m.verifiedAt}` : '')
        )
        return (
          `方法存档：${(data.methods || []).length} 条（lib/methods.json）\n` +
          (list.join('\n') || '（空）')
        )
      }
      if (action === 'read') {
        if (args.name) {
          const m = getMethod(String(args.name))
          if (!m) throw new Error('没有这条方法：' + args.name)
          return (
            `# ${m.title}（${m.name}）\n` +
            (m.whenToUse ? `适用：${m.whenToUse}\n` : '') +
            `验证：${m.verified === false ? '未验证' : '已验证 ' + (m.verifiedAt || '')}\n\n` +
            (m.steps || []).map((s, i) => `${i + 1}. ${s}`).join('\n') +
            (m.notes ? `\n\n易错点：${m.notes}` : '')
          )
        }
        return (renderMethods({ maxChars: 20000 }) || '（方法存档为空）')
      }
      if (action === 'save') {
        if (!args.title && !args.steps) {
          throw new Error('save 至少要给 title 或 steps')
        }
        const r = upsertMethod({
          name: args.name,
          title: args.title,
          whenToUse: args.whenToUse,
          steps: args.steps,
          notes: args.notes,
          verified: args.verified,
          improvement: args.improvement,
          tags: args.tags
        })
        if (!r.ok) throw new Error('写入失败：' + r.error)
        return (
          `已存入方法存档：${r.method.name}\n` +
          `文件：${r.file}\n` +
          `当前共 ${r.total} 条方法。下次出图插件会自动把它注入给 AI。`
        )
      }
      if (action === 'delete') {
        if (!args.name) throw new Error('delete 需要 name')
        const r = deleteMethod(String(args.name))
        return (
          `已删除 ${args.name}（${r.removed} 条），当前剩 ${r.total} 条。文件：${r.file}`
        )
      }
      throw new Error('未知 action：' + action)
    }
  }))

  tools.push(makeDef({
    name: 'cad_draw_pick',
    description:
      '弹出 Windows 原生对话框，让用户选**模型文件 / 输出目录 / 软件程序**，返回绝对路径。\n' +
      '为什么要这个工具：桌面应用的页面来源是 dsh-app://，插件面板直接调 HTTP 接口会被挂住，' +
      '但**工具调用这条路是可靠的**。所以「选路径」和「出图」建议由 AI 用工具完成，面板只负责收集参数。\n' +
      '模型（.sldasm 装配体）必须用 kind=model 选**原始位置**的文件：拖拽上传只是复制一个副本，' +
      '同目录零件引用会断，开出来是空装配体。',
    parameters: {
      type: 'object',
      properties: {
        kind: {
          type: 'string',
          enum: ['model', 'folder', 'exe'],
          description: 'model=选模型文件；folder=选输出目录；exe=选 SLDWORKS.exe / acad.exe'
        },
        initial: {
          type: 'string',
          description: '对话框的初始目录，可留空'
        }
      },
      required: ['kind']
    },
    async run(args) {
      const kind = String(args.kind || 'model')
      const initial = String(args.initial || '')
      if (kind === 'folder') {
        const r = await pickFolder(initial)
        if (!r.ok) throw new Error(r.error || '选择目录失败')
        return {
          ok: true,
          kind,
          path: r.path || '',
          canceled: Boolean(r.canceled),
          note: r.canceled ? '用户取消了选择' : '已选目录'
        }
      }
      const r = await pickFile(initial, kind)
      if (!r.ok) throw new Error(r.error || '选择文件失败')
      return {
        ok: true,
        kind,
        path: r.path || '',
        canceled: Boolean(r.canceled),
        note: r.canceled ? '用户取消了选择' : '已选文件'
      }
    }
  }))

  tools.push(makeDef({
    name: 'cad_draw_pdf',
    description:
      '一键把工程图 DWG 打印成 PDF。打印范围自动取图纸里**最外层的那个包围框**（面积最大的闭合多段线），' +
      '所以即使包围框被隐藏/冻结也能正确识别。输出默认黑白（monochrome.ctb），DWG 里的图层颜色原样保留。' +
      '注意：AutoCAD 的 PlotToFile 是异步的，本工具会轮询等 PDF 落盘（通常 20–30 秒），别中途取消。',
    parameters: {
      type: 'object',
      properties: {
        dwg: {
          type: 'string',
          description: '要打印的 DWG 绝对路径（通常是 cad_draw_build 的产物）'
        },
        pdf: {
          type: 'string',
          description: 'PDF 输出路径；留空则与 DWG 同名同目录'
        },
        paper: {
          type: 'string',
          description: '图幅 A0/A1/A2/A3/A4/A5；留空则按最外层包围框尺寸自动判定'
        },
        monochrome: {
          type: 'boolean',
          description: '是否黑白输出（monochrome.ctb），默认 true。GB 图纸应为黑白'
        },
        autocadExe: {
          type: 'string',
          description: 'acad.exe 路径；留空用插件配置，再留空则自动探测'
        }
      },
      required: ['dwg'],
      additionalProperties: false
    },
    timeoutMs: 6 * 60 * 1000,
    run: async (args) => {
      const { result, stages, stderr, code } = await runEngine(
        {
          action: 'pdf',
          ...args,
          autocadExe: args.autocadExe || config.autocadExe || ''
        },
        { timeoutMs: 5 * 60 * 1000 }
      )
      if (!result) {
        return `打印失败（引擎未返回结果，exit=${code}）\n` + (stderr || '').slice(-1200)
      }
      const lines = []
      for (const st of stages) {
        if (st?.stage === 'pdf_log' && st.msg) lines.push(st.msg)
      }
      if (result.ok) {
        lines.push('PDF 打印完成：')
        lines.push(fence({
          pdf: result.pdf,
          size: result.size,
          page_mm: result.page_mm,
          paper: result.paper,
          box: result.box,
          seconds: result.seconds,
          monochrome: args.monochrome ?? true
        }))
      } else {
        lines.push('PDF 打印失败：' + (result.error ?? '未知错误'))
        if (result.box) lines.push('最外层框：' + JSON.stringify(result.box))
      }
      return lines.join('\n')
    }
  }))

  tools.push(makeDef({
    name: 'cad_draw_close',
    description: '关闭 SolidWorks 里所有文档（不结束进程）。批量出图后释放资源用。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    timeoutMs: 2 * 60 * 1000,
    run: async () => {
      const { result } = await runEngine({ action: 'close' }, { timeoutMs: 120_000 })
      return result?.ok ? '已关闭所有文档。' : `关闭失败：${result?.error ?? '未知'}`
    }
  }))

  return tools
}

export function apply(ctx, config = {}) {
  const merged = { ...DEFAULT_CONFIG, ...config }

  const log = (level, message) => {
    try {
      ctx.logger?.[level]?.(`[dsh-cad-draw] ${message}`)
    } catch {
      /* 忽略 */
    }
  }

  log('info', `引擎 ${existsSync(path.join(HERE, 'engine.py')) ? '就绪' : '缺失'}，Python=${resolvePython()}`)

  /* 浏览器面板用的 HTTP API。
     ⚠ 必须在 apply 里**直接调用**，不能包在 ctx.effect 里：
       ctx.effect(() => registerCadApi(...))   // ❌
     因为 registerCadApi 内部要用 ctx.inject(['webServer'], ...)，
     而 ctx.inject 属于「apply 阶段的依赖声明」，在 effect 回调里调用会抛错；
     这个错又被 registerCadApi 自己的 try/catch 吞掉，退回 ctx.get('webServer')
     轮询 —— 而轮询在桌面 profile 下永远取不到服务，路由就永远没挂上，
     HTTP 请求于是**一直挂着不返回**（表现：面板探测/浏览全卡死）。 */
  const disposeApi = registerCadApi(ctx, { log })
  if (typeof disposeApi === 'function') {
    ctx.effect(() => disposeApi)
  }

  ctx.effect(() => {
    const disposers = []
    for (const tool of buildTools(merged)) {
      try {
        disposers.push(ctx.tools.register(tool))
      } catch (error) {
        log('warn', `工具注册失败 ${tool.name}: ${error?.message ?? error}`)
      }
    }
    return () => {
      for (const dispose of disposers) {
        try {
          dispose()
        } catch {
          /* 忽略 */
        }
      }
    }
  })

  ctx.effect(() =>
    ctx.systemPrompt.section({
      name: 'dsh-cad-draw',
      order: 620,
      text: [
        '## CAD 自动出图（dsh-cad-draw）',
        '',
        '当用户要把 SolidWorks 模型做成工程图 / DXF 时，用 cad_draw_build，不要手写脚本。',
        '',
        '### 软件启动路径',
        '- `cad_draw_build` / `cad_draw_pdf` / `cad_draw_probe` 都支持 `solidworksExe`（SLDWORKS.exe）和 `autocadExe`（acad.exe）。',
        '- 留空时按「插件配置 → 注册表 → 常见安装位置」自动探测；侧边栏「CAD 出图」面板里有对应输入框和「检测路径」按钮。',
        '- 多版本共存、装在非默认盘、或 COM 连错版本时，**让用户填这两个路径**，比排查注册表快。',
        '- `cad_draw_probe` 只报告路径与运行状态，**不会启动** SolidWorks/AutoCAD（避免点一下卡一分钟）。',
        '- `cad_draw_pick` 弹 Windows 原生对话框选模型/目录/程序（kind=model|folder|exe），拿到绝对路径。',
        '- **桌面端面板的 HTTP 接口不可靠**（页面来源 dsh-app://，插件接口会被挂住）。',
        '  用户报「探测不到 / 浏览没反应」时，让他在对话里说需求、由你用工具完成。',
        '- 装配体必须用 cad_draw_pick(kind=model) 选**原始位置**，否则零件引用会断、开出来是空装配体。',
        '',
        '### 标注标准（强制流程）',
        '',
        '**做任何尺寸标注之前，必须先调用 `cad_draw_standard` 读一遍所选标准的规则正文**，按里面的禁令和自查清单逐条执行，不要凭记忆标注。',
        '',
        `可选标准（插件配置默认：${merged.annotationStandard ?? 'GB'}）：`,
        '- `GB` —— 中国国标 GB/T 4458.4-2003，直径用 **φ**，第一角投影，一般公差 GB/T 1804-m',
        '- `ISO` —— 国际标准 ISO 129-1:2018，直径用 **⌀**，第一角投影，一般公差 ISO 2768-m',
        '- `ASME` —— 美国标准 ASME Y14.5-2018，直径用 **⌀**，**第三角投影**，理论正确尺寸加方框，单位必须在标题栏声明',
        '',
        '标注流程：`cad_draw_standard` 读标准 → 标 → 按标准末节的自查清单逐项复核 → 有不合规项就改。',
        '',
        '### 出图规则',
        '- 支持 .sldprt / .sldasm，**也支持 STEP / IGES / Parasolid**（插件会自动导入并另存 .sldprt 再出图）。',
        '- 但导入实体**没有设计尺寸**，所以 STEP/IGES 只能出视图、自动插入尺寸为 0；要标尺寸得按几何自己量。',
        '- 默认出三视图+轴测图、A3 横向框架、毫米单位、DXF 输出。',
        '- 公差与粗糙度需要用参数显式给：tolerance{type,plus,minus,onlyOver}、surfaceFinish{value}。',
        '',
        '### 装配体（.SLDASM）',
        '- 默认会自动插**明细表(BOM)** 和 **零件序号(气泡)**；可用 bom / balloons 关掉。',
        '- `drawset: true` = **整套图纸**：先出装配总图，再为每个零件出一张零件图，',
        '  最后合并成一个 DXF（多个图框并排）。件多时很慢（每张 1~4 分钟），',
        '  会通过 drawset_begin / drawset_sheet_done 阶段推进度（第 N/M 张）。',
        '- 用户说「整个装配体拖进去出多个图」「一套图纸」时，就用 drawset。',
        '',
        '### 打印 PDF',
        '- 用 `cad_draw_pdf`（或面板上的「打印 PDF」按钮）。范围自动取图纸**最外层包围框**，即使框被隐藏也能识别。',
        '- 默认黑白输出（monochrome.ctb），DWG 里图层颜色原样保留，只有打印结果变纯黑。',
        '- AutoCAD 的 PlotToFile 是异步的，工具会轮询等 PDF 落盘（约 20–40 秒），别中途取消，也别重复调用。',
        '',
        '### 按需填写的可选需求（留空自动跳过，不要替用户编）',
        '- `technicalRequirements`：技术要求 —— generalToleranceLevel(f/m/c/v 未注公差)、unnotedFillet(未注圆角)、unnotedChamfer(未注倒角)、unnotedRoughness(未注粗糙度)、surfaceTreatment(表面处理)、extraNotes(附加条目一行一条)。',
        '- `fits`：配合公差 —— {target:"φ5.6", code:"H7"}，给指定尺寸加公差代号；用户没指定就留空。',
        '- `fixture`：夹具/基准中心线 —— {enabled:true, note:"基准A中心线"}；用户明确需要才开。',
        '- 这些项在侧边栏「CAD 出图」面板里都有对应输入框，用户填了才生效；空值不写进图，避免出现空条目。',
        '',
        '### 其他',
        '- 剖视图默认 auto：只有模型存在切除/孔类特征（有内腔）时才自动加；也可显式 sectionView:true/false 覆盖。',
        '- 要看清内部结构时，让视图显示隐藏线（swHIDDEN_GREYED=1），DWG 里会出 HIDDEN 线型虚线，据此标内腔尺寸。',
        '- 局部放大图需显式给 detailView{scale,radius}，不是默认项。',
        '- 出图慢是正常的（SolidWorks 启动+重建），单件约 1–4 分钟，别中途重复调用。',
        '- **自动标注完成后必须多次复核**：按标准文件末节的自查清单逐项过，至少 3 轮（特征覆盖 → 重复/封闭链 → 方位/侵入/压线），有问题立刻改并重跑审计。',
        '- 浏览器侧「CAD 出图」面板（左侧栏图标）可拖文件、选标准、填参数。',
        '',
        '### 方法存档（出图前必读）',
        '插件内置了一份「已验证出图流程」手册（`lib/methods.json`，随插件发布，换设备也在）。',
        '- **出图前**：先 `cad_draw_method({action:"read"})` 读出当前最佳方法，照着它执行；',
        '  已有适配方案时不要自己另起流程。',
        '- **出图成功后**：必须调用 `cad_draw_method({action:"save", ...})` 把这次真正跑通的',
        '  步骤和踩到的坑沉淀下来（`notes` 写易错点最有价值）。下次出图插件会自动注入给你。',
        '- 失败的尝试别存成「已验证」；可以 `verified:false` 存成草稿。',
        '',
        '**更新规则（硬性）**：如果后续找到**更好/更快**的出图方式，**必须更新同一条方法**',
        '（`action:"save"` 传同一个 `name` 覆盖，不要并列留两条），并用 `improvement` 写清改进点。',
        '方法存档里的 `version` 会自动加一，上一版压进 `history` 便于回溯。',
        '',
        renderMethods({ maxChars: 5000 })
          || '（方法存档暂为空，出图成功记得用 cad_draw_method 存一条）'
      ].join('\n')
    })
  )
}
