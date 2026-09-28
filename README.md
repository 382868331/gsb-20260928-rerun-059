# robustlab — 协议健壮性研究系统

面向协议解析器的受控变异（fuzzing）研究框架。**目标、输入生成、实验结果三层分离**，
当前内置一个消息帧解析器作为被测目标，**不访问任何外部目标**。运行预算管理与
失败样本管理是通用组件，以后接入新协议时可以直接复用。

## 目录结构

```
goals/                        # 目标层：每个协议一个 JSON 实验配置
  message-parser.json
robustlab/
  parser/message.py           # 自带的消息帧解析器（含两个故意保留的历史缺陷）
  worker.py                   # 子进程入口：样本文件 -> 解析
  runner.py                   # 子进程执行器：超时 kill 进程组并回收、样本先落盘
  generator.py                # 固定种子、可复现的输入生成（结构变异 + 边界用例）
  budget.py                   # 运行预算：迭代次数 / 墙钟时间 / 首失败停止
  outcome.py                  # 结果分类与崩溃指纹
  minimizer.py                # 保持同一种失败的输入缩小（TLV 删除 + delta-debug）
  store.py                    # 失败样本与实验清单（按 crash/timeout/rejected 分目录）
  campaign.py / cli.py        # 实验编排与命令行
results/<campaign>/           # 结果层：manifest + failures/...
tests/test_system.py
```

## 失败模式严格分离

`robustlab/outcome.py` 定义四种互斥结果：

| 结果 | 含义 | worker 退出码 |
|------|------|----------------|
| `accepted` | 解析器正常接受 | 0 |
| `rejected` | 解析器**明确拒绝**（格式/校验错误，属于正常行为） | 2 |
| `crash` | 未捕获异常/进程异常死亡 | 1 或信号 |
| `timeout` | 超过截止时间，执行器 `SIGKILL` 整个进程组并回收 | -9 |

超时不是崩溃、拒绝也不是崩溃：缩小器只接受与原样本 **kind + 指纹都相同** 的候选。

## 使用

```bash
# 用 goals/ 中的固定配置做实验（推荐，可重复）
python -m robustlab fuzz --goal goals/message-parser.json --name demo

# 或直接给参数
python -m robustlab fuzz --seed 20260929 --iterations 40 --timeout 0.5 --name quick

# 重放任意样本（崩溃/超时退出码 3，便于脚本判断）
python -m robustlab replay results/demo/failures/crash/001__*/minimized.bin

# 测试
python -m unittest discover -s tests
```

固定种子保证可重复：`(seed, iteration)` 完全决定每一个输入；相同配置两次实验产出的
样本 SHA-256 一致。

## 自带缺陷演示（已被缩小到关键字节）

消息帧：`A5 5A | ver | flags | len(BE16) | hdr XOR | payload(TLV...) | CRC8`。

1. **CRASH — 分组码越界（缺少边界检查）**
   - 最小样本：`a55a01000003fddd0104a1`（11 字节）
   - payload 为单个 TLV `DD 01 04`：tag=0xDD 的 group 字段值为 `0x04`，
     而内部分组表只有 4 项（合法下标 0..3）→ `IndexError@_handle_group`。
   - **关键字节即偏移 9 的 `0x04`**：改成 `0x01` 后输入被正常接受。
2. **TIMEOUT — keepalive 循环终止条件错误**
   - 最小样本：`a55a01000003fdee0102ef`，TLV `EE 01 02`：偶数间隔使循环
     永不退出；执行器在截止时间后 kill 进程组并回收。

进程失败也不丢样本：执行器**先把当前样本写入磁盘再启动子进程**，随后分类结果
复制到 `failures/<kind>/`，同时保存 `original.bin`、`minimized.bin` 与 `finding.json`。

## 扩展新协议

1. 写一个新的 worker（读文件、解析，约定退出码 0/2/1），或复用
   `robustlab.worker` 的模式；
2. 提供新的种子/变异器（实现与 `SeededGenerator` 相同的 `generate(index)` 接口）；
3. 在 `goals/` 增加一个 JSON 配置，指定 `target`、种子与预算；
4. `Budget`、`ResultStore`、`SameFailureMinimizer`、`SubprocessRunner` 无需改动即可复用。
