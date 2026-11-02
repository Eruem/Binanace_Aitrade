# 架构说明

分层(指导书第 3 节):

```
Market Data Layer            (阶段 2)
    |
Data Validation & Indicators (阶段 2)
    |
LangGraph Workflow           (阶段 3)
    |-- Market Agent
    |-- Strategy Agent
    |-- Decision Agent
    |
Deterministic Risk Engine    (阶段 4)
    |
Execution Gateway            (阶段 5 Paper;阶段 6 Testnet;阶段 8 Live)
    |-- Paper Broker (默认,已实现)
    |-- Binance Spot Testnet (显式启用,阶段 6)
    |-- Binance Live 真实资金 (显式四重校验,阶段 8)
    |
Order Reconciliation & Persistence (阶段 5 本地 JSON;阶段 6 对账、阶段 7 入库)
    |
PostgreSQL / Monitoring / Telegram (阶段 7 已实现,数据库/通知可选启用)

横向:LLM Adapter + Dynamic Model Router(阶段 1,已实现)
```

## 核心边界(不可跨越)

1. Agent 只负责分析和生成建议,不直接下单;
2. Risk Engine 是确定性 Python 代码,拥有最终否决权;
3. Execution Gateway 是唯一可以创建订单的模块;
4. LLM Adapter 不持有交易所凭据,LLM 输出不是可信指令;
5. 回测(Freqtrade)与在线执行系统职责分离;
6. 默认 Paper Broker;Testnet/Live 必须显式启用并使用各自独立密钥;
   Live(真实资金)额外要求 `ENABLE_LIVE_TRADING=true` 与
   `risk.enabled=true`,缺一不可(阶段 8,项目所有者显式授权)。

## 当前已实现

### config/(阶段 0)

- `loader.py`:`${VAR}` / `${VAR:-default}` 插值 → Pydantic 校验 → 跨字段安全校验;
- 安全硬约束:拒绝 `enable_live_trading=true`;`testnet` 必须与
  `ENABLE_BINANCE_TESTNET=true` 成对出现;
- `ConfigSet.sanitized_summary()` 供启动横幅与检查脚本使用,密钥一律 `****`。

### llm/(阶段 1)

- `providers.py`:`OpenAICompatibleProvider`,直接用 httpx 调 Chat Completions,
  不用 OpenAI SDK(避免自动重试叠加);HTTP 状态按 429/5xx=可重试、
  400/401/403/404=致命 分类;
- `registry.py`:别名 → ModelSpec + Provider;
- `complexity.py`:确定性复杂度评分(只影响模型选择,不影响风控);
- `router.py`:`ModelRouter`,规则路由 + 复杂度升级 + 重试 + 备用模型 +
  每轮调用限额 + 并发信号量;全部失败返回 `SafeFailure(action=HOLD)`;
- `response_parser.py`:`parse_llm_json` 严格校验(空/非 JSON/未知字段拒绝);
- `usage_tracker.py`:每次调用的审计记录(内存 + 可选 JSONL)。

### data/(阶段 2)

- `market_data.py`:`Candle`(frozen Pydantic,拒 NaN/Inf)、`OHLCVSeries`、
  `MarketSnapshot`(多周期快照,阶段 3 Agent 输入);
- `binance_data.py`:`BinancePublicData`,CCXT async 公共行情,**强制无 API Key**,
  交易所对象可注入(测试不发真实请求);
- `binance_vision.py`:`BinanceVisionPublicData`,官方归档镜像备选源
  (部分网络 api.binance.com 不可达而 vision 可达);按日 zip/CSV 解析,
  微秒时间戳归一为毫秒,缺失日跳过不伪造;下载器可注入;
- `data_validator.py`:缺口/重复/倒序/OHLC 逻辑/NaN/未收盘最新 K 线/过期/
  多周期对齐,全部确定性检查;任一 error 即拒绝本轮(不许 AI 猜测补齐);
- `indicators.py`:SMA/EMA/RSI(Wilder)/MACD(12,26,9)/ATR(Wilder)/
  Bollinger(20,2σ)/成交量均值与变化率;纯 Python,数据不足输出 None,
  输入污染抛 ValueError(第二道防线)。

### agents/(阶段 3)

- `schemas.py`:`MarketAnalysis` / `StrategyProposal` / `TradingDecision`
  (extra=forbid;金额 Decimal;BUY/SELL 必须带正 amount,HOLD 禁带;
  GRID 禁止作为决策策略;expires_at 必须晚于 created_at);
- `state.py`:TypedDict + reducer;两个分析节点各占独立 key,并发追加的
  错误/审计列表用 operator.add;
- `nodes.py`:5 个节点,依赖全部注入(router/行情客户端/时钟/审计路径);
  decision_id、created_at、symbol 由系统覆写,不信任模型自述;
- `graph.py`:数据错误 → 跳过分析直接合成 HOLD;正常 → Send 扇出并行
  Market/Strategy 分支 → 汇合 Decision → record;Checkpointer 持久化,
  thread_id=cycle_id 可追溯;SQLite 持久化经 AsyncSqliteSaver。

安全降级矩阵(全部有测试):
数据错误→hold_data_error;市场模型挂→hold_llm_failed(合成 HOLD);
决策输出非法/未知字段→hold_llm_failed;全部模型挂→HOLD;
模型给出 HOLD→hold_no_trade。

### risk/(阶段 4)

确定性 Python 风控,对交易计划拥有最终否决权;LLM 不持有任何风控接口。

- `engine.py`:`RiskEngine.evaluate` 按固定顺序执行 11 条规则,
  任一 REJECT 即否决;逐条 `RuleCheck` 记录 + JSONL 自审落盘:
  1. 风控启用(`enabled=false` 视为停机,拒绝一切);
  2. Kill Switch(禁新开仓;SELL 平仓豁免);
  3. HOLD → `NO_ORDER`(无订单可审,保留 hold_* 终态);
  4. 信号过期(expires_at);
  5. 交易对白名单;
  6. 数据错误停机(stop_on_data_error);
  7. 重复订单(decision_id 已执行);
  8. 日亏损上限(禁新开仓;SELL 豁免);
  9. 连续亏损(禁新开仓;SELL 豁免);
  10. 仓位计算 + 单笔风险复核;
  11. 订单验证(余额/敞口/最小名义/裸卖)。
- `position_sizing.py`:BUY 金额取 5 个上限的最小值(决策金额、单笔权益%、
  敞口余量、可用余额÷(1+费+滑点)、单笔风险换算),0.01 取整;
  **单笔风险 = assumed_stop_loss_pct × 订单金额**,不是订单金额百分比;
- `kill_switch.py`:持久化状态;损坏 = 视为已触发;auto_pause 只能经
  人工 `release` 恢复;LLM 来源的触发被拒绝;
- `daily_loss.py`:UTC 日键净实现盈亏;损坏状态 = 拒绝交易(不静默放宽);
- `order_validator.py`:纯函数订单校验,执行层(阶段 5/6)不得绕过;
- `agents/nodes.py::risk_node`:decision → risk → record;账户快照获取
  失败 / 未配置引擎 → 否决(fail-safe);通过 → `final_status=risk_approved`
  并携带 sized 金额,否决 → `risk_rejected` + 逐条原因。

风控结果三态:`APPROVED`(放行并给出仓位)/ `REJECTED`(否决)/
`NO_ORDER`(HOLD,无订单)。全部写入 cycle 审计 JSONL 的 `risk` 字段,
`decision_id` 贯穿决策 → 风控 → 订单 → 成交。

### exchange/(阶段 5/6)

执行网关:唯一可以创建订单的模块;Paper Broker 不接触任何密钥,
Testnet 客户端只持有注入的独立 Testnet 密钥(见下)。

- `interfaces.py`:`Broker` 抽象接口(submit/get_order/cancel/get_account)
  + `OrderResult`(指导书 9.3 要求的记录字段:Client Order ID、交易所订单 ID、
  成交量、均价、费用、时间)+ `TradeRecord`;业务层只依赖此接口;
- `order_state.py`:订单状态机(10 状态:CREATED / RISK_APPROVED /
  SUBMITTING / NEW / PARTIALLY_FILLED / FILLED / CANCELED / REJECTED /
  EXPIRED / UNKNOWN)+ 合法流转表;非法跳变抛
  `InvalidTransitionError`,终态不可再变;UNKNOWN 只能经查询恢复;
- `paper_broker.py`:本地模拟成交——参考价注入(或 price_hint)、
  滑点(BUY 加价 / SELL 减价)、手续费(计入成本基础 / 从收入扣除)、
  部分成交(fill_ratio 首成交 + complete_fill 补足)、余额与持仓记账、
  未成交 BUY 锁定可用余额;状态文件损坏 → 构造即抛错,拒绝交易;
- `order_manager.py`:订单幂等的唯一入口——Client Order ID 由 decision_id
  确定性生成(稳定、唯一、<=36 字符、只含允许字符);下单前先持久化意图
  (CREATED → RISK_APPROVED → SUBMITTING)再调 Broker;提交异常 → UNKNOWN;
  恢复先查询原单,交易所无记录才允许同一 ID 重试,**绝不生成新 ID 重发**;
  重复提交幂等返回,绝不重复记账;
- `agents/nodes.py::execute_node` + `graph.py::_route_after_risk`:
  decision → risk → (APPROVED) execute | record → record;
  无风控放行 / 未配置执行层 → `exec_rejected`(fail-safe);
  UNKNOWN → `exec_unknown` 并注册防重复(结果未知宁可阻止,不重复下单);
  REJECTED 不注册(明确未成交);
- 持久化:订单/成交/余额在 `var/paper_broker.json`,意图在
  `var/order_intents.json`(原子写;.gitignore 已排除 var/)。

`decision_id` 贯穿决策 → 风控 → Client Order ID → 订单 → 成交;
审计 JSONL 的 `order` 字段记录每次执行结果。

### exchange/(阶段 6 追加)

Binance Spot Testnet 接入(显式启用;`TRADING_MODE=paper` 时这些组件
完全不参与):

- `binance_client.py`:薄传输层(CCXT async 私有/公共接口,签名自动)。
  安全边界(阶段 8 修订):默认只连 Testnet——`testnet=False` 必须显式
  `allow_live=True` 才会构造,否则直接拒绝;
  **从不读取任何环境变量**,密钥只经构造参数注入;Testnet:set_sandbox_mode(True)
  必须在首个请求前启用,并校验 URL 指向 `testnet.binance.vision`,
  否则拒绝一切请求;Live(阶段 8):**绝不**启用 sandbox,URL 必须指向
  `api.binance.com`(指向 Testnet 或未知地址一律拒绝);
  代理注入:配置 `EXCHANGE_PROXY_URL` 时设置 `https_proxy` 属性
  (CCXT `trust_env=False` 不读系统代理;异步 aiohttp 只认
  `http_proxy`/`https_proxy` 且两者同设冲突,本项目流量全为 https);
  错误归类:ccxt NetworkError → ExchangeNetworkError
  (上抛,上层标 UNKNOWN),ccxt ExchangeError → ExchangeAPIError
  (上层映射 REJECTED);交易对规则(LOT_SIZE 步长 + 最小名义)按交易对缓存;
- `testnet_broker.py`:实现 `Broker` 接口。BUY 用 quoteOrderQty
  (8 位向下取整,精确花费 N USDT);SELL 用 ticker 估算数量并按
  stepSize 向下取整;低于最小名义 → **本地 REJECTED,不发单**;
  Binance 原生 6 种状态与订单状态机 1:1 映射(未知 → UNKNOWN);
  手续费从 my_trades 汇总(仅 USDT 部分);网络异常上抛不吞;
  账户快照:USDT free/locked + 非 USDT 按 ticker 标记;
- `order_reconciler.py`:定期把本地意图与交易所核对——
  SUBMITTING/UNKNOWN 查到即同步、确认无单则释放(允许同 ID 重试);
  在途/终态与交易所矛盾 → **危险不一致 → Kill Switch auto_pause
  (停止新开仓),恢复必须人工确认**;查询本身失败(断线)不动任何状态,
  下轮再查;
- `live_broker.py`(阶段 8):`BinanceLiveBroker` 继承 Testnet Broker
  全部订单/对账逻辑(与环境无关),上游客户端必须经 allow_live +
  正式 API URL 确认;真实资金,风控强制开启;
- `factory.py`:按 `trading_mode` 构建 Broker。fail-safe:paper →
  **绝不**创建任何交易所客户端(注入参数有意忽略,Testnet/Live 密钥齐全
  也不用);testnet → 双保险校验开关与独立密钥后构建;
  live(阶段 8)→ 模式 + ENABLE_LIVE_TRADING=true + 独立 Live 密钥 +
  risk.enabled=true 四重校验(配置层与工厂层各验一遍,纵深防御);
  未知模式 → 拒绝。
- `order_manager.py`(阶段 6 追加 API):`list_intents`(对账快照)、
  `confirm_absent`(确认未送达 → 回退 RISK_APPROVED 同 ID 重试)、
  `sync_from_exchange`(用交易所状态同步本地意图,经状态机校验)。

### database/(阶段 7)

审计/查询增强层(幂等主存储仍是本地 JSON/JSONL;DB 故障自动降级,
绝不影响交易主流程):

- `models.py`:指导书 §10 的 11 张表 + `schema_migrations`——
  market_snapshots / indicator_snapshots / llm_calls / agent_runs /
  agent_decisions / risk_audits / orders / trades / positions /
  account_snapshots / system_events;UUID 字符串主键;金额价格数量
  NUMERIC(24,8)(读回 Decimal);orders.client_order_id 唯一约束;
  时间统一 UTC 毫秒;**任何表都不含 API Key/Secret/Token/密码列**
  (测试按列名片段断言);
- `migrations/`:轻量迁移 runner(版本号 + schema_migrations 表,
  顺序执行、幂等;不用 Alembic);`scripts/migrate_db.py` 为入口,
  DATABASE_URL 为空时拒绝运行(防误建 SQLite);
- `repository.py`:`Recorder`——`record_cycle`(一个事务写入整轮
  行情/指标/Agent 运行/LLM 调用/决策/风控/订单/成交/账户/仓位/事件;
  订单与成交同事务,满足"事务覆盖订单意图和状态更新")、
  `record_order`(以 client_order_id 为幂等键的 upsert)、
  `trace_decision(decision_id)`(决策 → Agent 输出 → 风控审核 →
  订单 → 成交 全链路一次查出)、`record_system_event`;
- 接入:`NodeDeps.recorder` → `record_node` 双写(JSONL 主备份 +
  DB 增强);record_cycle 失败只记 db_write_failed 事件,不打断 cycle;
- URL scheme 白名单(postgresql/sqlite),口令脱敏只显 scheme/host。

### monitoring/(阶段 7)

- `logger.py`:JSON 行结构化日志,标准字段(timestamp/level/cycle_id/
  thread_id/decision_id/agent_name/task/model_alias/provider/latency_ms/
  result_status/error_code);`RedactionFilter` + 格式化后再整行脱敏:
  sk-… 密钥、Bearer/Authorization、token/password 键值、
  Telegram Token(数字:长串)、URL 内嵌口令;
- `metrics.py`:`MetricsCollector`(线程安全计数器/仪表/延迟样本):
  模型成功失败与延迟、每轮 token、行情数据延迟、Agent 失败、
  风控否决与结论、订单/成交状态、账户余额、Kill Switch 状态、
  cycle 终态;`snapshot()` 导出(无外部依赖);
- `telegram.py`:`TelegramNotifier` **只发不收**——只有 sendMessage
  一个出站动作;无入站处理/命令解析/轮询(测试断言公开 API 面);
  发送失败只记警告永不抛出(旁路);未配置 = no-op;
  接入:`NodeDeps.notifier`,仅 executed/exec_rejected/exec_unknown/
  risk_rejected 四种终态发摘要。

## 后续阶段

指导书第 13 节的 0-7 阶段已全部实现;进一步增强(Prometheus 导出、
WebSocket 行情、Freqtrade 回测集成等)留待后续需求。
