# AlgoKiller

面向 ARM64 执行 trace 的算法还原 harness。给定一段 GB 级 trace 日志和一个目标（密文、字段、调用链……），驱动 LLM 通过受约束的工具调用自主搜索证据、追踪数据流、还原算法，最终交付可执行 Python 代码或结构化分析报告。

**无视 trace 文件大小，一般 200K 上下文就能还原算法或完成特定任务。**

---

## Quick Start

```bash
git clone https://github.com/yourname/AlgoKiller.git
cd AlgoKiller

# 1. 构建 native 搜索工具（必须，harness 启动时会调用 ak_search 二进制）
cd tools/search && make && cd ../..

# 2. 配置模型与 API Key
cp .env.example .env
$EDITOR .env

# 3. 跑起来
python run_algokiller.py --trace-file ./my_trace.log --mode ciphertext
```

运行环境：Python 3.11+、可用的 C 编译器（`cc` / `gcc` / `clang`）。

---

## 它能做什么

启动时通过 `--mode` 选定一种分析模式：

- **`ciphertext`** — 给定一段密文（header 值、token、加密后字节……），从 trace 中反向追踪生成位置，识别 buffer 边界与数据流，还原加密 / 签名 / 编码管线，并交付可复现密文生成过程的 Python 源码。
- **`general`** — 通用 trace 证据分析：字段含义、执行流时间线、检测点（反调试 / 风控 / 完整性）、调用边界、buffer 生命周期，或任何能从 trace 证据中回答的问题。默认交付结构化分析，仅在用户明确要求或任务本身是算法复现时才落地源码。

两种模式共享同一套工具与同一个 agent loop，差别仅在系统提示词中关于"何时停止"、"何时可以提问用户"、"最终交付物形态"的约束。

---

## 用法

### 交互模式

```bash
python run_algokiller.py --trace-file ./my_trace.log --mode ciphertext
```

进入 REPL（`ak >>`），支持 prompt-toolkit 历史记录、历史搜索（Ctrl-R）和多行粘贴。`q` / `quit` / `exit` 退出。

### 一次性任务

```bash
python run_algokiller.py --trace-file ./my_trace.log --mode ciphertext \
  "还原生成密文 a3b2c1... 的算法"

python run_algokiller.py --trace-file ./my_trace.log --mode general \
  "说明 9999 行 x0 返回值是如何计算出来的"
```

### 恢复会话

每次运行 harness 都会在 `sessions/` 下写一份 `<timestamp>.json` 快照（包含 messages、loop count、config、args），每轮 agent loop 自动更新。

```bash
python run_algokiller.py --resume-session sessions/20260509_213015.json
```

恢复时会沿用快照里的 `--trace-file` 和 `--mode`，不允许覆盖。加 `-i` 可恢复后进入 REPL 而非自动 `continue`。

---

## 配置

在 `.env`（或 `HARNESS_ENV_FILE` 指向的路径）中设置。模型路由通过 [LiteLLM](https://github.com/BerriAI/litellm) 完成。

### 模型与凭证

| 变量 | 说明 |
|------|------|
| `LITELLM_PROVIDER` | `anthropic` / `openai` / `google`（即 `gemini`）/ `openai-compatible`。后者表示走 OpenAI 协议但打到自定义 `API_BASE` |
| `LITELLM_MODEL_NAME` | 模型名，例如 `claude-opus-4-7`、`gpt-5.4`、`gemini-2.5-pro`。不带 provider 前缀时由 `LITELLM_PROVIDER` 自动补 |
| `API_KEY` | API Key。**支持逗号分隔多个 Key 自动轮转**（用尽时会切到下一把） |
| `API_BASE` | 自定义端点。留空走 provider 默认。**注意：设置 `API_BASE` 不会改变协议**——`LITELLM_PROVIDER=anthropic` 仍发 Anthropic 协议请求；如果你的网关是 OpenAI 兼容的，必须把 provider 改成 `openai` 或 `openai-compatible` |

### 配置示例

```bash
# Anthropic 直连
LITELLM_PROVIDER=anthropic
LITELLM_MODEL_NAME=claude-opus-4-7
API_KEY=sk-ant-...

# OpenAI 兼容网关（如自建 OpenRouter / DeepSeek / 内网代理）
LITELLM_PROVIDER=openai-compatible
LITELLM_MODEL_NAME=deepseek-v4-pro
API_BASE=https://your-gateway.example.com/v1
API_KEY=key1,key2,key3              # 自动轮转

# Google Gemini
LITELLM_PROVIDER=google
LITELLM_MODEL_NAME=gemini-2.5-pro
API_KEY=...
```

### 调优参数

均为可选，未设置时使用括号中的代码默认值。

| 变量 | 默认          | 说明 |
|------|-------------|------|
| `HARNESS_REASONING_EFFORT` | `medium`    | 支持 `reasoning_effort` 的模型的推理强度 |
| `HARNESS_TEMPERATURE` | `0`         | 采样温度（`.env.example` 给的是 `1`，按需调整） |
| `HARNESS_MAX_TOKENS` | `99999`     | 单次响应输出 token 上限 |
| `HARNESS_MAX_ITERATIONS` | `99999`     | Agent loop 最大迭代次数 |
| `HARNESS_MODEL_RETRIES` | `5`         | 模型请求失败重试次数 |
| `HARNESS_SYSTEM_REINJECTION_INTERVAL` | `20`        | 每 N 轮重新注入系统提示词以对抗约束遗忘 |
| `HARNESS_CONTEXT_COMPACTION_THRESHOLD_CHARS` | `500000`    | 活跃上下文超过此字符数触发 note 压缩 |
| `HARNESS_ARTIFACTS_DIR` | `artifacts` | 还原代码与最终 markdown 的输出目录 |
| `HARNESS_ENV_FILE` | —           | 显式指定 `.env` 路径，覆盖 cwd 查找 |

---

## 设计

### 单 Agent Loop

一个 `while True` 循环：调用模型 → 解析 `tool_calls` → 执行工具 → 把结果作为 tool message 追加到上下文 → 再次调用模型。没有状态机、没有规划器、没有路由层。"规划"完全由 LLM 在每轮推理中自行完成，harness 只负责执行和约束。

算法还原是深度优先的推理过程——追踪一个寄存器值的来源可能需要 10 轮连续搜索。多 agent 切换会丢失推理链条；单 agent 的连续上下文天然适合这种深度追踪。

### 最小工具集（4 个）

| 工具 | 职责 |
|------|------|
| `trace_search` | 大小写不敏感的精确子串搜索。必须二选一 `from_line`（向后）/ `before_line`（向前，最近优先）；`limit ≤ 100`。十六进制查询未命中时自动尝试 endian 反序与 leading-zero 修剪 |
| `trace_context` | 按文件行号展开上下文，必须显式指定 `before` 与 `after`，各 `≤ 100` |
| `ask_user` | 向用户提问。**调用先经过独立的 `AskUserReviewAgent` 审查**——若任务尚未完成、问题只是"是否继续"，验收 agent 会拒绝并要求主 agent 继续工作 |
| `write_recovered_source` | 写出最终 Python 还原源码到 `artifacts/`。harness 自动在文件名后追加 `_<MODE>_<timestamp>` |

工具少 → agent 不会在工具选择上浪费 token；参数硬上限 → 强制小步搜索，避免单次灌入 100MB+ trace 导致上下文爆炸。

### 周期性系统提示词重注入

长会话中 LLM 会逐渐"遗忘"系统提示词中的约束（必须带证据、不能跳过 `trace_context`、不能盲目搜索全文……）。每 `HARNESS_SYSTEM_REINJECTION_INTERVAL` 轮无条件重新注入完整系统提示词。用少量 token 换长会话行为稳定性。

### 自动上下文压缩（note agent）

当活跃上下文超过 `HARNESS_CONTEXT_COMPACTION_THRESHOLD_CHARS` 时：

1. 一个独立的 `NoteCompactionAgent` 扫描当前上下文，提取已确认事实、高置信推断、已排除假设、未决问题、下一步动作；
2. 写入 `notes/<timestamp>_note.md`，每条事实必须挂在稳定锚点上（行号、relative address、寄存器、`mem_r`/`mem_w`、call/hexdump/ret 边界）；
3. harness 清空旧上下文，从「system prompt + 笔记」重建会话；
4. 主 agent 继续依赖笔记结论前，被强制要求用 `trace_context` 抽查 1-3 个最重要锚点——以此对抗压缩引入的幻觉。

这让分析可以跨越模型上下文窗口限制无限延续。

### 模型无关路由

通过 LiteLLM 统一发请求，harness 本身不绑定任何具体模型。同一份提示词和工具定义可以跑在 Claude / GPT / Gemini / 任何 OpenAI 兼容端点上。`API_KEY` 支持多 Key 逗号分隔自动轮转。

### Native 搜索后端

`tools/search/ak_search`（C，~960 行）是核心性能基石：

- mmap 整个 trace，单次构建行偏移索引；
- ASCII 大小写不敏感的 BMH 搜索，不材料化 lowercase 副本；
- **daemon 模式**：harness 启动时拉起一个常驻 `ak_search daemon`，所有 `trace_search` / `trace_context` 调用复用同一份内存映射与索引，不重复 IO。

GB 级 trace 上 trace_search 的延迟通常在百毫秒量级。

---

## 输出物

跑完一次任务，会在以下位置产生文件（均带启动时间戳）：

```
artifacts/
├── <MODE>_<timestamp>.md                    最终 markdown 报告（assistant 最后一条文本）
├── <name>_<MODE>_<timestamp>.py             write_recovered_source 写出的 Python 源码
└── <name>_<MODE>_<timestamp>.notes.md       源码旁的简短证据/置信度说明（如 agent 提供 notes 字段）

notes/
└── <timestamp>_note.md                      每次自动压缩生成一份阶段性笔记

sessions/
└── <timestamp>.json                         会话快照，可用 --resume-session 恢复
```

---

## Trace 格式

接受 [GumTrace](https://github.com/lidongyooo/GumTrace) 生成的 ARM64 执行 trace。harness 强依赖以下行格式：

```
[module] 0xABS!0xREL  mnemonic operands ; observed_inputs -> observed_outputs
call func: name(args)
hexdump at address 0x... with length 0x...:
  0x00000000  48 65 6c 6c 6f 20 57 6f 72 6c 64 00 00 00 00 00  |Hello World.....|
ret: value
```

- 指令行以 `[` 开头；`;` 后是寄存器/内存观测（`x0=...`、`mem_r=...`、`mem_w=...`、`-> x8=...`），都是当前执行的真实值
- `call func:` / `ret:` 是外部调用摘要，按时间顺序夹在指令流中
- hexdump 出现在 `call`/`ret` 之间，每行 16 字节，按内存地址递增；右侧 `|...|` 是 ASCII 预览（不可打印为点），严格还原以左侧 hex 为准
- **文件行号是跨工具对齐的稳定锚点**——所有结论必须能锚定到具体行号

---

## 项目结构

```
run_algokiller.py                 入口脚本（注入默认 trace-file / mode 后转交 cli.main）
pyproject.toml                    包元数据，console script: algokiller
.env.example                      配置模板

src/algokiller_harness/
├── cli.py                        argparse、REPL、错误诊断、会话编排
├── config.py                     .env 加载，HarnessConfig dataclass
├── prompts.py                    系统提示词（base + ciphertext + general）
├── agent_prompts.py              ask_user 验收 agent 与 note 压缩 agent 的提示词
├── tool_schemas.py               4 个工具的 JSON Schema
├── tool_protocol.py              ToolExecutor 接口
├── trace_agent.py                核心 agent loop、系统提示词重注入、压缩触发
├── routing_executor.py           工具调用按名称分发到具体 executor
├── trace_executor.py             trace_search / trace_context（调用 ak_search daemon）
├── artifact_executor.py          write_recovered_source + 最终 markdown 落盘
├── user_executor.py              ask_user 的实际终端交互
├── ask_user_reviewer.py          ask_user 验收 agent
├── note_compactor.py             上下文压缩 agent
├── note_store.py                 notes/ 目录管理与笔记格式
├── session_store.py              sessions/*.json 读写
├── model_client.py               LiteLLM 调用、多 Key 轮转、重试、参数构造
├── message_utils.py              消息格式与控制台打印
├── __init__.py
└── __main__.py                   python -m algokiller_harness 入口

tools/search/
├── search.c                      C 实现：mmap + 行索引 + BMH，支持 match/context/daemon
├── Makefile
├── build.sh
├── ak_search                     编译产物（gitignored 推荐）
└── README.md                     CLI 子命令说明

artifacts/  notes/  sessions/     运行期生成
tests/                            pytest
docs/                             开发文档
```

---
