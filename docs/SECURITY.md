# 安全边界

## 密钥管理

- 密钥只通过本地 `.env` 提供;`.env.example` 只含空占位符;
- `.env` 在 `.gitignore` 中,禁止提交、禁止打包进 Docker 镜像;
- 不得把密钥粘贴到聊天、代码、日志、数据库普通日志、前端或异常信息里;
- `config/loader.py` 的 `mask_secret()` 对所有展示场景输出 `****`,
  不输出原值或任何片段;
- Provider 的 `__repr__` 显式覆盖,防止调试打印泄漏 api_key。

## 交易安全

- 默认 `TRADING_MODE=paper`;
- `ENABLE_LIVE_TRADING=true` → 启动校验直接拒绝(本版本不实现真实交易);
- `TRADING_MODE=testnet` 与 `ENABLE_BINANCE_TESTNET=true` 必须成对;
- Testnet 与正式 API Key 分开配置,互不读取;
- 第一阶段仅 Spot 现货;禁止合约、杠杆、借贷。

## LLM 安全

- LLM 输出不是可信指令:必须经 Pydantic Schema + 业务校验;
- 模型全部不可用 → `SafeFailure(action=HOLD)`,不产生任何交易指令;
- `on_all_models_failed` 只允许 `HOLD`,配置校验强制;
- 复杂度评分只影响模型选择,不能影响风控阈值;
- LLM 重试策略不适用于非幂等的交易所下单请求(阶段 5/6 遵守)。

## 风控安全(阶段 4)

- `risk/` 全部是确定性 Python,LLM 不能修改限额、自行审批、覆盖
  Kill Switch 或在风控否决后要求执行;
- `RiskEngine` 不暴露任何 LLM/模型接口(测试断言);
- Kill Switch 状态文件损坏 = 视为已触发;恢复只能经人工 `release`,
  LLM 来源的触发请求被拒绝;
- 日亏损状态损坏 = 拒绝交易(不静默放宽);
- 未配置风控引擎 / 账户快照获取失败 = 否决(fail-safe);
- `risk.enabled=false` 视为停机,拒绝一切新订单。

## 执行安全(阶段 5)

- `exchange/` 是唯一可以创建订单的模块;本阶段只有 Paper Broker,
  整个包 grep 不到任何 api_key/secret/Authorization(测试断言 + 代码审查);
- 无风控放行绝不执行:execute_node 只在风控引擎 + OrderManager
  同时注入时挂载;节点内部再校验 risk_result(双重 fail-safe);
- Client Order ID 由 decision_id 确定性生成:同一决策永远同一 ID,
  重复提交幂等返回,绝不重复记账;
- 下单前先持久化订单意图;提交异常 → UNKNOWN;
  UNKNOWN 恢复只能查询原单,**未确认前不得生成新 Client Order ID 重发**;
  结果未知时同样注册防重复(宁可阻止,不重复下单);
- 订单状态机强制合法流转,非法跳变抛异常(终态不可变);
- Paper 状态文件 / 意图文件损坏 → 拒绝交易(无法保证幂等就不下单);
- Paper 成交价格来自注入的参考价,无参考价即拒绝,不猜测价格。

## Testnet 安全(阶段 6)

- 客户端默认只连 Testnet:`testnet=False` 必须显式 `allow_live=True`
  (阶段 8 修订;默认拒绝延续 fail-safe);
- 客户端**从不读取任何环境变量**(源码断言):密钥只经构造参数从
  配置注入;任何 `BINANCE_*` 环境变量存在也不会被客户端自行使用;
- set_sandbox_mode(True) 必须在首个请求前启用,并校验连接 URL 指向
  `testnet.binance.vision`,否则拒绝一切请求(测试:FakeExchange 在
  每次 API 调用时断言 sandbox 已启用);
- Testnet 密钥与正式密钥完全分开(`BINANCE_TESTNET_API_KEY/SECRET`);
  配置错误信息只含环境变量名,不含密钥值;
- `TRADING_MODE=paper` 时工厂绝不创建真实下单客户端(即使 Testnet
  密钥齐全;测试以毒丸 exchange 对象断言零触碰);
- 异常文本不携带密钥(测试断言);
- 对账发现本地与交易所不一致 → Kill Switch auto_pause 停止新开仓,
  恢复只能人工 release;
- 网络超时 → UNKNOWN,绝不当作"订单不存在"盲目重发;
  只有查询确认交易所无此单,才允许同一 Client Order ID 重试。

## Live 真实资金安全(阶段 8,项目所有者显式授权)

Live 是可选能力,默认关闭;开启需四要素同时满足,配置层与工厂层
各校验一遍(纵深防御),任一缺失即拒绝启动:

1. `TRADING_MODE=live`;
2. `ENABLE_LIVE_TRADING=true`(paper/testnet 模式下为 true 依旧拒绝);
3. 独立 Live 密钥 `BINANCE_LIVE_API_KEY/SECRET`(与 Testnet 密钥
   完全分开、禁止复用;只写本地 `.env`;脱敏摘要中恒为 `****`);
4. `risk.enabled=true`(真实资金模式不允许关闭风控引擎)。

附加边界:

- Live 客户端**绝不**启用 sandbox;URL 必须确认指向 `api.binance.com`,
  指向 Testnet 或未知地址一律拒绝(与 Testnet URL 门对称);
- 订单状态机、Client Order ID 幂等、对账、UNKNOWN 恢复与 Testnet
  共用同一套代码(真实资金下同样"绝不盲目重发");
- 建议正式账户 API 权限只勾选"现货交易",不要提现权限;
- 启动横幅强制显示 `LIVE TRADING: *** ENABLED` 警告;
- docker-compose 始终强制 paper,不会带出 live 配置。

## 代理安全(阶段 8)

- `EXCHANGE_PROXY_URL` 只允许 `http://`/`https://`;URL 内的
  userinfo(可能就是代理账号口令)在一切摘要/错误中整段遮蔽;
- 代理只是传输通道:Testnet/Live 密钥同样只在客户端内部,
  代理无法读取(HTTPS 端到端加密);
- 不配置代理 = 直连;系统代理**不会**被自动读取(CCXT trust_env=False
  是有意设计,防止请求被静默改道)。

## 数据库与监控安全(阶段 7)

- 数据库 11 张审计表**没有任何 API Key/Secret/Token/密码列**
  (测试按列名片段全表断言);decision_id 贯穿决策→风控→订单→成交,
  一笔决策可完整追踪;
- database.url 只允许 postgresql/SQLite scheme;数据库口令只经环境
  变量 DATABASE_URL 提供;错误信息只含 scheme 名,不回显完整 URL
  (避免口令随 ValidationError 泄漏);
- `sanitized_summary()` 对数据库 URL 隐藏 user:password、
  对 Telegram Token/Chat ID 输出 `****`;
- 结构化 JSON 日志在 formatter 前后双重脱敏(RedactionFilter +
  整行兜底正则):sk-… 密钥、Bearer/Authorization、token/password
  键值、Telegram Bot Token、URL 内嵌口令(测试逐项断言);
- Telegram **只发送通知**:模块无入站处理、无命令解析、无轮询
  (测试断言公开 API 面与源码不含 getUpdates/webhook/下单词);
  发送失败只记警告,绝不影响交易主流程;
- 数据库故障自动降级为仅 JSONL 审计(db_write_failed 事件留痕),
  交易主流程不受影响(测试:DB 挂掉后 cycle 仍正常 executed);
- 指标为进程内内存计数,不含任何敏感值。

## 已知待加强

- 数据库表当前无行级保留策略(长期运行需定期归档/清理);
- 指标无 Prometheus 导出端点(运维用 `snapshot()` 或脚本查询)。
