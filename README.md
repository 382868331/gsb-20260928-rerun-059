# Python 协议健壮性研究系统

这是一个仅研究项目自带目标的确定性健壮性实验系统。研究目标、输入生成器和实验结果分离，运行预算与失败样本管理可复用于后续协议。

## 结构

- `goals/simple_message.json`：研究目标、目标解析器和失败分类策略。
- `targets/simple_message.py`：项目自带的二进制消息解析器及一个受控本地缺陷。
- `proto_robust/generator.py`：固定种子的语法输入与结构化变异生成器。
- `proto_robust/runner.py`：独立进程执行、超时终止、异常退出回收。
- `proto_robust/reducer.py`：保持同一种失败的输入缩减。
- `proto_robust/fuzzer.py`：预算控制、去重、失败样本和摘要落盘。
- `results/`：实验输出，默认运行不与目标和生成逻辑混放。

## 失败语义

解析器主动抛出 `ParseReject` 归为 `rejected`，只代表拒绝畸形输入，不算健壮性缺陷。未预期异常归为 `crash`，超时归为 `timeout`，子进程异常退出归为 `runner_error`。缩减崩溃时要求异常类型仍相同，因此不会把崩溃、拒绝或超时混为一类。

## 运行

```bash
python -m proto_robust.cli --goal goals/simple_message.json --output results/seed-20260929
```

固定种子和生成器顺序保证同一命令可重复。发现失败后，系统先保存原始样本，再执行缩减，并保存：

- `*.original.bin`：触发失败的当前原始输入。
- `*.minimized.bin`：保持同类失败的缩小输入。
- `*.json`：签名、索引、十六进制内容和关键字节位置。
- `campaign.json`：种子、预算、执行数量和发现列表。

内置缺陷可缩小到 `534d01000001ff`，即合法 SM 头后仅一个 payload 字节 `ff`；该字节被错误当作操作表下标。

## 测试

```bash
python -m unittest discover -s tests -v
```

系统不会访问外部目标；新增协议时新增目标模块、目标文件和生成器即可复用预算、隔离执行、签名去重、缩减与样本保存流程。
