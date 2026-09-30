# Python 协议健壮性研究系统

这是一个仅研究项目自带目标的确定性健壮性实验系统。研究目标、输入生成器和实验结果分离，运行预算与失败样本管理可复用于后续协议。

## 结构

- `goals/simple_message.json`：研究目标、目标解析器和失败分类策略。
- `targets/simple_message.py`：项目自带的二进制消息解析器及一个受控本地缺陷。
- `proto_robust/generator.py`：固定种子的语法输入与结构化变异生成器。
- `proto_robust/runner.py`：独立进程执行、超时终止、异常退出回收。
- `proto_robust/reducer.py`：保持同一种失败（异常类型 + 抛出位置 + 关键条件）的输入缩减，逐步记录理由。
- `proto_robust/fuzzer.py`：预算控制（含重复试验与缩减执行）、重复试验证据、检查点与 `--resume`。
- `targets/multi_defect.py`：三个人工缺陷——两个在不同位置抛出相同 `IndexError` 的解析缺陷，加一个依赖消息顺序的间歇时序缺陷。
- `goals/multi_defect.json`：多缺陷分组、缩减保真、偶发证据与恢复实验的目标。
- `results/`：实验输出，默认运行不与目标和生成逻辑混放。

## 失败语义

解析器主动抛出 `ParseReject` 归为 `rejected`，只代表拒绝畸形输入，不算健壮性缺陷。未预期异常归为 `crash`，超时归为 `timeout`，子进程异常退出归为 `runner_error`。缩减崩溃时要求异常类型仍相同，因此不会把崩溃、拒绝或超时混为一类。

### 同类异常的分组

两个不同缺陷可能抛出完全相同的异常（如两个 `IndexError`）。崩溃签名因此包含目标模块内的**抛出位置**（`文件:行号`）和从异常消息派生的**关键条件指纹**（如越界下标 `index:4`）。分组键形如：

```
crash:IndexError@multi_defect.py:73#index:4
crash:IndexError@multi_defect.py:84#index:2
```

### 缩减保真

缩减不只是保持异常类型：每个候选都必须仍在**原来的抛出位置**、以**原来的关键条件**失败。把复杂输入缩成同文件里另一个更简单的越界（不同下标）会被拒绝。关键字节位置通过逐字节置零后失败是否仍满足来确认。每一次接受或拒绝都写入 `reduction_steps`（阶段、动作、复现命中次数、理由、候选十六进制），并在接受时立即落盘。

### 偶发失败与重复试验

发现崩溃后用固定次数重复试验记录稳定程度（`hits/trials`、每次结果与 `stable` 标志）。稳定样本单次确认即可；未达到全命中的样本按 `--confirm-trials`（默认 4）反复确认，命中率达到阈值（默认 0.5）才接受缩减步。**单次未复现绝不会删除样本**：首次崩溃本身即证据，样本以偶发（flaky）形式保留，缩减时同样多试验确认。

### 预算耗尽与恢复

`--max-executions` 统计全部隔离执行（变异样本 + 重复试验 + 缩减试探），耗尽时 campaign 标记 `budget_exhausted` 并写入检查点（生成器索引、已用执行数、每个发现的状态）。用 `--resume` 重跑同一输出目录会：

- 重新加载已保存的发现与已去重键，原始样本 `*.original.bin` 永不覆盖；
- 先续完未完成的缩减（从当前最小候选继续，保留并追加每一步理由）；
- 从上次停下的生成器索引继续变异。

## 运行

```bash
python -m proto_robust.cli --goal goals/simple_message.json --output results/seed-20260929

# 多缺陷：两个同异常缺陷 + 时序缺陷，先耗尽预算再恢复
python -m proto_robust.cli --goal goals/multi_defect.json --output results/multi --max-executions 20
python -m proto_robust.cli --goal goals/multi_defect.json --output results/multi --resume --max-executions 2000
```

固定种子和生成器顺序保证同一命令可重复。发现失败后，系统先保存原始样本，再执行缩减，并保存：

- `*.original.bin`：触发失败的当前原始输入。
- `*.minimized.bin`：保持同类失败的缩小输入。
- `*.json`：含位置/条件的签名、索引、十六进制内容、关键字节位置、重复试验证据和逐步缩减理由。
- `campaign.json`：种子、预算、执行数量、状态、下一生成器索引和发现列表（检查点）。

内置缺陷可缩小到 `534d01000001ff`，即合法 SM 头后仅一个 payload 字节 `ff`；该字节被错误当作操作表下标。

## 测试

```bash
python -m unittest discover -s tests -v
```

系统不会访问外部目标；新增协议时新增目标模块、目标文件和生成器即可复用预算、隔离执行、签名去重、缩减与样本保存流程。
