# 回测(规划)

- 主回测工具:**Freqtrade**(指导书第 11 节)。
- VectorBT 仅作为后续可选研究模块。
- Freqtrade 与自研 LangGraph + CCXT 执行系统**不得同时控制同一账户的同一交易对**。
- FreqAI 是 Freqtrade 的机器学习功能,与本项目的 LLM Agent 不是一回事。

## 状态

`freqtrade_adapter.py` **尚未实现**(计划在阶段 2(数据)+ 阶段 5(Paper)之后、
阶段 6 之前开始)。当前阶段创建本 README 仅作为接口与职责说明,不提供假实现。
