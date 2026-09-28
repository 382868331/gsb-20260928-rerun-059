"""robustlab: 协议健壮性研究系统。

三层分离：
  * 目标（goals/）：被测协议入口与实验参数；
  * 输入生成（robustlab.generator）：固定种子、可复现的变异；
  * 实验结果（results/）：执行记录、失败样本、缩小结果。
"""

__version__ = "0.1.0"
