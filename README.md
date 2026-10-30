# dsh-cad-draw

SolidWorks 模型 → GB 国标工程图 → DXF/DWG，全自动。

一个 [DeepSeek Harness](https://github.com/deepseek-ai) 插件：
拖进 `.sldprt` / `.sldasm`（也支持 STEP / IGES），自动套国标图框、投三视图+轴测图、
显示隐藏线、按 GB/ISO/ASME 标准标注，最后一键打印成 A3 黑白 PDF。

## 更新日志

### v0.5.1（当前）

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
