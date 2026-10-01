# Python 协议健壮性研究系统

这是一个仅研究项目自带目标的确定性健壮性实验系统。研究目标、输入生成器和实验结果分离，运行预算与失败样本管理可复用于后续协议。

## 结构

- `goals/simple_message.json`：研究目标、目标解析器和失败分类策略。
- `targets/simple_message.py`：项目自带的二进制消息解析器及三个受控本地缺陷。
- `proto_robust/generator.py`：固定种子的语法输入与结构化变异生成器，内置定向样本。
- `proto_robust/runner.py`：独立进程执行、超时终止、异常退出回收，并提取缺陷位置。
- `proto_robust/stability.py`：按异常类型 + 缺陷位置 + 关键条件判定同一种失败，并保存重复试验证据。
- `proto_robust/reducer.py`：保持同一种失败的输入缩减，逐步记录接受理由。
- `proto_robust/fuzzer.py`：预算控制、去重分组、失败样本、活动检查点和摘要落盘。
- `results/`：实验输出，默认运行不与目标和生成逻辑混放。

## 失败语义与分组

解析器主动抛出 `ParseReject` 归为 `rejected`，只代表拒绝畸形输入，不算健壮性缺陷。未预期异常归为 `crash`，超时归为 `timeout`，子进程异常退出归为 `runner_error`。

多个同类异常会被同时追踪：崩溃签名不仅包含异常类型，还包含缺陷所在文件、函数和行号，因此两个都抛出 `IndexError` 但位于不同代码位置的人工缺陷会分成独立发现。

## 内置缺陷

1. `operation-table`（索引 8，`534d01000001ff`）：payload 首字节 `ff` 把操作表下标算成 254，`IndexError`。
2. `channel-table`（索引 9，`534d0100000201fe`）：首字节是合法操作、第二字节 `fe` 越界访问通道表，`IndexError`，与第一个缺陷位置不同。
3. `timing-stall`（索引 10，`534d01000000` + flags `77`）：解析器在该 flags 下睡眠超过紧超时，表现为时序相关超时。

## 缩减语义

缩减不只是保持异常类型。每个候选输入都会重新执行，并同时确认：

- 异常类型仍相同；
- 异常仍在原始缺陷位置（文件、函数、行号）抛出；
- 触发缺陷的关键字节条件（如操作表缺陷的 offset 6=`ff`，通道表缺陷的 offset 7=`fe`）仍满足。

这样可以避免把一个复杂缺陷缩成另一个只抛出相同异常的简单问题。每一步接受的缩减都会在样本 JSON 的 `reduction_steps` 中留下动作、结果十六进制、命中次数/试验次数和文字理由；原始样本始终单独保存在 `*.original.bin`。

## 偶发失败

新失败先重复试验（崩溃默认 5 次，超时默认 2 次），所有单次结果都写入 `confirmation.trials`，并记录命中数、试验数和复现率。一次重复没出现不会删除样本：复现率低于 1/3 时仍保留原始样本与证据，只是标记 `stable=false` 且不做缩减。缩减阶段的每个候选也要多次复现（多数命中才接受）。

## 预算、检查点与恢复

执行数量达到预算即标记 `budget_exhausted=true` 并停止。每个用例执行后都原子写入 `campaign.checkpoint.json`，记录下一个待执行索引、累计执行数和已发现签名。再次用相同 `--output` 运行会自动从检查点恢复：原始样本、样本 JSON 与每一步缩减理由都保留，新运行只对新增用例计费，摘要中以 `resumed=true` 标识。加 `--fresh` 可忽略旧检查点。

## 运行

```bash
python -m proto_robust.cli --goal goals/simple_message.json --output results/seed-20260930 --timeout 0.5
```

超时取 0.5 秒时，时序缺陷（解析器睡眠 1.5 秒）可被检出；宽松超时下它会表现为正常返回。固定种子和生成器顺序保证同一命令可重复。发现失败后保存：

- `<签名>#<索引>.original.bin`：触发失败的原始输入。
- `<签名>#<索引>.minimized.bin`：保持同类失败与关键条件的缩小输入。
- `<签名>#<索引>.json`：签名（含缺陷位置与关键条件）、复现证据、每步缩减理由、关键字节位置。
- `campaign.checkpoint.json`：可恢复的活动状态。
- `campaign.json`：种子、预算、执行数量、预算耗尽/恢复标记和发现列表。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：两个同抛 `IndexError` 但位置不同的人工解析缺陷被分别分组；第二个缺陷的缩减保持原缺陷行与关键字节；一个交替失败的目标验证偶发失败保留全部重复试验证据；一次小预算运行在预算耗尽后经恢复继续发现时序超时，且原始样本和每步缩减理由仍在。

系统不会访问外部目标；新增协议时新增目标模块、目标文件和生成器即可复用预算、隔离执行、签名分组、复现证据、缩减与样本保存流程。

## 多会话状态序列（第二轮增强）

第一轮面向单条无状态消息；协议故障往往需要一个输入序列跨多个会话状态才能触发。第二轮在不改动第一轮流程的前提下新增会话模式：

- 研究输入是有序交互序列（`SessionSequence`），带显式会话起点（默认 `closed`），保存为独立 JSON，可随时用 `replay` 子命令从干净起点完整重放。
- 目标 `targets/session_store.py` 是有状态的会话存储：`OPEN -> PREPARE -> SELECT -> COMMIT` 等命令逐步建态。状态门控缺陷 `commit-table` 只在 `selected/mode=read/bucket=mirror` 状态触发；同样的 COMMIT 帧在 `open/armed` 只是明确拒绝。
- 每次复现都用全新 spawn 进程重放完整序列，并逐步记录交互名称、帧十六进制、状态前/后、结果与耗时（`confirmation.trials[].steps`）。因此超时、拒绝或崩溃后的工作进程不可能带着污染状态测试下一序列。

### 失败分类

超时（`timeout`）、明确拒绝（`rejected`，不算缺陷）、进程崩溃（`runner_error`，保留退出码）与未预期异常（`crash`）分别归类。崩溃签名包含异常类型、缺陷文件/函数/行号以及所需会话状态路径 `state/mode/bucket`，因此两个同为 `IndexError` 但位置或建态路径不同的缺陷不会合并。

### 序列缩减语义

缩减在两个轴上进行，每个候选都从干净会话起点重放并多次复现：

- 删除非必需的前置/中间交互（保留完整会话起点和触发缺陷所必需的建态顺序）；
- 下调单帧的键/载荷字节。

接受条件同时检查异常类型、缺陷文件/函数/行号、会话状态路径和触发交互的命令/键字节。状态门控的 COMMIT 缺陷会删掉非必需的 SET 交互，但不会被缩成 `PREPARE` 处另一个位置的同类 `IndexError`。每一步接受都记录动作、命中/试验次数、完整候选序列与文字理由。

### 间歇失败

新用例最多做两次免费检测尝试（固定相反奇偶），保证偶发缺陷不会因第一次未命中而漏检。随后的确认尝试逐次编号（`confirm-0..n`），每次都是独立新进程；命中与未命中都写入证据。复现率达不到严格多数时，原始序列与全部尝试证据保留（`stage=kept_unstable`、`stable=false`），但不做缩减。

### 预算暂停与可继续缩减

检测、确认与缩减的每次重放都计入预算。确认或缩减中途预算耗尽时，活动检查点（`session.campaign.checkpoint.json`）记录：下一个待执行序列索引、累计重放数、已见签名，以及每个发现所处阶段（`confirming`/`reducing`/`reduced`/`kept_unstable`）和当前缩减器状态（当前最佳候选、已接受步骤）。用相同 `--output` 再次运行会继续未完成的确认/缩减：原始序列 `*.original.sequence.json` 与当前最小候选 `*.minimized.sequence.json` 始终分别保存，二者都能独立重放。

### 运行

```bash
# 多会话状态序列活动（紧超时以检出时序缺陷）
python -m proto_robust.cli session --goal goals/session_store.json \
  --output results/session-core-demo --timeout 0.5

# 只跑指定的生成序列索引
python -m proto_robust.cli session --output results/demo \
  --timeout 0.5 --indexes 0,1,2,3

# 独立重放原始或最小序列
python -m proto_robust.cli replay \
  results/session-core-demo/<stem>.minimized.sequence.json --timeout 0.5

# 第一轮的无状态消息模式保持不变
python -m proto_robust.cli run --goal goals/simple_message.json \
  --output results/seed-20260930 --timeout 0.5
```

发现产物（每个签名）：

- `<签名>#<索引>.original.sequence.json`：触发失败的原始有序交互序列与会话起点。
- `<签名>#<索引>.minimized.sequence.json`：保持同一缺陷、同一状态路径的当前最小候选（缩减暂停时也已保存）。
- `<签名>#<索引>.json`：签名（含缺陷位置与状态路径）、每次尝试的完整交互证据、缩减步骤与理由、阶段标记。
- `session.campaign.checkpoint.json` / `session.campaign.json`：可恢复活动状态与摘要。
