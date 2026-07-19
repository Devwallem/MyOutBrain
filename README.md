# MyOutBrain

MyOutBrain 是一个面向单一创作者的、本地优先的长期知识与认知记忆核心。
它把 Codex、OpenCode 和 Claude Code 视为可替换的智能体入口，把规范记忆、
来源关系、审阅决定和演变历史保存在同一个项目无关的私人实例中。

当前发布代际为 **V2**，运行协议为 **V2.3**，私人实例 Schema 为 **11**。
Python 包仍处于 `0.1.0` 开发版本。

> MyOutBrain V2 已完成可发布的本地知识闭环，但仍是记忆核心而不是完整的
> 桌面应用或自主智能同伴。人格、情绪、多媒体、实时同步、无人值守模型调用
> 和自动知识批准不在当前版本范围内。

## 核心原则

- **一个私人实例**：不同项目和智能体入口共享同一个长期记忆核心。
- **本地优先**：基础闭环不要求网络、Embedding 模型或常驻进程。
- **人工拥有语义权**：模型只能提出知识变更，不能自动批准、改写或删除规范记忆。
- **来源可追溯**：长期记忆保留稳定身份、版本、适用范围和最低来源凭证。
- **入口可替换**：客户端配置和 Skill 不保存规范数据，重装入口不会删除知识。
- **紧凑召回**：普通任务先取得相关规范记忆，需要核验时再展开完整证据。
- **冲突不投票**：反证会阻止无冲突结论并进入统一审阅，不按来源数量自动裁决。

## 源码仓库与私人实例

源码仓库和私人实例是两个不同的边界，不应放在同一个目录中。

### 本仓库保存

```text
MyOutBrain/
├── src/                 # Python 实现与 V2 协议 Schema
├── skills/              # 通用 Companion / Reflector Skills
├── tests/               # CLI、MCP 和跨入口黑盒测试
├── evaluation/          # 召回回归数据
├── docs/                # 发布说明、ADR 和智能体文档
├── .scratch/            # 本地规格与实施事项记录
├── CONTEXT.md           # 领域语言
└── pyproject.toml       # 包与开发工具配置
```

这里不保存任何真实用户知识、Vault、SQLite 数据库、对象存储或运行缓存。

### 私人实例保存

执行 `myoutbrain init` 后会在另一个本地目录生成：

```text
MyOutBrain-private/
├── myoutbrain.toml      # 实例身份、Schema 与本地配置
├── store/
│   ├── memory.sqlite3   # 规范记忆与审阅状态的事实来源
│   └── objects/         # 内容寻址的来源对象
├── runtime/             # 可重建索引、缓存、日志和临时工作区
└── vault/               # 可重建的人类 / Obsidian 视图
```

`vault/` **仍然有用**，但它属于私人实例，而不是云端源码仓库。V2 中 SQLite
规范状态和内容寻址对象才是事实来源；Vault 用于人类浏览、审计和可选的 Obsidian
工作流。删除生成视图不应造成知识丢失，视图可以从规范状态重建。

## 安装

### 要求

- Windows、macOS 或 Linux
- Python 3.13+
- Git（从源码安装时）
- 可选：Obsidian 1.12.7+ 及其 CLI
- 可选：`sentence-transformers`，仅用于未来或实验性的本地 Embedding 后备

### 从源码安装

以下示例使用 PowerShell：

```powershell
git clone https://github.com/Devwallem/MyOutBrain.git
Set-Location MyOutBrain

py -3.13 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e .
```

如需安装可选 Embedding 依赖：

```powershell
& .\.venv\Scripts\python.exe -m pip install -e ".[embeddings]"
```

## 创建私人实例

私人实例应位于源码仓库之外：

```powershell
$MyOutBrainPython = "$(Resolve-Path .\.venv\Scripts\python.exe)"
$MyOutBrainInstance = "$env:USERPROFILE\MyOutBrain-private"

& $MyOutBrainPython -m myoutbrain init `
  --root $MyOutBrainInstance `
  --format text

& $MyOutBrainPython -m myoutbrain status `
  --root $MyOutBrainInstance `
  --format text

& $MyOutBrainPython -m myoutbrain doctor `
  --root $MyOutBrainInstance `
  --format text
```

成功状态应报告：

```text
MyOutBrain V2 canonical schema 11
Canonical store: ok
Object store: ok
Writer: available (single-writer)
Integrity: ok
```

## 连接智能体

安装 Codex 入口：

```powershell
& $MyOutBrainPython -m myoutbrain adapter install codex `
  --root $MyOutBrainInstance

& $MyOutBrainPython -m myoutbrain adapter check codex `
  --root $MyOutBrainInstance
```

也可以把同一个私人实例连接到另外两个入口：

```powershell
& $MyOutBrainPython -m myoutbrain adapter install opencode `
  --root $MyOutBrainInstance

& $MyOutBrainPython -m myoutbrain adapter install claude-code `
  --root $MyOutBrainInstance
```

安装器会注册同一个主私人实例，写入受管的 MCP 配置和无状态 Skill。安装或重装
不会复制规范数据；卸载入口也不会删除私人实例。安装后请重启对应客户端或新建任务，
让 MCP 配置和 Skill 生效。

## 日常使用

MyOutBrain 的首选控制面是与智能体的自然对话。CLI 主要用于运维、自动化和
黑盒验收。

### 1. 任务前召回

对智能体说：

> 请先使用 MyOutBrain 召回与这个任务有关的长期记忆，再开始工作。

入口会通过 MCP 请求任务相关的紧凑召回包。回答主要来自私人知识库时，应显示
MyOutBrain 知识来源声明；需要核验时再展开证据。

### 2. 捕获明确学习信号

V2 只捕获五类明确学习信号：

- 用户纠正
- 已确认决定
- 可复用步骤
- 重复失败及其解决办法
- 值得持续研究的问题

普通闲聊、任务时长、消息数量、任务完成或沉默都不会自动形成学习记录。

可以对智能体说：

> 我明确确认：……。请把它作为学习信号提交，但不要替我批准长期记忆。

### 3. 反思与审阅

对智能体说：

> 现在反思本轮，把候选记忆、形成方式、依据、冲突和盲区交给我审阅。

Reflector 会把有界输入变成统一审阅提案。创作者可以逐项修改、批准、拒绝或
延期；只有批准后的正确意图才会物化为规范记忆、人类归档或研究线程。

个人认知必须逐项明确确认，不能通过“全部批准”批量定义。

### 4. 后续召回和审计

可以自然询问：

- “你还记得我们关于 X 的决定吗？”
- “为什么你认为 X？”
- “展开这条记忆的来源和演变。”
- “这条知识是否受到过反证？”
- “把这条认识标记为历史可信。”
- “忘掉这条记忆。”（默认可恢复停用）
- “永久删除，并先展示影响闭包。”

## 常用运维命令

```powershell
# 查看实例状态
& $MyOutBrainPython -m myoutbrain status `
  --root $MyOutBrainInstance --format text

# 只读完整性诊断
& $MyOutBrainPython -m myoutbrain doctor `
  --root $MyOutBrainInstance --format text

# 查看统一审阅队列
& $MyOutBrainPython -m myoutbrain review-list `
  --root $MyOutBrainInstance --format text

# 查看不复制问题或正文的紧凑召回日志
& $MyOutBrainPython -m myoutbrain recall-activity `
  --root $MyOutBrainInstance --format text
```

备份、迁移、修复、垃圾回收和永久删除需要显式版本、幂等键或影响确认。运行
`python -m myoutbrain --help` 及具体子命令的 `--help` 查看当前协议参数。

## V2 已发布能力

- 学习信号经 Reflector 和统一审阅成为可召回规范记忆。
- Codex、OpenCode、Claude Code 共享同一记忆身份、版本和审阅状态。
- 名称、旧别名、分区、胶囊、全文检索和可选语义后备共同支持有界召回。
- 反证使当前任务不可回答并进入审阅，待审期间不改变既有规范知识。
- 知识修订、历史化、取代、可恢复停用和永久删除保留相应审计边界。
- 胶囊裂分、故障恢复和合并保持知识身份、正文、关系与召回结果。
- 经审计的知识闭包可通过普通 ZIP 进行 dry-run 和幂等迁移。
- 整库冷快照只恢复到新目录，并在只读 Doctor 通过后允许切换。
- 基础闭环不依赖网络、Embedding 模型或常驻进程。

九项发布阻塞场景见 [`docs/releases/v2.md`](docs/releases/v2.md)，唯一 V2 行为规格
见 [`.scratch/myoutbrain-v2/spec.md`](.scratch/myoutbrain-v2/spec.md)。

## 当前非目标

- 固定 Web、桌面、Obsidian 或 TUI 审阅界面
- 无人值守 headless 模型调用
- 自动知识批准或自动反证裁决
- 人格、情绪、用户声音和作者模仿
- 多媒体理解
- 实时同步、设备配对和多用户协作
- 内建迁移加密、签名或云端备份服务
- 推荐算法或持久知识权重

这些能力可以在真实使用证明需要后，通过同一个 MemoryGateway 协议增量加入，
但不能把客户端缓存、向量索引、Vault 或模型私有记忆升级为规范事实来源。

## 开发与验证

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m mypy
```

发布门禁测试：

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_cli_v2_release.py -q
```

领域术语见 [`CONTEXT.md`](CONTEXT.md)，架构决策见 [`docs/adr/`](docs/adr/)，本地
事项约定见 [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md)。

## 隐私与备份

- 不要把私人实例目录提交到源码仓库。
- 不要让智能体绕过 MCP/CLI 直接读取 SQLite、对象存储或整个 Vault。
- `local-only` 内容不得仅为完成任务而放宽成可外发内容。
- 普通备份是私人实例的冷 ZIP 快照；V2 不默认提供加密或自动保留策略。
- 恢复必须写入新目录并通过 Doctor，不能覆盖最后一个可用实例。

## License

仓库当前尚未包含开源许可证。除非仓库所有者明确添加许可证，否则不要假定代码
可以被公开复制、修改或再分发。
