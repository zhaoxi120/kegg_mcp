# KEGG MCP

[English](README.md) | **简体中文** | [日本語](README.ja.md)

**让 Codex 在本地将蛋白质 FASTA 或 KO 证据转化为可追溯的 KEGG 报告及可选图形。**

KEGG MCP 帮助研究人员通过自然语言请求注释蛋白质序列、检查已有的 KEGG Orthology
（KO）证据，并探索选定的 KEGG 参考数据。它将证据、决策、溯源信息和生成的文件保存在
一起，使结果能够接受复核，而不是被当作黑箱输出。

MCP 是 Model Context Protocol（模型上下文协议）的缩写：它是让 Codex 调用本套件工具的
本地接口。KEGG MCP 不是网站，也不是托管式分析服务。

> [!IMPORTANT]
> **项目状态：** Alpha。本套件完整功能面向 Linux 和搭载 Apple Silicon 的原生 macOS 14
> 或更高版本，并要求 Python 3.11.x。Windows 用户应使用 WSL2；不支持原生 Windows 和
> Intel macOS。统一安装目前仍受发布门禁约束，因此请使用经过审核的发布版本检出，并遵循
> [安装指南](docs/installation.md)。

> [!NOTE]
> 结果描述的是注释证据和 KEGG 参考关系。它们不能证明实验功能、通路存在或活性、通量、
> 表型或统计富集。

## 一次请求，获得可追溯的结果

<p align="center">
  <img
    src="docs/assets/fasta-to-pathway-example.webp"
    alt="一次 Codex 请求，将蛋白质 FASTA 依次送入本地 KO 注释和 KEGG 分析，最终生成可追溯的报告和可选通路图形"
    width="840"
  >
</p>

<p align="center"><em>一次请求即可协调各个阶段。实际结果取决于输入、注释配置和所选的
KEGG 参考数据。</em></p>

## 它能做什么？

从您已有的材料开始：

| 您已有 | KEGG MCP 可以 | 您将获得 |
| --- | --- | --- |
| 蛋白质 FASTA | 运行已配置的本地 DeepKOALA 以生成 KO 注释证据，然后对其进行分析 | 注释证据、易读的分析、可检查的表格和可选图形 |
| K-number 列表或注释表 | 跳过序列注释，直接进行 MODULE 评估、描述性通路 KO 覆盖度分析或 KO 集合比较 | 一份报告，以及可追溯的 CSV、TSV 和 JSON 结果 |
| KEGG 术语或受支持的标识符 | 搜索候选项、检查选定条目，并保留歧义，而不是默认选择某个匹配项 | 范围受限、可追溯且带有检索详情的查询结果 |

不太常用的工作流还可以检索 KEGG 明确列出的 PubMed 标识符、使用明确的关系类型追踪
KEGG 关系、保留选定的参考数据，并为受支持的 KEGG Mapper 或 KEGG Syntax 路径准备
经过验证的本地输入文件。完整功能列表请参阅
[Core MCP 参考文档](docs/mcp-server.md)。

## 结果是什么样的？

一次请求可以生成三类输出：

- **易读的报告**，说明分析了什么并总结主要结果。
- **可检查的证据文件**，采用 CSV、TSV 或 JSON 格式，包含决策和溯源信息。
- **可选的静态图形**，采用 SVG 或 PNG 格式，用于选定的通路叠加图或 MODULE 逻辑图。

并非每个请求都会生成全部输出。如果您已经有 KO 证据，则会跳过可选的 DeepKOALA
步骤。渲染始终是可选的。

## 这个工具适合您吗？

### 适用场景

- 您有蛋白质 FASTA、KO 标识符或注释表。
- 您希望由 Codex 协调一个可复现、面向 KEGG 的工作流。
- 您需要让歧义、多重指派、阈值和溯源信息保持可见。
- 相较于托管式多用户服务，您更倾向于使用本地文件和明确的网络访问。

### 不适用于

- 基因预测、翻译、序列比对或不受限制的基因组注释。
- 统计富集、差异丰度、代谢建模、通量或表型预测。
- 非 KEGG 数据库、任意图遍历或因果网络分析。
- Web UI、公共托管、多用户存储或重新分发 KEGG 内容。

## 试用

安装套件并将文件放入本地配置允许的文件夹后，提示词就可以聚焦于研究任务。

### 蛋白质 FASTA

> 将 `/absolute/project/inputs/proteins.faa` 作为分离株蛋白质组进行注释。分析生成的 KO
> 证据，总结选定的 MODULE 结果和描述性通路 KO 覆盖度，并将选定结果渲染为 SVG。
> 报告解析得到的 DeepKOALA 模型版本。

### 已有 KO 证据

> 将 `/absolute/project/inputs/mag-ko.tsv` 作为 MAG 进行分析。将接受和不确定的证据分开
> 处理，并分别解释精确 MODULE 完成度和通路 KO 覆盖度。

### KEGG 候选项搜索

> 在 KEGG Orthology 中搜索 `citrate synthase`。保留所有候选项，不要选择最佳匹配，
> 然后仅检索我所选择的标识符的详情。

更多合成示例请参阅[示例指南](examples/README.md)。如果已知分析单元，请告知套件它是
分离株基因组、MAG、分离株蛋白质组、泛基因组还是宏基因组群落。

## 开始使用

### 使用 Codex 或 ChatGPT 安装

最简便的方法是将此仓库交给能够访问您本地文件和终端的助手。

1. 复制仓库 URL：

   ```text
   https://github.com/zhaoxi120/kegg_mcp
   ```

2. 将它粘贴到 Codex，或粘贴到拥有本地终端访问权限的 ChatGPT 工作区中，并附上以下
   请求：

   > 从这个仓库安装 KEGG MCP。严格遵循 `docs/installation.md`。找到一个经过审核的发布
   > 版本检出；如果没有，则停止操作。检查我的平台和先决条件是否受支持。进行任何更改
   > 之前，请让我确认所需的私有目录、我的 KEGG 使用资格和访问模式，以及是否下载
   > DeepKOALA。先运行不会产生更改的预检，并且仅在预检成功后继续。安装完成后，不要在
   > 当前任务中重新安装；请告诉我如何从源码检出目录之外的一个新 Codex 任务中验证仓库的
   > 三项 Skills 及其本地 MCP 服务器。

3. 回答助手关于本地路径、下载和 KEGG 访问的问题。安装完成后，在源码检出目录之外打开
   一个新的 Codex 任务，并尝试上面的任一提示词。

当前完整设置要求：

- Linux，或搭载 Apple Silicon 的原生 macOS 14 或更高版本；Windows 主机使用 WSL2。
- Python 3.11.x、`uv` 0.11.16 或更高版本、Git，以及支持本地插件的 Codex CLI。
- 一个经过审核的发布版本检出、私有状态目录和项目目录，以及一种明确的 KEGG 访问模式。

拥有终端访问权限的 ChatGPT 工作区可以协助完成设置，但安装后的 Skills 和 MCP 服务器
需要在 Codex 中启用。无法访问本地文件和终端的普通聊天只能解释安装过程。有关手动设置和
操作详情，请参阅[安装与操作](docs/installation.md)。

请使用 **套件安装器** 完成完整的 Codex 设置。Core 和 DeepKOALA Python wheel 包
不会引入另一个服务器发行包。Renderer Python wheel 包会安装兼容的 Core
发行包作为依赖，以共享类型化契约并访问 KEGG 资产，但它不会注册或启动 Core
stdio 服务器。任何 wheel 包都不会安装仓库范围的 Skills。**仅安装 wheel 包不会使仓库范围的
Skills 可用。** 如需逐组件设置，请参阅[手动部署](docs/manual-component-deployment.md)。

## 负责任地解释结果

- K-number 指派是注释证据，而不是实验验证。
- 精确 KEGG MODULE 完成度评估受支持的参考逻辑；它不能证明通路活性、通量或表型。
- 通路 KO 覆盖度是相对于明确参考分母的描述性重叠。它不代表通路存在、完整性、活性或
  富集。
- 搜索结果只是候选项，并不代表实体身份已被自动确认。
- 未映射的标识符或缓存未命中不能证明某个生物实体不存在。

在来源和策略支持相应判定时，报告会明确区分接受、不确定、拒绝、重复和冲突的证据。
群落和泛基因组结果描述的是汇总的编码潜力，而不是单个分离株。

## 本地数据与 KEGG 访问

- 输入文件和生成的结果保留在本地配置允许的文件夹内。联网模式仅发送所选 KEGG 请求所需
  的有限标识符、术语和参数。
- 只有当用户和工作都符合公共学术 KEGG REST 访问资格时，才使用已确认的
  `public_academic` 访问。其他联网部署需要使用获得适当许可的端点。
- `offline_cache` 不会发出 KEGG HTTP 请求，也绝不会回退到网络。
- 缓存的 KEGG 响应、通路资产和渲染衍生文件必须保留在本地，并排除在版本控制、软件包、
  示例、CI 构件和发布内容之外。
- MIT 源码许可证不授予 KEGG 内容、DeepKOALA 代码或权重、KOfam profiles 或其他第三方
  材料的权利。

联网使用前，请查阅 [KEGG API 文档](https://www.kegg.jp/kegg/rest/)和
[KEGG 法律声明](https://www.kegg.jp/kegg/legal.html)。

## 文档

### 从这里开始

- [安装与操作](docs/installation.md)
- [合成示例](examples/README.md)
- [故障排除](docs/troubleshooting.md)
- [服务、结果与报告](docs/services-results-reporting.md)

### 了解分析方法

- [导入与证据契约](docs/import-contracts.md)
- [MODULE 评估](docs/module-analysis.md)
- [通路覆盖度与 KO 集合比较](docs/pathway-comparison-analysis.md)

### 技术与维护者参考资料

- [Core 软件包](docs/core-package.md)和 [Core MCP 工具](docs/mcp-server.md)
- [跨组件架构](docs/architecture.md)和
  [可视化架构](docs/visualization-architecture.md)
- [手动组件部署](docs/manual-component-deployment.md)
- [Codex Skill 评估](docs/skill-evaluation.md)和
  [发布就绪度](docs/release-readiness.md)

## 许可证

项目源码采用 [MIT License](LICENSE)。KEGG 内容、DeepKOALA 代码和权重、KOfam profiles
及其他第三方资产仍受其各自条款约束。
