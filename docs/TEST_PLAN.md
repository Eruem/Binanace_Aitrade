# 测试计划与结果

## 方法

```bash
pytest                  # 全部
pytest tests/unit       # 单元
pytest tests/safety     # 安全
```

## 阶段 3 覆盖(指导书 13 节阶段 3 验收)

| 验收项 | 测试 | 状态 |
|---|---|---|
| Market 与 Strategy 并行 | `test_agents_flow.py` 并行分支用例(Send 扇出) | ✅ |
| Decision 等待两者完成 | `test_decision_waits_for_both_agents` | ✅ |
| 模型失败时不下单 | LLM 失败/非法 JSON/未知字段/全挂 → 全部合成 HOLD | ✅ |
| 数据错误时不下单 | 缺口/抓取异常 → hold_data_error,零模型调用 | ✅ |
| 工作流状态可追踪 | Checkpointer 按 thread_id 恢复;SQLite 持久化;审计 JSONL | ✅ |
| Schema 约束 | `test_agent_schemas.py`(amount 规则/GRID 拒绝/未知字段) | ✅ |

## 阶段 4 覆盖(指导书 13 节阶段 4 验收)

| 验收项 | 测试 | 状态 |
|---|---|---|
| 超限订单被拒绝 | `test_risk_engine.py` 敞口满/单笔风险/日亏损/连续亏损/风控禁用 | ✅ |
| 过期信号被拒绝 | `test_expired_signal_rejected` | ✅ |
| 余额不足被拒绝 | `test_insufficient_balance_rejected`(余额 0)、`test_too_small_balance_rejected_below_min_notional`(低于最小名义) | ✅ |
| 重复订单被拒绝 | `test_duplicate_order_rejected` | ✅ |
| Kill Switch 开启时禁止新开仓 | `test_kill_switch_blocks_buy`;SELL 豁免 `test_kill_switch_allows_sell`;流程级 `test_kill_switch_blocks_new_buy` | ✅ |
| 风控结果可审计 | 引擎自审 JSONL(`TestAudit`)2 项 + cycle 审计 `risk` 字段(`test_risk_audit_recorded_in_cycle_audit`) | ✅ |
| 仓位计算手算基准 | `test_position_sizing.py` 8 项(5 个上限逐一验证) | ✅ |
| Kill Switch 边界 | `test_kill_switch.py` 8 项(损坏=触发、auto_pause 需人工、LLM 来源拒绝) | ✅ |
| 日亏损限制 | `test_daily_loss.py` 8 项(触限/利润抵消/跨日隔离/损坏拒绝交易) | ✅ |
| 风控接入工作流 | `test_risk_flow.py` 8 项(通过带仓位、否决、账户失败 fail-safe、无引擎兼容) | ✅ |
| 第 14 节 #7 余额不足 | 同上 | ✅ |
| 第 14 节 #8 最小名义金额 | `test_below_min_notional_rejected` | ✅ |
| 第 14 节 #10 风控超限 | 引擎超限用例 + 流程级日亏损/Kill Switch 否决 | ✅ |
| 第 14 节 #11 重复 Client Order ID | `test_duplicate_order_rejected`(decision_id 级;交易所级在阶段 6) | ✅(决策级) |

## 阶段 5 覆盖(指导书 13 节阶段 5 验收)

| 验收项 | 测试 | 状态 |
|---|---|---|
| 完成一笔模拟 BUY | `test_paper_broker.py::test_buy_full_fill_math`(数量/费/余额手算核对) | ✅ |
| 完成一笔模拟 SELL | `test_sell_full_fill_math`(先买后卖一轮,已实现盈亏 -2.00 手算) | ✅ |
| 支持部分成交模拟 | `test_partial_fill_then_complete`(0.004+0.006)、流程级 `test_partial_fill_flow` | ✅ |
| 订单状态流转正确 | 状态机 9 项(`test_order_state.py`)+ history 落盘 `test_state_transitions_recorded` + 意图链 `test_submit_success_persists_intent_before_order` | ✅ |
| 重复提交不会重复记账 | Broker 级 `test_duplicate_submit_is_idempotent`、Manager 级 `test_duplicate_submit_returns_same_order_no_double_book`、流程级 `test_duplicate_cycle_submission_is_idempotent` | ✅ |
| 下单前先持久化意图 | `test_submit_intent_is_on_disk_before_broker_call`(Broker 被调用瞬间读意图文件=SUBMITTING) | ✅ |
| 超时 → UNKNOWN 不换 ID | `test_timeout_marks_unknown_and_never_new_id` | ✅ |
| 超时但交易所已受理 | `test_timeout_but_order_reached_exchange`(恢复查到原单,不重发) | ✅ |
| UNKNOWN 恢复流转 | `test_recover_unknown_finds_filled_order`(UNKNOWN→FILLED 合法) | ✅ |
| 状态损坏拒绝交易 | `test_corrupt_state_refuses_to_trade`、`test_corrupt_intent_file_refuses_orders` | ✅ |
| 滑点方向 | `test_slippage_direction`(BUY 高于参考、SELL 低于参考) | ✅ |
| 无参考价不猜价 | `test_no_price_rejected_not_guessed`、`test_price_hint_fallback` | ✅ |
| 持久化跨重启 | `test_persistence_survives_restart`(订单/余额/成交仍在,重提幂等) | ✅ |
| 风控未放行不执行 | `test_risk_rejected_never_reaches_broker`、`test_execute_without_risk_approval_rejects` | ✅ |
| 无执行层不挂 execute | `test_no_manager_no_execute_node` | ✅ |
| 提交异常 → exec_unknown | `test_broker_exception_marks_unknown`(并注册防重复) | ✅ |
| 无 API Key 接触 | Grep `exchange/` 无 api_key/secret/Authorization;接口层与 Paper 均不持密钥 | ✅ |

## 阶段 6 覆盖(指导书 13 节阶段 6 验收 + 14 节 #9/#12/#14/#15)

| 验收项 | 测试 | 状态 |
|---|---|---|
| 必须确认连接的是 Testnet | `test_binance_client.py::test_sandbox_enabled_before_first_request`(FakeExchange 每次调用断言 sandbox 先行)、`test_url_gate_rejects_non_testnet`(URL 非 testnet.binance.vision → 拒绝构造) | ✅ |
| 正式 API Key 不会被读取 | `test_client_never_reads_env`(源码无 environ/getenv;正式网环境变量存在也无影响)、`test_exception_never_carries_secret`、`test_error_message_never_contains_keys` | ✅ |
| testnet=False 拒绝(Live 未实现) | `test_testnet_false_rejected`、`test_unknown_mode_rejected` | ✅ |
| Testnet 配置校验 | `test_config_loader.py::test_testnet_requires_keys` / `test_execution_mode_must_match_trading_mode` / `test_testnet_keys_masked_in_summary`;工厂双保险 `test_testnet_requires_flag_double_check` / `test_testnet_requires_keys_double_check` | ✅ |
| paper 绝不创建真实客户端 | `test_paper_never_creates_client_even_with_testnet_keys`(注入毒丸 exchange,密钥齐全也只建 PaperBroker) | ✅ |
| BUY 精确金额(quoteOrderQty) | `test_buy_sends_quote_order_qty`(8 位向下取整、clientOrderId 传递、均价手算) | ✅ |
| SELL 数量精度(LOT_SIZE) | `test_sell_quantizes_by_step_and_ticker`(500/50000→0.01000)、`quantize_step` 3 项 | ✅ |
| 低于最小名义不发单 | `test_buy_below_min_notional_rejected_locally` / `test_sell_below_min_notional_rejected`(零下单调用) | ✅ |
| 状态 1:1 映射 | `test_status_mapping_one_to_one`(6 种原生状态)、`test_unknown_native_status_maps_unknown` | ✅ |
| 交易所拒绝 → REJECTED | `test_api_rejection_becomes_rejected`(携带原因,非 UNKNOWN) | ✅ |
| 网络超时 → UNKNOWN 不盲目重发 | `test_network_error_propagates_unknown`(异常上抛,Manager 标记 UNKNOWN)、`test_unknown_absent_released_for_same_id_retry`(确认无单才同 ID 重试)、`test_query_failure_leaves_state_untouched`(断线不动状态) | ✅ |
| 重启后能对账恢复(#15) | `test_crash_restart_recovery`(全新实例从磁盘对账 → 同步 FILLED;恢复后重提幂等零下单)、`test_unknown_synced_when_found_on_exchange` | ✅ |
| 任意异常时停止新开仓 | `test_new_but_absent_is_divergence_and_pauses`(auto_pause)、`test_terminal_*` / `test_rejected_but_exists_*` 不一致 4 项、`test_divergence_blocks_new_buys_via_risk`(Kill Switch 联动风控拒 BUY) | ✅ |
| 手续费明细(计价货币) | `test_fee_from_mytrades_quote_asset_only`(BNB 部分忽略) | ✅ |
| 账户快照 | `test_get_account_marks_positions_at_ticker`(equity/锁定/持仓手算) | ✅ |
| 幂等撤单 | `test_cancel_terminal_order_idempotent` | ✅ |

## 阶段 7 覆盖(指导书 13 节阶段 7 验收 + 10/12/16 节)

| 验收项 | 测试 | 状态 |
|---|---|---|
| 11 张表齐全 | `test_database.py::test_all_guide_tables_exist` + `test_base_metadata_guidance_tables_count` | ✅ |
| 一笔决策全链路可追踪 | `test_decision_traceability.py::test_full_cycle_decision_traceable_end_to_end`(决策→Agent 输出→风控审核→订单→成交→LLM 调用→行情/指标→账户/仓位,端到端 Paper+SQLite) | ✅ |
| HOLD 决策也入库 | `test_hold_cycle_still_records_decision`(NO_ORDER 审计、零订单零成交) | ✅ |
| 订单意图/状态更新单事务 | `test_record_order_transactional_upsert` + `test_order_update_is_single_transaction` | ✅ |
| Client Order ID 唯一 | `test_client_order_id_unique` | ✅ |
| 金额 NUMERIC/Decimal | `test_amount_columns_return_decimal` | ✅ |
| 库内无密钥列 | `test_no_secret_columns_anywhere` + `test_database_schema_has_no_secrets`(按列名片段全表断言) | ✅ |
| 全 cycle 后库内无密钥值 | `test_no_secret_values_in_db_after_full_cycle` | ✅ |
| 迁移幂等 | `test_migrations_apply_once` | ✅ |
| DB 故障降级不影响 cycle | `test_db_failure_degrades_to_jsonl_and_cycle_completes`(BrokenRecorder → 仍 executed,JSONL 完整) | ✅ |
| 日志字段符合 §12 | `test_monitoring.py::test_json_formatter_includes_standard_fields`(12 个标准字段) | ✅ |
| 日志脱敏(API Key/Bearer/Token/口令) | `test_redact_*` 5 项 + `test_json_formatter_never_leaks_secret_in_extra` + `test_redaction_filter_applies_to_record` | ✅ |
| 指标覆盖 §12 监控清单 | `test_metrics_monitoring_coverage`(9 类指标逐项)+ 延迟统计/线程安全 | ✅ |
| Telegram 只发不收 | `test_telegram_has_no_command_execution_surface`(公开 API 面 + 源码无 getUpdates/webhook/下单词) | ✅ |
| Telegram 失败不影响主流程 | `test_telegram_failure_never_raises` + `test_telegram_http_error_returns_false` | ✅ |
| Telegram Token 不进消息体 | `test_telegram_summary_message_has_no_secret` | ✅ |
| database.url scheme 门禁 | `test_database_url_scheme_rejected`(错误只含 scheme 名,不含口令) | ✅ |
| 口令/Token 脱敏摘要 | `test_database_url_masked_in_summary` + `test_telegram_token_masked_in_summary` | ✅ |
| Telegram 成对配置 | `test_telegram_partial_config_rejected` | ✅ |

## 阶段 2 覆盖

| # | 场景 | 测试 | 状态 |
|---|---|---|---|
| 5 | 行情数据缺失/重复/倒序/坏 OHLC/NaN | `test_data_validator.py` | ✅ |
| 6 | K 线尚未收盘 | `test_data_validator.py` unclosed_last | ✅ |
| — | 数据过期 | `test_data_validator.py` stale | ✅ |
| — | 多周期对齐 | `test_data_validator.py` alignment | ✅ |
| — | 指标手算基准(RSI/ATR/EMA/MACD/BB) | `test_indicators.py` | ✅ |
| — | CCXT 客户端转换(假交易所) | `test_binance_data.py` | ✅ |
| — | Vision 归档解析(含微秒时间戳归一) | `test_binance_vision.py` | ✅ |
| — | 真实公开行情端到端 | `test_binance_vision_public.py` | ✅(真实数据) |
| — | api.binance.com 直连 | `test_binance_public.py` | ⏭ 本网络不可达,自动跳过 |

## 阶段 0/1 覆盖(对应指导书第 14 节条目,当前阶段可覆盖者)

| # | 场景 | 测试 | 状态 |
|---|---|---|---|
| 1 | LLM API 超时 | `test_providers.py` 超时 → retryable | ✅ |
| 2 | LLM 返回空内容 | `test_router.py` / `test_response_parser.py` | ✅ |
| 3 | LLM 返回非法 JSON | `test_response_parser.py` | ✅ |
| 4 | 主备模型全部不可用 | `test_router.py` → SafeFailure(HOLD) | ✅ |
| 16 | Kill Switch 类:限额 | `test_router.py` max_calls_per_cycle | ✅ |
| 17 | 配置指向正式交易环境 | `test_config_loader.py` live 拒绝启动 | ✅ |
| 18 | 日志脱敏 | `test_safety_secrets.py` | ✅ |

尚未覆盖(依赖后续阶段):数量精度(交易所规则)、与真实交易所的
UNKNOWN 对账(阶段 6);部分成交、下单超时、崩溃重启恢复、
重复 Client Order ID 已在阶段 5 以 Paper 模拟覆盖(交易所级核对在阶段 6)。

## 结果

### 运行记录

- **2026-10-31 阶段 0/1 验收**(`.venv` + Python 3.14,Windows):
  - `pytest` → **58 passed, 0 failed**(2.04s)
    - `tests/unit/test_config_loader.py` 13 项
    - `tests/unit/test_complexity.py` 5 项
    - `tests/unit/test_response_parser.py` 8 项
    - `tests/unit/test_providers.py` 10 项
    - `tests/unit/test_router.py` 13 项
    - `tests/unit/test_usage_tracker.py` 3 项
    - `tests/safety/test_safety_secrets.py` 6 项
  - `scripts/check_config.py`(注入测试变量)→ 退出 0,输出中 api_key 为 `****`
  - `main.py` → 退出 0,横幅显示 `TRADING MODE: PAPER` / `LIVE TRADING: DISABLED`
  - `ENABLE_LIVE_TRADING=true` 时 `main.py` → 退出 1,拒绝启动(安全测试覆盖)

首轮曾有 13 项失败,已修复后全绿:
1. `config/models.yaml` 注释中的占位符示例被插值引擎误解析 → 改注释写法;
2. `parse_llm_json` 未显式拒绝未知字段(依赖 schema 配置不可靠)→ 解析层直接拒绝未声明字段;
3. 测试夹具在无 backup provider 时注册了 backup 别名;router 对未注册别名缺兜底 → 夹具修正 + router 增加候选过滤。

- **2026-10-31 阶段 2 验收**:
  - `pytest` → **111 passed, 1 skipped**
    - 新增单元:`test_market_data.py` 7、`test_data_validator.py` 13、
      `test_indicators.py` 17、`test_binance_data.py` 5、`test_binance_vision.py` 7
    - 新增集成:`test_binance_vision_public.py`(**真实 Vision 归档数据端到端通过**)
  - 跳过项:`test_binance_public.py`(api.binance.com 在本网络连接超时,
    按设计自动 skip;不影响其余测试)
  - 修复:Vision 归档时间戳为微秒(16 位),解析层已做单位归一并有回归测试
  - 环境发现:`api.binance.com` 不可达,但 `data.binance.vision` 可达;
    本机可用 Vision 数据源读取真实公开行情

- **2026-10-31 阶段 3 验收**:
  - `pytest` → **137 passed, 1 skipped**
    - 新增:`test_agent_schemas.py` 11 项、`test_agents_flow.py` 15 项
      (并行/等待/6 类安全降级/Checkpointer 3 项)
  - 开发中修复的三个 langgraph 集成问题:
    1. 条件路由 path_map 不支持列表 fan-out → 改用 `Send` API;
    2. lambda 节点丢失协程标记导致协程未 await → 改用 `functools.partial`;
    3. 同步 SqliteSaver 不支持异步图 → AsyncSqliteSaver + aiosqlite

- **2026-10-31 阶段 4 验收**:
  - `pytest` → **189 passed, 1 skipped**
    - 新增:`test_position_sizing.py` 7、`test_kill_switch.py` 7、
      `test_daily_loss.py` 8、`test_risk_engine.py` 22、`test_risk_flow.py` 8
      (合计 52 项风控测试;全量从 137 → 189)
  - 开发中修复:风控 HOLD 路径曾把终态误标为 `risk_rejected`,
    改为 `NO_ORDER` 时保留 `hold_*` 终态(risk_node 三态区分);
    风控自审与 cycle 审计分流为独立文件,避免互相污染行数断言
  - 安全边界验证:`test_engine_has_no_llm_interface` 确认风控引擎不暴露
    任何 LLM/模型接口;损坏的 Kill Switch 状态=已触发、损坏的日亏损
    状态=拒绝交易(两个 fail-safe 反向测试)

- **2026-10-31 阶段 5 验收**:
  - `pytest` → **232 passed, 1 skipped**
    - 新增:`test_order_state.py` 9、`test_paper_broker.py` 16、
      `test_order_manager.py` 10、`test_execute_flow.py` 8
      (合计 43 项执行层测试;全量从 189 → 232)
  - 开发中修复:手算笔误(9998.4995-0.4995=9998.0000,代码正确);
    SELL 流程测试的 decision_id "ignored" 不足 8 字符被 Schema 拒绝
    (与主流程 DECISION_JSON 同样用 "ignored-by-system");
    in-memory 模式(无 state_path)最初每次读取都返回空白状态,
    改为进程内共享 dict
  - 安全边界验证:`exchange/` 全包 grep 无任何 api_key/secret/Authorization;
    无风控放行 → 不执行(fail-safe 双测试);状态文件损坏 → 拒绝交易
  - 网络依赖说明:固定 skip 仅 `test_binance_public.py`(api.binance.com
    不可达);`test_binance_vision_public.py` 为真实 Vision 数据端到端,
    网络抖动时临时 skip、重跑即过(本次验收最终运行为通过)

- **2026-11-01 阶段 7 验收(全部 0-7 阶段完成)**:
  - `pytest` → **346 passed, 1 skipped**
    - 新增:`test_database.py` 16、`test_monitoring.py` 19、
      `test_decision_traceability.py` 6(集成)、
      `test_config_loader.py` +8(共 49 项;全量从 297 → 346)
  - 开发中修复:
    1. 敏感列名检查把 `input_tokens`(LLM token 计数)误报为 token
       密钥列 → 改为按列名片段精确匹配(secret/password/token/key 段,
       复数 tokens 合法);
    2. Pydantic field_validator 抛 ValueError 时 ValidationError 会回显
       完整输入值(数据库 URL 口令随之泄漏)→ scheme 校验移到
       ConfigSet 层,报错只含 scheme 名;
    3. YAML 把纯数字 chat_id 解析为 int 导致校验失败 → before
       validator 统一转字符串;
    4. positions 快照语义:账户快照是 risk 时点的,买入当轮仓位为空
       是正确行为(第二轮 HOLD 才带出仓位)——测试按此语义断言。
  - 验收确认:一笔决策经 `trace_decision` 可查到 Agent 输出、风控
    审核、订单、成交、LLM 调用、行情/指标、账户/仓位全链路;
    日志/摘要/库内三处均无密钥;DB 故障降级路径实测(cycle 照常
    executed,JSONL 完整);Telegram 源码与公开 API 面确认无任何
    入站/执行接口。
  - 依赖变化:requirements.txt 收录 `sqlalchemy==2.1.6` +
    `psycopg[binary]==3.3.6`(同步引擎;测试用 SQLite);
    docker-compose DATABASE_URL scheme 改为 `postgresql+psycopg://`。

- **2026-11-01 阶段 6 验收**:
  - `pytest` → **297 passed, 1 skipped**
    - 新增:`test_binance_client.py` 21、`test_testnet_broker.py` 19、
      `test_reconciler.py` 13、`test_broker_factory.py` 9、
      `test_config_loader.py` +3(共 65 项;全量从 232 → 297)
  - 开发中修复:
    1. `_to_result` 是同步函数但调用异步 `_fee_for`(协程未 await,
       TypeError)→ `_to_result` 改为 async 并在三处调用点 await;
    2. `to_exchange_symbol("BTC/USDT:USDT")` 把结算后缀拼进交易所符号
       (BTCUSDTUSDT)→ 先剥离 `:...` 再去斜杠;
    3. 阶段 5 的两个测试桩 Broker 未同步新签名 `get_order(..., symbol=)`
       → 桩方法补 `symbol=None` 透传;
    4. 工厂测试的最小 ModelsFile 未关 fallback 导致 ConfigSet 构造失败
       → `FallbackConfig(enabled=False)`;testnet 双保险测试先过配置层
       校验再翻转开关模拟绕过
  - 安全边界验证:全新测试确认 sandbox 先于首个请求(FakeExchange 内置
    断言)、URL 门禁拒绝非 Testnet、客户端源码无 environ/getenv、
    密钥不出现在任何异常文本;`exchange/` grep 仅构造参数传参,
    无日志输出密钥路径
  - 全部测试离线:Testnet 客户端/Broker 均注入 FakeExchange/FakeClient,
    验收运行零真实网络请求(api.binance.com 不可达不影响)

- **2026-11-01 阶段 8 验收**(代理注入、Live 真实资金交易、驱动循环;
  项目所有者显式授权解除"禁止真实资金"约束,修订指导书第一阶段边界):
  - `pytest` → **376 passed, 1 skipped**
    - 新增:`test_config_loader.py` +10(proxy 4 + Live 6)、
      `test_binance_data.py` +3、`test_binance_client.py` +6(proxy 2 + Live 4)、
      `test_broker_factory.py` +7(proxy 1 + Live 6)、
      `test_run_agent.py` +4(全量 346 → 376)
  - 现有安全测试零破坏:paper+flag=true 依旧拒绝、无 allow_live 构造
    依旧拒绝、工厂未知模式依旧拒绝、客户端源码无 environ/getenv
  - 开发中发现并修复:
    1. CCXT 异步(aiohttp)**不消费** `.proxies` 字典,且 `http_proxy`
       与 `https_proxy` 同设会抛 InvalidProxySettings → 统一注入
       `https_proxy` 属性(本项目流量全为 https);
    2. 工厂双保险测试在 ConfigSet 构造时就被配置层拒绝(纵深防御生效)
       → 改为"合法构造后篡改再断言"模式;
    3. test_binance_data 的 FakeExchange 需要 rows 参数 → 修正构造
  - 真实网络冒烟(只读公共行情,无密钥):
    同步/异步 ccxt 经 `https_proxy=http://127.0.0.1:10808`
    均成功获取 `BTC/USDT` ticker(api.binance.com);
    `testnet.binance.vision` 走代理 HTTP 200
  - Live 边界验证(全离线):四要素缺一不可(逐项测试)、
    Live 绝不 sandbox、Testnet URL 指向拒绝、paper 毒丸零触碰、
    驱动脚本装配挂载 risk_node/execute_node
  - Telegram 启用(同日追加):通知器显式代理注入(经
    `EXCHANGE_PROXY_URL`,httpx `proxy=` 参数)+ 注入 client 优先
    2 项测试 → 全量 **378 passed, 1 skipped**;真实链路验证:
    经代理 `getUpdates` 取 chat_id、`TelegramNotifier.send`
    实发测试消息成功

- **2026-11-02 阶段 9 验收(只读监控面板,Streamlit)**:
  - `pytest` → **399 passed, 1 skipped**
    - 新增:`test_dashboard_data.py` 21 项(只读数据层 17 +
      安全断言 3 + AppTest 整页冒烟 1;全量 378 → 399)
  - 架构:数据/展示分离——`monitoring/dashboard_data.py`
    纯只读函数(JSONL 解析、坏行跳过、循环活跃判断、KPI 汇总、
    展示行构造),不依赖 streamlit/网络;`scripts/dashboard.py`
    仅做展示;
  - 安全断言:面板脚本无交易/配置/密钥相关接口(按源码禁词),
    数据层无环境变量读取与写入 API,`.streamlit/config.toml`
    固定 `127.0.0.1` 且遥测关闭;
  - 真实冒烟:headless 实例 HTTP 200 + `_stcore/health` ok;
    Streamlit AppTest 整页渲染无异常(标题/9 指标/4 标签页);
  - 依赖:`streamlit==1.66.1` 锁定(Python 3.14 验证安装)。

- **2026-11-02 Live 首连修复(三连:"测试桩测不出"类问题)**:
  - 背景:首次真实 Live 构造与 `--once` 运行连续暴露三个离线测试
    桩覆盖不到的缺陷,逐个修复并补真实 ccxt 回归:
  - `pytest` → **405 passed, 1 skipped**(399 → 401 → 402 → 405)
    1. `_confirm_live` 对**全量** ccxt `urls` 求 repr——真实 ccxt 的
       urls 天生带 `.test` 模板区(含 testnet 主机,即使从未开
       sandbox)→ Live 检查永远误杀 → 改为只看活跃区 `urls["api"]`
       (+2 真实 ccxt 离线回归);
    2. 客户端调用 `public_get_tickerprice`,真实 ccxt 上的隐式方法名
       是 `public_get_ticker_price`(路径 `ticker/price` 带斜杠)→
       首次真实私有调用(账户快照给持仓标记行情)AttributeError →
       修正方法名,并新增"从客户端源码提取全部隐式方法名、断言真实
       ccxt 均存在"的回归测试(+1);
    3. Live 账户持有无现货市场的理财收据资产 `LDBTTC`(X/USDT 不存在)
       → 标记行情 BadSymbol 废掉整个账户快照 → 风控永久 fail-safe
       否决一切 → `get_account` 对 BadSymbol 跳过该资产,其余行情
       失败(断线/其他 API 错误)照常上抛否决(+3)。
  - 真实只读验证(全程零下单):Live 账户快照经代理拉取成功——
    可用 0 USDT + JST 灰尘(1e-8)标记正常,LDBTTC 无市场被跳过;
    密钥/代理/读权限均正常。

- **2026-11-02 Docker 部署(常驻 loop + 面板)**:
  - `pytest` → **406 passed, 1 skipped**(405 → 406,新增
    `test_setup_logging_file_writes_redacted_json`:文件日志 JSON 行
    + 脱敏断言);
  - 镜像:python:3.14-slim(Docker Hub 被阻断,经 docker.m.daocloud.io
    拉取后本地 tag);`.dockerignore` 排除 `.env`/`var`/`tests`,
    已验证镜像内无 `.env`、ENV 无任何密钥,镜像级默认仍是 paper
    (裸 docker run 无 .env 绝不触碰真实资金);
  - compose:`loop`(`--loop --interval 900`,env_file .env,代理覆盖为
    `host.docker.internal:10808`,`./var` 读写 + `./config` 只读,
    unless-stopped)+ `dashboard`(发布 `127.0.0.1:8501`,只读挂载,
    容器内 `STREAMLIT_SERVER_ADDRESS=0.0.0.0`,healthcheck)+
    `postgres`(profile `db` 可选,默认不启动);
  - 开发中修复:
    1. `run_agent` 从未接线结构化日志 → `setup_logging` 增加 `file`
       参数(stderr JSON + `var/agent_loop.log`,面板按 mtime 判循环
       活跃),循环内新增 `cycle_started/cycle_completed` 结构化事件;
    2. Streamlit 容器崩溃循环(exit 255):非交互环境无凭据文件时
       Email onboarding 提示读 stdin EOF → `config.toml` 增加
       `showEmailPrompt = false` + compose 命令 `--server.headless true`;
  - 真实验证:容器内只读账户检查通过(`host.docker.internal` 代理
    直通,无需改代理设置);常驻循环对用户配置的 16 个交易对逐个
    决策(全部 hold_no_trade/NO_ORDER,llm/data 错误均为 0),
    面板 health `ok`、HTTP 200,`var/agent_loop.log` 持续写入。

- **2026-11-02 时钟漂移修复(-1021 InvalidNonce)**:
  - 现象:14:45–16:09 连续多轮账户快照失败,风控 fail-safe 否决
    全部周期(Telegram 刷屏 `risk_rejected`,note 为
    `InvalidNonce: Timestamp ... 1000ms ahead of the server's time`);
  - 根因:Windows 时间服务从未 NTP 同步(源 = 主板 CMOS 时钟),
    本地钟漂移超过币安 ±1s 签名容差;16:10–16:25 之间时钟回落到
    容差内后自愈(16:25 轮恢复 hold_no_trade)——不修必然复发;
  - 修复:私有(签名)请求前对拍服务器时间——`_ensure_time_sync`
    调 ccxt `load_time_difference()`(公共 `/api/v3/time`,免签名),
    把 `options['timeDifference']` 钉在服务器时间上,签名时间戳 =
    本地钟 − 差值;300 秒冷却、对拍失败不阻断(签名真失败仍由上层
    fail-safe 兜底)、测试桩无该方法自动跳过;ccxt options 同步开
    `adjustForTimeDifference=True`(将来走 load_markets 的双保险);
  - `pytest` → **409 passed, 1 skipped**(406 → 409,新增:冷却期内
    只对拍一次且过期重拍、对拍失败不阻断私有请求、无该方法跳过);
  - 真实验证:账户快照成功,`timeDifference=168ms`(本地超前 168ms
    已记录并从签名中扣除);容器已 `up -d --build` 重建上线。

- **2026-11-02 用量落盘接线 + 消耗调优**:
  - 发现:`UsageTracker` 支持 `sink_path` 但 `from_configset` 创建时
    从未传入,`settings.paths.usage_log` 形同虚设,历史 token 消耗
    无从统计 → 接线(+1 测试 `test_from_configset_wires_usage_sink`);
  - 实测首批数据(5 条):market/fast 平均 in991/out430、
    strategy/reasoning in1871/out813、decision/reasoning in3226/out309,
    单币对一轮 ≈7.6k tokens;按 16 对 × ~28 分钟节奏估算 ~6.2M/天;
  - 调优(项目所有者批准):compose `--interval 900→1800`;
    `trading.yaml` symbols 16→6(BTC/ETH/BNB/SOL/DOGE/ADA);
    面板 `loop_activity` 默认窗口同步 900→1800(否则间隔拉长后
    面板误报 stale);OPERATIONS 服务说明同步;
  - `pytest` → **410 passed, 1 skipped**(边界测试显式传
    `interval_s`,不受默认值变更影响);
  - 实测:容器 Cmd 确认 `--interval 1800`,新周期仅遍历 6 对,
    `hold_no_trade/NO_ORDER` 正常,面板 healthy。
  - 预期消耗:约 1M/天(原 ~6.2M 的 1/6)。
