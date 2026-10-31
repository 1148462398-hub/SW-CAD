# dsh-cad-draw

SolidWorks 模型 → GB 国标工程图 → DXF/DWG，全自动。

一个 [DeepSeek Harness](https://github.com/deepseek-ai) 插件：
拖进 `.sldprt` / `.sldasm`（也支持 STEP / IGES），自动套国标图框、投三视图+轴测图、
显示隐藏线、按 GB/ISO/ASME 标准标注，最后一键打印成 A3 黑白 PDF。

## 更新日志

### v0.11.0（当前）

**架构调整：AI 工具当主力，面板退为「参数收集器」。**

用户提出：「完全靠插件代码很难做到检测路径和出图，能否少量调用 AI 模型来帮助，
插件只要提供绘图的方法来指导模型就行」。

这个判断是对的，而且有实测依据：

| 通道 | 状态 | 证据 |
|---|---|---|
| **插件工具**（`cad_draw_probe` / `cad_draw_build`） | ✅ 一直可靠 | probe 0.13s 返回；今晚出过 13 张整套图纸 |
| **面板 HTTP 接口** | ❌ 桌面端会挂住 | 日志证明请求进了处理器，但回包出不来 |

所以新增两件东西：

**① 新工具 `cad_draw_pick`**

```jsonc
cad_draw_pick({ kind: "model" | "folder" | "exe", initial: "D:\\" })
// → { ok, path, canceled }
```

弹的是**真正的 Windows 原生对话框**（`OpenFileDialog` / `FolderBrowserDialog`），
走工具通道调用 —— 这条路今晚从没失败过。

**② 面板新增「推荐：交给 AI 出图」**

- 按钮「生成给本鱼的指令（复制）」：把当前面板所有参数拼成一条指令，复制到剪贴板
- 生成的文本可以直接粘到对话框
- 原来那排「开始出图」按钮保留，但明确标注「HTTP 通道，可能无响应」

生成的指令长这样：

```text
用 cad_draw_build 出图：
  model: "D:\5寸机\装配体1.SLDASM"
  outDir: "D:\5寸机\出图_装配体1"
  paperSize: "A3"
  annotationStandard: "GB"
  views: ["front", "top", "right"]
  landscape: true
  bom: true
  balloons: true
  drawset: true
```

**为什么这样分工合理**

- 参数收集、路径选择、图幅/标准这些都交给面板（点选式，用户直观）
- 真正需要「判断」的部分（选哪个文件、弹窗、看图验证、错了重试）交给 AI
- 底层出图仍然 100% 是插件代码（`engine.py` + SolidWorks/AutoCAD COM），AI 只做调度

### v0.10.1
### v0.10.1（当前）

**加诊断细目日志 + 25 秒看门狗**（不再无限转圈）。

从 `%TEMP%\dsh-cad-draw-api.log` 已经确认：**路由注册是通的、请求进了处理器**：

```
18:01:00Z GET  /dsh-cad-draw/api/probe
18:01:04Z POST /dsh-cad-draw/api/pick-file
18:01:20Z GET  /dsh-cad-draw/api/probe
```

所以卡点在处理器**内部**，不在注册。加了四级日志把卡点钉死：

```
ENTER   GET /dsh-cad-draw/api/probe
  probe: 命中，开始探测
  probe: 探测完成 313ms，回包
EXIT    GET /api/probe 320ms
```

| 日志停在哪一行 | 说明 |
|---|---|
| 只有 `ENTER` | 路由分发前就卡了 |
| `ENTER` + `命中`，没有「完成」 | **卡在 `await probe()` 里**（引擎/子进程） |
| 有「完成」没有 `EXIT` | 卡在 `sendJson`（`res` 不是 Node 响应对象） |
| 有 `WATCHDOG 强制回 504` | 25 秒没回包，被看门狗兜住 |

看门狗会在 25 秒时强制返回 504 并附日志路径，界面至少能给出明确报错，
而不是一直转圈让人以为「探测不到」。

### v0.10.0
### v0.10.0（当前）

**面板接口「一直挂着不返回」的最终根因：`ctx.inject` 被包在 `ctx.effect` 里调用。**

```js
ctx.effect(() => registerCadApi(ctx, { log }))    // ❌ 错
```

`registerCadApi` 内部要调 `ctx.inject(['webServer'], ...)`，
而 `ctx.inject` 是 **apply 阶段的依赖声明**，在 effect 回调里调用会抛错；
这个错又被 `registerCadApi` 自己的 try/catch 吞掉，静默退回
`ctx.get('webServer')` 轮询 —— 而轮询在桌面 profile 下永远取不到服务。

于是路由**从来没挂上**，但 webServer 的 prefix 匹配仍然把请求交给了
一个不存在的处理器，请求就**一直挂着**：
- 浏览器：`net::ERR_UNEXPECTED` / 一直 pending
- 命令行实测：`/api/progress`、`/api/probe` 全部超时（8 秒无响应）
- 而同一时刻插件工具 `cad_draw_probe` **1 秒返回** → 证明宿主和引擎都好的

修正：

```js
const disposeApi = registerCadApi(ctx, { log })     // ✅ apply 里直接调
if (typeof disposeApi === 'function') ctx.effect(() => disposeApi)
```

**另外加了诊断日志**，下次再出问题一眼就能定性：

```
%TEMP%\dsh-cad-draw-api.log
```

- 文件为空 → 路由没注册上（注册问题）
- 有内容但请求超时 → 处理器内部卡住（引擎/COM 问题）

离线自检：

```
工具数: 5
路由: ["/dsh-cad-draw","/api/dsh-cad-draw"]
```

### v0.9.9
### v0.9.9（当前）

**找到面板接口「请求一直挂着」的根因：路由压根没注册上。**

排查过程（值得记下来）：

1. 用户截图：`dsh-app://app/dsh-cad-draw/api/probe` →
   `net::ERR_UNEXPECTED`；`pick-file` → 404。
2. 查 DSH 桌面端 `protocol.handle(SCHEME)` 源码，确认 `dsh-app://app/*`
   除前端资源外**全部带鉴权 cookie 转发给宿主 HTTP**
   （`return forwardWebRequest(request, hostUrl, hostCookie)`）。
   → 所以**相对路径本来就是对的**，我上一版改成
   `http://127.0.0.1:<端口>` 是错的方向，已回滚。
   （实测宿主 `/` 需要鉴权，直接访问返回 401 / ERR_FAILED，
   但插件自己的 `/dsh-cad-draw/api/*` 路由是通的。）
3. 直接请求 `/dsh-cad-draw/api/progress` 和 `/api/probe`，
   **两个都超时**（`/` 却秒回 401）→ 说明 HTTP 服务活着，
   但请求进了宿主后没有任何响应。
4. 同一时刻调插件工具 `cad_draw_probe`：**1 秒返回**，
   两个路径都探测到了 → 宿主和引擎完全正常，**只是路由没挂上**。

根因：我用的是 `ctx.get('webServer')` 200ms 轮询（取不到就重试），
在桌面 profile 下始终取不到，路由永远没注册。
DSH 自己的插件（client-modules / client-connection）都是官方写法：

```js
ctx.inject(['webServer'], (webCtx) => {
  webCtx.effect(() => webCtx.webServer.register({
    kind: 'prefix', path, handler
  }), 'label')
})
```

改成这个写法后，离线自检确认两条路由都注册成功：

```
ctx.inject 调用, deps = ["webServer"]
工具数: 5
注册的路由: ["/dsh-cad-draw","/api/dsh-cad-draw"]
```

另外加了一条**别名路由** `/api/dsh-cad-draw`：万一桌面端只转发 `/api/*`
前缀，这个别名能保证接口仍然可达。

⚠ 这个改动在 `lib/api.js`（宿主侧），**必须重启 DSH** 才生效。

### v0.9.8
### v0.9.8（当前）

**找到「探测不到 / 浏览加不进去」的真正原因：桌面端的 URL 协议不对。**

用户 DevTools 截图给出了决定性证据：

```
Failed to load resource: net::ERR_UNEXPECTED
  dsh-app://app/dsh-cad-draw/api/probe
  dsh-app://app/dsh-cad-draw/api/pick-file   → 404
```

桌面应用的页面来源是 `dsh-app://app/`，而我们写的是相对路径
`API = '/dsh-cad-draw'`。于是请求被解析成
`dsh-app://app/dsh-cad-draw/api/probe` —— **桌面协议不转发插件注册的 HTTP 路由**：
- GET `/api/probe` → `net::ERR_UNEXPECTED`（协议层直接失败）
- POST `/api/pick-file` → 404

所以这两个按钮怎么点都没反应。**和 `defaultForm` 那个 bug 是两个独立问题**，
上一版修的是状态，这一版修的是传输。

实测宿主 HTTP 端口还带鉴权（未带凭证直接请求返回 **401**），
所以桌面端也无法简单拼绝对地址。

**改法**

`resolveApi()` 按来源自动选基地址：

| 页面来源 | 用的基地址 | 说明 |
|---|---|---|
| `http(s)://` | 同源 `/dsh-cad-draw` | 浏览器里打开 GUI，最稳，同源免鉴权 |
| `dsh-app://` 且 boot 里有端口 | `http://127.0.0.1:<端口>/dsh-cad-draw` | 自动挖端口 |
| `dsh-app://` 且挖不到端口 | 面板顶部显示**红色提示** | 告诉用户改用浏览器地址 |

**当前正确用法：把 `http://127.0.0.1:<端口>` 复制到浏览器打开**，
在那个页面里用「CAD 出图」面板 —— 同源请求，探测 / 浏览 / 出图都能通。

三种环境实测（mock React 真实渲染）：

```
浏览器 (http):              144 节点  警告横幅=false
桌面端 dsh-app + boot含端口: 144 节点  警告横幅=false
桌面端 dsh-app + boot无端口: 152 节点  警告横幅=true
```

### v0.9.7
### v0.9.7（当前）

**修「软件启动路径添加不进去 + 自动探测半天没结果」。**

根因是一个很隐蔽的错误：`defaultForm` 其实是**函数**——

```js
function defaultForm() { return { views: [...], ... } }   // ← 函数
const [form, setForm] = useState(defaultForm)             // 靠 lazy initializer 调用，没问题
```

但 v0.9.4 里我为了「热更新补默认字段」写成了：

```js
const form = { ...defaultForm, ...rawForm }   // ❌ 展开一个函数 = {}
```

展开函数只得到 `{}`，**所有默认字段（包括 `views`）全部丢失**。
于是：输入框回填空白、`form.views.includes()` 直接抛错、
整个面板状态错乱（表现为「填了也加不进去」）。

正确写法：

```js
const DEFAULTS = defaultForm()          // 先调用
const form = { ...DEFAULTS, ...rawForm }
```

**另外加了 localStorage 持久化**（用户填的路径不该因为刷新/热更新丢）：

- 表单变化防抖 300ms 存 `dsh-cad-draw.form`
- 探测结果存 `dsh-cad-draw.probe`，重挂载时先用缓存渲染
- 挂载时与 `defaultForm()` 合并，并对 `views` 做数组兜底

实测三种场景（mock React 真实渲染）：

```
全新（无缓存）:    OK 144 节点  exe 输入框 = (空) | (空)
旧缓存缺 views:    OK 144 节点  exe 输入框 = X | (空)
完整缓存:          OK 144 节点  exe 输入框 = \SOLIDWORKS\SLDWORKS.exe | sk\AutoCAD 2018\acad.exe
```

**关于「自动探测半天没探测到」**：实测 `probe()` 本身只要 **0.13 秒**，
两个路径都能找到：

```
solidworks: F:\sw\SOLIDWORKS\SLDWORKS.exe            exists=true
autocad:    C:\Program Files\Autodesk\AutoCAD 2018\acad.exe  exists=true
```

慢的假象同样来自 state 丢失 —— 探测结果到了但渲染没留住。
现在结果会缓存，且打开面板立刻先用缓存显示。

### v0.9.6
### v0.9.6（当前）

**软件启动路径也改成浏览选择**（用户要求：SolidWorks / AutoCAD 的 exe 也要
用资源管理器那种方式选，不要手打）。

- `POST /api/pick-file` 新增 `kind` 参数：`model` / `exe` / `all`
- 面板「软件启动路径」两行各加「浏览…」按钮，弹原生 `OpenFileDialog`，
  过滤器 `可执行程序|*.exe`，初始目录取自动探测到的安装目录
- 现在面板一共有 3 个「浏览…」：SolidWorks / AutoCAD / 输出目录

**顺带修掉两个 PowerShell 中文坑（都是实测踩的）**

1. **必须用 `-EncodedCommand`，不能用 `-Command`**：
   `-Command` 按控制台代码页解码参数，脚本里的中文（对话框标题、文件筛选器）
   会变成乱码，PowerShell 直接报
   「表达式或语句中包含意外的标记」/「字符串缺少终止符」。
   改成 `Buffer.from(ps, 'utf16le').toString('base64')` + `-EncodedCommand`。

2. **必须设 `[Console]::OutputEncoding = UTF8`**：
   PowerShell 用 OEM 代码页写 stdout，Node 按 UTF-8 读，
   于是用户选到 `D:\5寸机\自动出图` 这种中文路径时，返回的路径是乱码，
   后面拼文件路径必然失败。实测加这一行后中文路径完好。

**关于「不依赖 AI 模型」**：插件的出图链路本来就是纯代码——
面板按钮 → `POST /api/build` → `engine.py` → SolidWorks COM → AutoCAD COM → DXF/DWG，
全程没有 AI 参与。AI 只在用户于对话框里下指令时才通过 `cad_draw_build` 工具调它，
两者是同一条后端。

### v0.9.5
### v0.9.5（当前）

**只保留一套标注：全部交给 AutoCAD，SolidWorks 不再插模型尺寸。**

用户指出：「发现标注怎么还是在 SolidWorks 里标注，之前不是试过在 AutoCAD 里标注吗」。
查下来是**两套同时在跑**：

```
· 插入标注 7 个，尺寸 7 个          ← SolidWorks 标的
· AutoCAD 标注：外形 6、直径 4、线性 12   ← AutoCAD 又标一遍
```

图上于是出现两套尺寸互相压。现在规则改成：

- `acadDimension: true`（默认）→ SolidWorks 那段**整段跳过**，只留 AutoCAD
- 只有显式 `swDimension: true` 才让 SolidWorks 标（不用 AutoCAD 时）

日志会显示「标注：交给 AutoCAD 做（…）」。

**其余修复**

1. **保存路径改成资源管理器式选择**：面板「保存到」右侧「浏览…」按钮，
   调 `POST /api/pick-folder`，宿主用 PowerShell 弹原生
   `FolderBrowserDialog`（TopMost，不会被浏览器盖住）。

2. **装配体必须选原始路径**（这是装配图一直出不好的真因）：
   拖拽只能把 `.SLDASM` **一个文件**复制到 uploads，同目录的 21 个零件
   引用全断，SolidWorks 打开是空装配体。实测对比：

   | | 拖拽（副本） | 选原始路径 |
   |---|---|---|
   | 明细表 | **2 行** | **13 行** |
   | 零件序号 | 失败 | 4 个 |
   | 视图图层 | **全 0** | 233 / 192 / 215 |
   | AutoCAD 标注 | **0** | 6 / 4 / 12 |

   新增 `POST /api/pick-file` + 面板「选择模型文件（装配体请用这个）」
   按钮；拖入 `.sldasm` 时会弹黄色警告说明原因。

3. **标注数量收紧**（装配图几十个孔全标会糊成一团黑）：
   - 同直径的孔只标一处，前缀 `N×`（实测 `10×φ25.40`）
   - 直径上限：零件 10、装配体 4（原来 62 个）
   - 线性尺寸上限：零件 6、装配体 2（原来每个方向 7 个）
   - 装配体标注总数 110 → 25

4. **线宽归一成 GB 的 2:1**：SolidWorks 导出轮廓 0.70 + 我们细线 0.25
   是 2.8:1，粗细对比过头。新增 `normalize_lineweights()`，
   轮廓统一 0.50、虚线/中心线/尺寸线 0.25。

5. **DXF 导出改为同一会话内完成**：之前是标注完关掉再 `Documents.Open`
   重新开，拿到失效 COM 对象，报 `<unknown>.SaveAs`。现在趁 doc 还活着
   直接 `SaveAs(dxf, 61)`，再 `SaveAs(dwg)` 存回，实测稳定。

### v0.9.4
### v0.9.4（当前）

**修「拖进去界面黑了」**。

原因：面板热更新后，`useState(defaultForm)` 里保存的还是**旧对象**，
里面没有 v0.9.3 新加的 `outDir` 字段。而「会存到：」那行写的是

```js
form.outDir.trim()      // ← undefined.trim() 抛 TypeError
```

React 渲染时抛异常 → 整棵子树崩 → 面板黑屏。
之所以「一拖就黑」，是因为这行被 `file && ...` 挡着，
只有拖入文件（`file` 变真）才会执行到。

修法两层：

1. 取值全部兜底：`String(form.outDir || '').trim()`、`value: form.outDir || ''`
2. **根上防住**：组件顶部每次渲染都合并一次默认值

```js
const [rawForm, setForm] = useState(defaultForm)
const form = { ...defaultForm, ...rawForm }   // ★ 新增字段自动补上
```

以后再加字段，热更新也不会因为「旧 state 缺字段」炸掉。

验证：用 mock React 把组件真实渲染一遍（模拟旧 form + 已拖入 .SLDASM）——
145 个节点全部正常，「装配体（整套图纸）」「会存到」都在。

⚠ 这个改动在 `client.js`，需要**页面强制刷新（Ctrl+F5）**才生效。

### v0.9.3
### v0.9.3（当前）

用户实测提的三个问题，全部修复：

**① 没有保存路径选择** → 面板新增「保存位置」区块

- 输入框可填绝对路径
- 留空 = 模型旁边的 `出图_<模型名>` 文件夹
- 下面实时显示「会存到：…」
- 有「恢复默认（模型旁）」按钮
- 出图日志第一行就打印输出目录

**② DXF 应该是 AutoCAD 画的，不是 SolidWorks 导出的**

这是真 bug：`save_and_export` 在 **AutoCAD 加工之前**就导出了 DXF，
所以那份 DXF 里没有标注、视图没搬进图框、没有中心线。
用户打开它自然「什么都没有」。

新增 `acad_plot.export_dxf_from_dwg()`：AutoCAD 加工完
→ 打开 DWG → `SaveAs(dxf, 61)` → `Close(False)`
（SaveAs 只改文档关联名，磁盘上的 DWG 不受影响）。

实测对比（同一个零件「机架」）：

| | 旧 DXF（SW 导出） | 新 DXF（ACAD 导出） |
|---|---|---|
| 大小 | 77 KB | **192.2 KB** |
| `DSH_DIM` | **0 次** | **90 次** |
| `DSH_CENTER` | 0 | 15 |
| `DSH_VIEW_1/2/3` | 0 | ✓ |

装配体实测：21 实例 → A2 → BOM 13 行 → 气泡 4 个 →
中心线 192 条 → 标注 103 个 → **DXF 690 KB / 110 个 DIMENSION 实体**。

**③ 装配图「出了 SolidWorks 图就停了，AutoCAD 里啥都没有」**

其实 AutoCAD **跑了**（DWG 里确实有 DSH_VIEW/DSH_DIM/DSH_CENTER），
但两个原因让用户以为停了：

1. **DXF 是加工前的**（见 ②），用户打开 DXF 看不到任何 AutoCAD 内容
2. **日志把新阶段全隐藏了** —— `bom` / `balloons` / `acad_*` 这些
   阶段在 `api.js` 的 `label` 映射表里没有对应项，`if (!fn) continue`
   直接跳过，所以界面上从「已保存」直接跳到「收尾」，看起来像中断

已补齐 30+ 个阶段的中文日志：
`装配体：21 个零件实例` / `图幅自动升级：A3 → A2` /
`明细表：已插入 13 行` / `零件序号：生成 4 个` /
`AutoCAD 分图层：FRAME=68 BOM=5 VIEW_1=233 …` /
`AutoCAD 中心线：96 个圆心，画了 192 条点画线` /
`AutoCAD 标注：外形 6、直径 62、线性 35` /
`AutoCAD 重新导出 DXF：✓ 690 KB`

### v0.9.2
### v0.9.2（当前）

**按 GB 修正线型与尺寸标注**（用户：「线段和尺寸线标注有没有按规范」）。

| 项 | 修前 | 修后 | GB 依据 |
|---|---|---|---|
| 尺寸线/界线线宽 | 9 (0.09mm) | **25 (0.25mm)** | 细实线 b/2 |
| 界线离开轮廓 | 1.0mm | **2.0mm** | GB/T 4458.4 ≥2 |
| 箭头大小 | 3.5 (=字高) | **2.49 (≈2/3 字高)** | 箭头长 2~5mm |
| 尺寸数字颜色 | 256 (ByBlock) | **7 显式** | ByBlock 单色打印会变白 |
| 中心线 | **完全没有** | **DSH_CENTER 细点画线** | GB/T 4457.4 |

新增 `draw_centerlines()`：对每个视图里的圆/圆弧圆心画一横一竖点画线，
两端超出轮廓 3mm。实测机架零件：7 个圆心 → 14 条中心线。

修掉三个「烘进实体、改系统变量无效」的坑：

- **尺寸文字高度**：实体 TextHeight 是 **0.18mm**（不是 3.5），
  打印出来数字完全看不见——尺寸线箭头都在就是没字。
  必须逐个实体 `e.TextHeight = text_h`（实测修正 12 个）。
- **小数位**：`DIMDEC=2` 但实体显示 4 位（93.4010）。
  必须逐个设 `e.PrimaryUnitsPrecision`（整数→0，非整数→2）。
- **文字颜色 ByBlock**：单色打印解析成白色，白字打白纸。

⚠ 尚未解决：**尺寸数字被尺寸线穿过**（GB 明令禁止）。
需要按每个尺寸单独挪文字位置，留待下个版本。

### v0.9.1
### v0.9.1（当前）

**装配图排版修复**：明细表不再压在视图上。

用户给的方向是对的：「BOM 也是包围框的思路，给外边缘做包围框，
然后 BOM 独立图层，避免和工程图图框以及视图重叠」。已按这个实现：

- 新增 **`DSH_BOM` 独立图层**：落在明细表包围框内的图元全部划进去
- 布局算法 `_place_avoiding()`：视图按**包围框**避让明细表和彼此
  （先试原位 → 上推 → 左移 → 全图网格搜索找空位）
- **自动升图幅**：装配体零件 ≥10 且原定 A3 时，自动升到 A2
  （实测 12 零件的无人机：13 行明细表 180×103mm，
  A3 剩下的空间装不进 102×126 的视图，布局只能 clamped）

实测（`D:\5寸机\装配体1.SLDASM`，21 实例 / 12 种零件）：

```
[assembly]          components=21
[paper_escalated]   A3 → A2（零件太多）
[bom]               ok=true rows=13  box=(409,75)-(589,178)
[acad_layers]       DSH_BOM=5  DSH_VIEW_1/2/3
[acad_layout_by_layer]
   DSH_VIEW_1  (171.6, 260.6)  original ✓
   DSH_VIEW_2  (419.8, 252.2)  up ✓
   DSH_VIEW_3  (339.8, 260.6)  left ✓
[done]              ok=true
```

修复的三个真 bug：

1. **`DSH_BOM` 图层从没被创建** —— `e.Layer='DSH_BOM'` 抛异常被吞，
   计数永远是 0、图元永远留在 0 层。代码看着完全正确，就是不动。
2. **标题栏规则缺 `target is None` 判断** —— 把已经判成 DSH_BOM 的
   明细表图元又改回 DSH_TITLE（两者都在右下角）。
3. **图层分类的「就近兜底」无条件抢图元** —— 图框分区线、标题栏线
   全被塞进视图图层，把视图包围框撑成「高 340mm」（A3 只有 297），
   视图再也摆不进图框。现在要求图元必须和「视图框外扩 15mm」相交。

另修：`AutoBalloon4` 返回的是 COM 对象数组，直接放进结果会在
`json.dumps` 时报 `Object of type CDispatch is not JSON serializable`，
现在只留数量。

### v0.9.0
### v0.9.0（当前）

**整套图纸**：拖一个 `.SLDASM` 进去，一次出「装配总图 + 每个零件图 + 合并 DXF」。

```jsonc
cad_draw_build
  model: "D:/xxx/总装.SLDASM"
  drawset: true        // 整套模式
  bom: true            // 明细表
  balloons: true       // 零件序号气泡
```

流程：

```
① 解析装配体（GetComponents → 去重零件路径）
② 装配总图：视图 + 明细表(BOM) + 零件序号(气泡)   → 总装.dwg
③ 每个零件：视图 + 尺寸 + 公差 + 粗糙度           → 零件A.dwg ...
④ 合并：全部并进一个 DXF（多图框并排）             → 总装_整套图纸.dxf
```

实测（2 个零件的装配体）：

```
[drawset_plan]       共 3 张图
[drawset_sheet_done] 1/3 装配总图        ok
[drawset_sheet_done] 2/3 20250107-4-4.5  ok
[drawset_sheet_done] 3/3 GPS - 副本      ok
[drawset_merge_done] 3 张，总宽 1300mm   ok
→ 测试装配_整套图纸.dxf  392.5 KB，块引用 3 个
```

本期还打通了**零件序号**，关键就一个参数：

```python
# SelectByID2 的 Callout 必须是「显式的空 IDispatch 指针」
VARIANT(pythoncom.VT_DISPATCH, None)
# 传 None / pythoncom.Empty / 0 全都报「类型不匹配 (None, 8)」
```

另外 **IView 对象上根本没有 Select / Select2 方法**，只能走 SelectByID2。

进度条现在会显示「正在出第 2/3 张：零件图 · GPS - 副本」。

### v0.8.0
### v0.8.0（当前）

**多图合成一个 DXF**（新增 `lib/acad_merge.py`）——整套图纸的基础能力。

用户要的是「多个 CAD 图在一个 DXF 文件里，多放几个工程图图框」。
SolidWorks 的 `NewSheet2/3/4` 在这台机器上 `TemplateIn` 参数一直报
`类型不匹配 (None, 3)`（空串 / 模板路径 / pythoncom.Empty 全试过），
所以改走 **AutoCAD 合并**路线：每张图单独出 DWG，再并进一个 DXF。

```python
from acad_merge import merge_dwgs
merge_dwgs([dwg1, dwg2, ...], 'out.dxf', gap_mm=20)
# -> {'ok': True, 'sheets': 2, 'widthMm': 860}
```

实测：两张 A3（420 宽）并排 → X 0~944.9mm，输出 223.7 KB，
DXF 里就是两个块引用（`测试装配`、`GPS - 副本`），每块一个完整图框。

踩过的三个坑：

- **`CopyObjects` 跨文档复制失败** ——
  传 Python list 报「对象数组无效」；包成 VARIANT 数组又报「对象不在数据库中」；
  用 SelectionSet 也还是「对象不在数据库中」。
  最后改用 `ModelSpace.InsertBlock(点, <dwg路径>, 1,1,1, 0)` ——
  块名给外部 DWG 路径等价于整图插入，一次成功。
- **`SaveAs('x.dxf')` 存出来是 `x.dxf.dwg`** ——
  只给路径时 AutoCAD 一律按 DWG 存，必须显式传 `acSaveAsType`
  （61 = ac2013_dxf，24 = ac2013_dwg）。
- `ss.Select(5, 0,0,0,0)` 选不到东西 —— 全选模式**只接受一个参数** `ss.Select(5)`。

还没做的编排：拖入一个 `.SLDASM` → 自动出「装配总图 + 每个零件图」→ 合并成一个 DXF。
各块都已单独验证可用，差把它们串起来。

### v0.7.0
### v0.7.0（当前）

**支持装配图**：`.SLDASM` 现在会走装配图流程，自动插入**明细表（BOM）**。

- `is_assembly` 判定 + `assembly` 阶段（报告零件实例数）
- 新增 `insert_bom_table()`：在第一个视图上插 GB 明细表，位置在标题栏正上方
- 新增 `bom` / `bomTemplate` / `bomAnchor` / `bomType` 参数

关键 API 坑（从 `sldworks.tlb` 读出来的真实签名）：

```python
IView::InsertBomTable3(
    UseAnchorPoint: bool,   # ← 第一个参数不是模板路径！
    X: float, Y: float,     # 米
    AnchorType: int,        # 0左上 1右上 2左下 3右下
    BomType: int,           # 0仅零件 1仅顶层 2缩进
    Configuration: str,
    TableTemplate: str,     # .sldbomtbt
    Hidden: bool)
```

按旧文档把模板路径塞第一个参数，只会报 `类型不匹配。 (None, 1)`。

另修两个 bug：

- **`open_model` 打不开 `.SLDDRW`** —— 原来只区分 `.sldasm` 与「其它=零件」，
  图纸也按零件类型打开，返回 None，报错却是 `'NoneType' object has no attribute 'GetTitle'`。
- **明细表伸出图框** —— `AnchorType=3` 时 X 给的是左下角，表格向右铺。
  给 315mm 时整体铺到 483mm（A3 只有 420）。改成 `pw - 0.185` 后包围框正好 `[0,0,420,297]`。

⚠ 还没做：**零件序号（气泡）**。`IDrawingDoc::AutoBalloon4` 的 10 个参数已确认，
但它要求先选中视图，`SelectByID2` 的 Callout 参数类型还没试对，目前返回 None。

### v0.6.5
### v0.6.5（当前）

- **打通 FeatureWorks 的 COM 绑定**（新增 `lib/featureworks.py`）。
  这是 STEP 特征识别的前置条件，之前一直卡在「拿不到方法」：

  | 问题 | 原因 | 解法 |
  |---|---|---|
  | `Dispatch('FeatureWorks.FeatureWorksApp')` 报「找不到指定的模块」 | 注册表里 `InprocServer32` 是相对路径 `.\fworks\fworks.dll` | 在 SW 里 `sw.LoadAddIn(<SW目录>\fworks\fworks.dll)` |
  | `GetAddInObject` 拿到 `<COMObject <unknown>>`，`dir()` 为空 | `IFeatureWorksApp` 是 **vtable 接口**，不是纯 IDispatch | 用 **comtypes**：`GetAddInObject(...).QueryInterface(mod.IFeatureWorksApp)` |
  | comtypes `GetActiveObject` 报 `MK_E_UNAVAILABLE` | ROT 里没有 | SW 是单实例服务，`CreateObject` 就拿到运行中的实例 |
  | 调用卡住 874 秒 | 识别时弹「swbrowser.sldedb 遗失」模态框 | 后台线程自动按回车点掉 |

  另外修了整个环境：注册表写 `Toolbox Data Location = C:\SOLIDWORKS Data (3)\`
  但**该目录不存在**，导致 Toolbox / FeatureWorks 数据库一直缺。
  已把 `swbrowser.sldedb` 和 `lang` 补到该目录。

  `recognize()` 已能跑通到「调用成功、返回结果」，
  但三个测试零件都返回 0 个特征，还需继续排查（见 README 末尾）。

### v0.6.4
### v0.6.4（当前）

- **出图进度条** —— 面板「开始出图」下面显示百分比进度。
  `/api/build` 是跑完才返回的，所以新增 `GET /api/progress`，
  引擎每吐一个阶段就更新内存里的进度（`engine.js` 里 40+ 个阶段的百分比映射），
  面板每 800ms 轮询一次画进度条；完成 100%，失败停在出错那一步并标红。
- **用完就关** —— 模型零件导出后立即在 SolidWorks 里关掉（`model_closed` 阶段），
  AutoCAD 里的 DWG 标注完也关掉（`acad_closed` 阶段）。
  只关本文档，不动进程，也不碰用户自己开的图。可用
  `closeModelAfter` / `closeAcadAfter` 关掉这个行为。

### v0.6.3
### v0.6.3（当前）

- **修 `name 'ensure_running' is not defined`** —— `engine.py` 的 `connect()` 调用少写了模块前缀，
  出图一开始就崩。这是所有出图路径都会踩的致命错误。
- **主按钮看不清** —— 按钮原来用主题变量 `--dsw-alias-brand-primary` 当底色 + 白字，
  某些主题下这个变量很浅，白字压上去完全看不见。改成固定深蓝 `#1f6fb2` + 显式白字，
  禁用态用灰底深字。复选框也补了 `accentColor`。
- **「打印 PDF」找不到包围框** —— 原来只认闭合多段线，图框用 4 条 LINE 画时就报
  「找不到最外层包围框（没有闭合多段线）」。现在兜底顺序：
  闭合多段线 → `DSH_FRAME` 图层范围 → 全部几何范围。
- **投影视图设位置无效** —— `Create1stAngleViews2` 建的视图带对齐约束，
  直接设 `Position` 后非基准视图会被拉回原位（实测一个跑到 y=-104mm）。
  现在先 `RemoveAlignment()` 再设位置。
- 新增 `acadDimension` 开关（面板复选框 + 工具参数，默认开）。
  关掉后视图直接在 SolidWorks 里摆进图框，不走 AutoCAD 搬动。

### v0.6.2


**修「把图拖进去却没有输出」** —— 报错 `[WinError 3] 系统找不到指定的路径。: ''`。

根因：浏览器里 `File` 对象**没有本地路径**（`file.path` 只有 Electron 才有），
面板把 `file.name`（只有文件名、没有目录）直接当成 `model` 传给引擎，
引擎 `os.path.dirname('某某.SLDPRT')` 得到 `''`，`os.makedirs('')` 就炸了。

修法：

- 新增 `POST /dsh-cad-draw/api/upload?name=xxx`，面板出图前先把文件字节传到本机落盘
  （`文档\dsh-cad-draw\uploads\`，同名加时间戳不覆盖），再把**绝对路径**传给引擎
- Electron/桌面端若已有 `file.path` 则跳过上传，省一次拷贝
- 引擎加防呆：`model` 不是绝对路径时直接给人类可读的错误，不再抛 `WinError 3`

### v0.6.1


**修「自动检测路径检测不到」** —— 探测逻辑本身没问题（接口实测返回正确路径），
是面板把结果只写进日志区、输入框一直是空的，看起来像没检测到。现在：

- 打开面板**自动探测一次**，结果显示在每个输入框正下方（来源 / 是否运行 / 实际路径）
- 输入框 placeholder 直接显示探测到的路径
- 按钮改名为「重新检测」，随时可重跑
- 没找到时红字提示「✗ 没找到 … → 请在上面填完整路径」

### v0.6.0

**可配置 SolidWorks / AutoCAD 启动路径。**

多版本共存、装在非默认盘、或 COM 连错版本时，自动探测经常不准。现在：

- 侧边栏面板新增「软件启动路径」两个输入框 + 一个「检测路径」按钮
- `cad_draw_build` / `cad_draw_pdf` / `cad_draw_probe` 都支持 `solidworksExe` / `autocadExe` 参数
- 探测优先级：**手动指定 → 注册表 → 常见安装位置**
- `autocadExe` 可以只填安装目录，插件会自动补 `acad.exe`
- 「检测路径」只报告状态、**不会启动**程序（避免点一下卡一分钟）

### v0.5.1

修复两个实测出来的 bug：

- **同心圆的直径标注全叠在一条水平线上** —— `AddDimDiametric` 的弦点被硬编码成
  `(cx-r, cy) → (cx+r, cy)`，导致 φ56 和 φ13 的尺寸线完全重合，根本分不清哪个是哪个。
  现在改成：按圆心分组 → 同组内角度按 `360/n` 均分 → 引线长度按「本组最外圆半径 + 净距」
  计算，让所有直径文字落在最外圆之外的同一圈上，互不遮挡。
- **`open_doc` 返回的对象不可用** —— 晚绑定下 `acad.Documents.Open(...)` 拿到的对象
  不暴露完整 Document 接口（访问 `.ModelSpace` 报 `AttributeError: Open.ModelSpace`），
  必须改取 `acad.ActiveDocument`。

### v0.5.0

- **一键打印 PDF**：按图纸最外层包围框取范围，默认黑白（`monochrome.ctb`），
  DWG 里图层颜色保留、只有输出变纯黑。AutoCAD 的 `PlotToFile` 是异步的，工具会轮询等落盘。
- **STEP / IGES / Parasolid 支持**：自动 `LoadFile4` 导入 → 另存 `.sldprt` → 重开 → 出图。
  导入实体没有设计尺寸，所以只能出视图、自动尺寸为 0。

### v0.4.0

- 侧边栏新增**技术要求 / 配合公差 / 夹具中心线**输入框，留空自动跳过，不替用户编造。
- 自动标注强制**多轮复核**（特征覆盖 → 重复/封闭链 → 方位/侵入/压线）。

### v0.3.0

- 内置**标注标准库**：GB / ISO / ASME，标注前强制先读规则正文。
- 新增工具 `cad_draw_standard`。

## 安装

本插件是 DSH 的本地插件（`link:` 依赖）。在 DSH profile 的 `package.json` 里加：

```json
{
  "dependencies": {
    "dsh-cad-draw": "link:/path/to/dsh-cad-draw"
  },
  "dsh": {
    "profile": {
      "bundles": ["dsh-cad-draw"]
    }
  }
}
```

装完**重启一次 DSH 宿主**（Host 侧工具和侧边栏面板都是启动时注册的）。

## 环境要求

| 依赖 | 说明 |
|---|---|
| **SolidWorks 2020+** | 通过 COM 自动化，需已登录许可 |
| **AutoCAD 2018+** | 标注后处理与 PDF 打印，需有 `DWG To PDF.pc3` |
| **Python 3.11** + `pywin32` | 引擎脚本 |
| DSH ≥ 0.2 | 插件宿主 |

## 软件启动路径（v0.6.0）

面板里两个输入框，留空则自动探测：

| 字段 | 填什么 | 说明 |
|---|---|---|
| `solidworksExe` | `F:\sw\SOLIDWORKS\SLDWORKS.exe` | SLDWORKS.exe 绝对路径 |
| `autocadExe` | `C:\Program Files\Autodesk\AutoCAD 2018\acad.exe` | 可只填安装目录，自动补 `acad.exe` |

探测顺序：**手动指定 → 注册表 → 常见安装位置**。

```jsonc
// 或写在插件配置里（dsh/cordis.patch.yml）
config:
  solidworksExe: 'F:\sw\SOLIDWORKS\SLDWORKS.exe'
  autocadExe: 'C:\Program Files\Autodesk\AutoCAD 2018\acad.exe'
```

显式路径的价值：先按路径把进程拉起来，再 `Dispatch` 连运行中的实例。
多版本共存时不会连错版本，绿色版 / 非默认盘也能用。

「检测路径」按钮只报告**找到没、在不在跑**，不会启动程序——
否则点一下就卡 30~60 秒，很容易让人以为插件挂了。

## 能力

| 步骤 | 自动做的事 |
|---|---|
| 1 | 打开 `.sldprt` / `.sldasm`（STEP/IGES 会自动导入并另存） |
| 2 | 用 GB 模板新建工程图，套 GB 图框（A4/A3/A2/A1/A0，横/纵） |
| 3 | 单位强制毫米（模板自带英寸，不改会显示成 0.04 这种） |
| 4 | 投标准三视图 + 轴测图（`*前视` `*上视` `*右视` `*等轴测`） |
| 5 | 视图摊到图框外、拉开间距，再按国标位置移回框内 |
| 6 | 自动插入模型已有尺寸 |
| 7 | 批量加尺寸公差（对称/双向/极限） |
| 8 | 加 GB 粗糙度符号 |
| 9 | 保存 SLDDRW，导出 **DXF**（默认）+ DWG |
| 10 | 一键打印 A3 黑白 **PDF** |


## 用之前先探测环境

```
cad_draw_probe
```

会报 SolidWorks 版本、GB 模板/图框是否存在、Python 路径。

## 出图

```
cad_draw_build
  model: "D:\2.5无人机\上电池架3030.SLDPRT"
  paperSize: "A3"
  views: ["front","top","right","isometric"]
  tolerance: { enabled: true, type: "symmetric", plus: 0.05, onlyOver: 5 }
  surfaceFinish: { enabled: true, value: "3.2" }
  exportDxf: true
  exportDwg: true
```

输出到模型同目录，或 `outDir` 指定。

## 重要限制（都已实测，不是没做）

- **STEP / IGES / Parasolid 不能直接进**：`OpenDoc6` 会让 SolidWorks 进程崩溃（RPC 0x800706BE）。请先在 SolidWorks 里另存为 `.sldprt`。
- **剖视图 / 局部放大图无法纯 API 生成**：必须先画剖切线/放大圆并选中，而 `SelectByID2` 在 pywin32 晚绑定下报「类型不匹配」（DISP_E_TYPEMISMATCH）。需要在 SolidWorks 里手工补，或用预置好剖视图的自定义模板。
- **标题栏不会自动填**：SolidWorks 自带的 `a3 - gb.slddrt` 标题栏是**纯静态文字**（已逐个读注释验证，75 个注释里没有任何属性占位符），所以写自定义属性不会显示出来。要自动填「材质 / 图纸名称 / 图纸编号 / 版本」，得用带属性链接的自定义图框。
- **自动尺寸只来自模型里已有的尺寸**：模型里没标过的尺寸不会凭空出现，需要模型本身标了、或勾了「标记为图纸」。

## 环境依赖

- SOLIDWORKS（本机 2020 SP3，`F:\sw\SOLIDWORKS`）
- Python 3.11 + `pywin32`（`C:\Users\123\AppData\Local\Programs\Python\Python311`）
- 许可服务 `SolidWorks Flexnet Server` 必须运行

许可服务如果起不来，用 `scripts\run-fix-license.cmd`（需管理员）——它会把服务从安装包目录迁到稳定路径 `C:\Program Files\SolidWorks_Flexnet_Server` 并设开机自启。

## 踩过的坑（给后来人）

1. 视图名必须本地化，`*Front` 返回 None 不报错。
2. `InsertModelAnnotations3` 在 `IDrawingDoc` 上，不在 `IView`；Types 必须是 `8|32768=32776`。
3. 同一张图纸重复调用标注会返回 None（只生效一次）。
4. 单位必须在建视图**之前**改。
5. 强杀 SolidWorks 会留「错误报告」模态框卡死后续自动化；已自动关闭（`Disable Crash Diagnosis=1`），且引擎只 `CloseDoc` 不杀进程。

---

## 标注标准（v0.3.0 新增）

插件内置三套标注标准，**每次标注前强制先读规则正文**，避免凭记忆标注。

| 标准 | 依据 | 直径符号 | 投影法 | 一般公差 |
|---|---|---|---|---|
| **GB**（默认） | GB/T 4458.4-2003 尺寸注法 | φ | 第一角 | GB/T 1804-m |
| **ISO** | ISO 129-1:2018 | ⌀ | 第一角 | ISO 2768-m |
| **ASME** | ASME Y14.5-2018 | ⌀ | **第三角** | 标题栏声明 |

规则正文在 `lib/standards/{gb,iso,asme}.md`，每份包含：硬性禁令、单位规则、直径/半径/
对称结构标法、重复要素简化注法、尺寸布置要求、标注后自查清单。

### 用法

- 工具：`cad_draw_standard({standard:"GB"|"ISO"|"ASME"})` → 返回该标准完整规则
- 出图：`cad_draw_build({model, annotationStandard:"GB", ...})`
- 面板：侧边栏「CAD 出图」里有「标注标准」下拉框
- 配置：`annotationStandard` 默认值（缺省 `GB`）

### 模型侧强制流程

系统提示词已写入：**做任何尺寸标注之前，必须先调用 `cad_draw_standard` 读一遍**，
按规则里的禁令和自查清单逐条执行。引擎启动时也会回显 `annotation_standard` 阶段，
确认实际生效的标准，避免"选了 ISO 却按 GB 标"。

### 为什么要有这个

同一张图换个标准就要换标注法：直径符号 φ/⌀、第一角/第三角投影、一般公差标准号、
理论正确尺寸是否加方框……凭记忆最容易出错。把规则落成文件、标注前读一遍，比事后返工便宜。

---

## 可选需求输入（v0.4.0 新增）

技术要求、配合公差、夹具中心线都是**按需填**的，留空自动跳过，不会在图面上留下空条目。
侧边栏「CAD 出图」面板对应位置都有输入框。

### 技术要求 `technicalRequirements`

| 字段 | 说明 | 示例 |
|---|---|---|
| `generalToleranceLevel` | 未注公差等级 | `m` → 未注尺寸公差按 GB/T 1804-m |
| `unnotedFillet` | 未注圆角 | `R1` |
| `unnotedChamfer` | 未注倒角 | `C0.5` |
| `unnotedRoughness` | 未注粗糙度 | `Ra3.2` |
| `surfaceTreatment` | 表面处理 | `阳极氧化` |
| `extraNotes` | 附加条目，一行一条 | `去毛刺\n锐边倒钝` |

引擎会把这些整理成带序号的条目（`1. 未注尺寸公差按 GB/T 1804-m。` …），
直接可落到图纸左下角的技术要求区。

### 配合公差 `fits`

```json
{ "target": "φ5.6", "code": "H7" }
```

给指定尺寸加公差代号。用户没指定就不传，避免乱标配合。

### 夹具中心线 `fixture`

```json
{ "enabled": true, "note": "基准A中心线" }
```

用户明确需要才开。

### 空值行为

所有字段留空 → 技术要求 0 条、无配合公差、无中心线。
**不会替用户编造技术要求**，这是刻意的：技术要求涉及工艺决策，猜错了比不写更糟。

### 自动标注的多次复核（强制）

系统提示词已写明：自动标注完成后必须按标准末节的自查清单**至少复核 3 轮**
（特征覆盖 → 重复/封闭链 → 方位/侵入/压线），发现问题立刻改并重跑审计。

---

## 一键打印 PDF（v0.5.0 新增）

面板上「打印 PDF」按钮，或工具 `cad_draw_pdf`。

- **范围**：自动取图纸里**最外层的包围框**（面积最大的闭合多段线）。包围框被关闭/冻结也能读到。
- **纸型**：按包围框尺寸自动判定 A0~A5，也可显式传 `paper`。
- **黑白**：默认用 `monochrome.ctb`，DWG 里图层颜色原样保留，只有打印结果变纯黑（GB 图纸要求）。
- **异步**：AutoCAD 的 `PlotToFile` 是异步的，PDF 约 20–40 秒落盘，工具会轮询等文件大小稳定。

### 实现要点（`lib/acad_plot.py`，改之前先看）

| 坑 | 正确做法 |
|---|---|
| `PlotToFile` 立刻返回但没文件 | 轮询等文件大小稳定，最多 120s |
| `PlotType=4` 报「输入无效」 | **先 `SetWindowToPlot`，再设 `PlotType=4`** |
| 设纸型/PlotType 失败 | 先 `RefreshPlotDeviceInfo()` |
| `PlotPaperSize` 只读 | 用 `CanonicalMediaName`（要排除 FULL_BLEED / EXPAND 版） |
| `GetActiveObject` 报 MK_E_UNAVAILABLE | 用 `Dispatch("AutoCAD.Application")` |
| `-PLOT` 命令行 | **绝对不要用**，会卡提示符把 COM 搞挂 |

### 实测数据

A3 图纸：37 KB / 420.2 × 297.0 mm / 2250 条线段 / 全部 `(0,0,0)` 纯黑 / 37 秒。

---

## STEP / IGES 支持（v0.5.0 新增）

现在 STEP / IGES / Parasolid 可以直接拖进来出图，插件自动：

```
LoadFile4 导入 → SaveAs4 存成 .sldprt → 重新打开 → Create1stAngleViews2
```

### 能做什么

✅ 三视图 + 轴测图正常生成
✅ 导出 DXF / DWG
✅ 后续标注、打印 PDF

### 不能做什么

❌ **自动插入尺寸为 0** —— 导入实体没有设计尺寸（0 草图 / 0 特征）
⚠ 要标注必须按几何自己量；想拿到参数化尺寸需在 SolidWorks 里跑「特征识别(FeatureWorks)」或重新建模

### 两个必须记住的 API 坑

1. **不能用 `OpenDoc6` 开 STEP** —— 会让 SolidWorks 直接 RPC 崩溃（还会弹出 `sldexitapp` 错误报告窗口，把后续 COM 全卡死）。必须用 `LoadFile4`。
2. **`LoadFile4` 签名只有 4 个参数**：`(FileName, ArgString, ImportData, Errors)`；第 3 个 `ImportData` 传 Python `None` 会报「类型不匹配」，**必须传 `VARIANT(VT_DISPATCH, None)`**。
3. **导入后的文档未保存**（`GetPathName` 为空），`Create1stAngleViews2(空串)` 直接返回 False。必须先 `SaveAs4` 成 .sldprt 再重开。
4. 上一轮残留的同名 .sldprt 若还开在 SW 里，`SaveAs4` 会 err=1；保存前先 `CloseAllDocuments`。
