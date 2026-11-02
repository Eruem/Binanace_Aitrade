# AI Crypto Trading Agent

基于动态模型路由、多 Agent 协作、独立程序风控和 Binance Spot Testnet 的自动交易**研究系统**。

> **安全声明**
> - 默认只允许行情读取、分析、回测和模拟交易(Paper / Spot Testnet)。
> - 真实资金交易(Live)为**可选显式开启**:`TRADING_MODE=live` +
>   `ENABLE_LIVE_TRADING=true` + 独立 Live 密钥 + `risk.enabled=true`
>   四要素缺一不可;**paper/testnet 模式下 `ENABLE_LIVE_TRADING=true`
>   依旧被启动校验直接拒绝**。
> - 本项目不承诺收益;模型置信度不是真实概率;回测收益不代表实盘盈利;
>   **真实资金交易的全部损失由账户所有者承担**。

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 环境与脚手架、配置加载/校验/脱敏 | ✅ 完成,测试通过 |
| 1 | LLM Adapter(Provider/Registry/Router/复杂度/备用/解析/用量) | ✅ 完成,测试通过 |
| 2 | 行情与指标(CCXT) | ✅ 完成,测试通过 |
| 3 | Agent 与 LangGraph | ✅ 完成,测试通过 |
| 4 | Risk Engine | ✅ 完成,测试通过 |
| 5 | Paper Broker | ✅ 完成,测试通过 |
| 6 | Binance Spot Testnet | ✅ 完成,测试通过 |
| 7 | 数据库、监控、Telegram 通知、文档完善 | ✅ 完成,测试通过 |
| 8 | 交易所代理注入、Live 真实资金交易(显式四重校验)、驱动循环脚本 | ✅ 完成,测试通过 |
| 9 | 只读监控面板(Streamlit,仅监听 127.0.0.1) | ✅ 完成,测试通过 |

未实现的模块不提供假实现;对应目录里只有说明文件。

## 环境要求

- Python 3.14(本机与 Docker 镜像均为 3.14-slim)
- 可选:Docker / Docker Compose

## 安装

```bash
cd ai-crypto-trading-agent
python -m venv .venv
source .venv/Scripts/activate     # Windows Git Bash;Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt
```

## 配置

```bash
cp .env.example .env
# 编辑 .env,填入你自己的 OpenAI 兼容 API 配置
```

`.env` 已被 `.gitignore` 排除。**不要把密钥粘贴到聊天、代码或提交里。**

需要设置的最小集合(以某 OpenAI 兼容服务为例):

```dotenv
LLM_PRIMARY_BASE_URL=https://your-provider.example/v1
LLM_PRIMARY_API_KEY=你的密钥
LLM_FAST_MODEL=模型ID-快
LLM_REASONING_MODEL=模型ID-推理
LLM_SECONDARY_BASE_URL=https://备用服务/v1
LLM_SECONDARY_API_KEY=备用密钥
LLM_BACKUP_MODEL=备用模型ID
```

模型别名(`fast`/`reasoning`/`backup`)与任务映射在 `config/models.yaml`。

## 运行

```bash
python scripts/check_config.py    # 配置检查(输出已脱敏)
python main.py                    # 启动横幅与组件清单(Paper 模式)
python scripts/migrate_db.py      # 可选:启用 DATABASE_URL 后应用数据库迁移

# 驱动循环(真正跑起来的入口;阶段 8)
python scripts/run_agent.py                 # 单轮:每个配置的交易对各跑一轮
python scripts/run_agent.py --loop --interval 300   # 常驻循环(每 300 秒一轮)

# 只读监控面板(阶段 9;浏览器自动打开 http://localhost:8501)
python -m streamlit run scripts/dashboard.py
```

Docker(常驻部署:交易循环 + 只读面板,详见 docs/OPERATIONS.md):

```bash
docker compose up -d              # 启动 loop + dashboard(重启自恢复)
docker compose logs -f loop       # 跟踪交易循环日志(JSON 行)
docker compose restart loop       # 重启循环
docker compose down               # 停止并移除容器(宿主机 var/ 数据保留)
```

## 测试

```bash
pytest                           # 全部测试
pytest tests/unit                # 单元测试
pytest tests/safety              # 安全测试
```

测试结果见 `docs/TEST_PLAN.md`。

## 如何确认当前连接的是哪个账户环境

1. 启动横幅:`TRADING MODE: PAPER/TESTNET/LIVE` 与
   `LIVE TRADING: DISABLED/ENABLED` 一目了然;
2. paper/testnet 模式下 `ENABLE_LIVE_TRADING=true` 会被启动校验直接拒绝
   (配置层与工厂层双重校验);
3. Binance 客户端默认 `testnet=True`(需 sandbox + `testnet.binance.vision`
   URL 确认);Live 连接必须显式 `allow_live=True`,且 URL 必须指向
   `api.binance.com`、绝不启用 sandbox;
4. `TRADING_MODE=paper` 时工厂**绝不**创建任何交易所客户端
   (即使 Testnet/Live 密钥已配置,注入毒丸测试证明零触碰);
5. Docker 镜像级默认 `TRADING_MODE=paper`、`ENABLE_LIVE_TRADING=false`
   (裸 `docker run` 无 `.env` 绝不触碰真实资金);`.dockerignore` 排除
   `.env`,密钥只经 compose `env_file` 运行时注入(已验证镜像内无 `.env`);
6. 日志与 `check_config.py` 输出对所有密钥做 `****` 脱敏。

## 目录结构

见指导书第 4 节;本项目按其创建。当前实现主体:`config/`、`llm/`、`data/`、
`agents/`、`risk/`、`exchange/`、`database/`、`monitoring/`、`scripts/`、`tests/`。

## 文档

- `docs/ARCHITECTURE.md` — 架构与模块职责
- `docs/SECURITY.md` — 密钥、权限与安全边界
- `docs/OPERATIONS.md` — 启动、停止、恢复、排错
- `docs/TEST_PLAN.md` — 测试方法与结果

## 启用 Binance Spot Testnet(可选,阶段 6)

默认 `TRADING_MODE=paper`,无需任何交易所密钥即可运行。若要连接 Testnet:

1. 在 [testnet.binance.vision](https://testnet.binance.vision/) 申请**独立的**
   Testnet API Key/Secret(与正式账户密钥完全分开);
2. 在 `.env` 中设置(**不要**粘贴到聊天、代码或提交里):

```dotenv
TRADING_MODE=testnet
ENABLE_BINANCE_TESTNET=true
BINANCE_TESTNET_API_KEY=你的Testnet密钥
BINANCE_TESTNET_API_SECRET=你的Testnet密钥Secret
```

3. `python scripts/check_config.py` 确认脱敏摘要中 `execution.mode=testnet`。

系统在首个请求前强制 sandbox 模式并校验 URL 指向 `testnet.binance.vision`,
否则拒绝连接;`testnet=False` 的构造直接抛错(本版本不实现 Live)。

## 启用真实资金交易 Live(可选,阶段 8,高风险)

> ⚠️ **这是真实资金。** 默认关闭;开启前请确认你理解:
> 任何下单都花你账户里的真钱;系统不承诺收益;损失由你承担。
> 风控引擎在此模式下**强制开启且无法配置关闭**。

前置条件(缺一不可,任一不满足启动即拒绝):

1. 已申请 Binance **正式账户** API Key,权限**只勾选"现货交易"**
   (不要提现权限),Key/Secret 只写入本地 `.env`(**绝不**粘贴到聊天、
   代码或提交);与 Testnet 密钥完全分开、禁止复用;
2. `.env` 设置:

```dotenv
TRADING_MODE=live
ENABLE_LIVE_TRADING=true
BINANCE_LIVE_API_KEY=你的正式账户Key
BINANCE_LIVE_API_SECRET=你的正式账户Secret
# 本网络 api.binance.com 直连被阻断,必须走代理:
EXCHANGE_PROXY_URL=http://127.0.0.1:10808
```

3. `python scripts/check_config.py` 确认脱敏摘要:
   `trading_mode=live`、`execution.broker=binance_live_broker`、
   `risk.enabled=true`;
4. `python scripts/run_agent.py` 启动(横幅会显示
   `LIVE TRADING: *** ENABLED` 警告)。

安全确认点:Live 客户端**绝不**启用 sandbox,且构造时校验 URL 必须
指向 `api.binance.com`(指向 Testnet 或未知地址一律拒绝);订单状态机、
幂等 Client Order ID、对账与 UNKNOWN 恢复与 Testnet 完全同一套代码。

## 启用数据库审计与 Telegram 通知(可选,阶段 7)

数据库与通知默认关闭(仅本地 JSONL 审计),启用方式:

```dotenv
# PostgreSQL(推荐,配合 docker compose 的 postgres 服务):
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/crypto_agent
# 或本地 SQLite(无 PostgreSQL 时):
# DATABASE_URL=sqlite:///var/agent.db

# Telegram(仅发送通知;两者必须同时设置):
TELEGRAM_BOT_TOKEN=你的BotToken
TELEGRAM_CHAT_ID=你的聊天ID
```

启用后:

- `python scripts/migrate_db.py` 应用数据库迁移(幂等);
- 一笔决策可经 `database.repository.Recorder.trace_decision(decision_id)`
  追踪到 Agent 输出、风控审核、订单与成交;
- 日志为 JSON 行(含 cycle_id/decision_id/latency_ms 等结构化字段),
  API Key/Secret/Authorization/Telegram Token/数据库口令一律脱敏;
- Telegram **只发送通知**,绝不执行聊天文本中的任何指令;
- 数据库故障不影响交易主流程(自动降级为仅 JSONL 审计)。

## 启用只读监控面板(可选,阶段 9)

```bash
python -m streamlit run scripts/dashboard.py
```

- **只读**:展示最新周期与决策理由、风控检查明细、周期历史、
  风控审计、订单/意图状态、循环日志;30 秒自动刷新;
  **无任何下单、改配置入口**;
- **只监听本机**:`.streamlit/config.toml` 固定 `127.0.0.1:8501`,
  不对外网开放,遥测关闭;
- 数据源全部是 `var/` 下的审计/状态/日志文件;循环"活跃/已停止"
  由日志 mtime 给出迹象判断;
- 依赖 `streamlit==1.66.1`(已锁定在 requirements.txt)。

## 已知限制(阶段 0-7)

- 订单/成交/风控的**幂等主存储**仍是本地 JSON/JSONL 文件
  (var/,已 gitignore);数据库是审计/查询增强层,DB 故障时自动降级,
  不参与下单决策;Testnet 的 `daily_realized_pnl` 汇总仍需成交明细
  独立分析(当前入库为快照值);
- Paper Broker 为本地模拟:单进程假设、JSON 文件持久化、部分成交模型
  简化为"首成交 + 一次补足";账户快照由 Paper Broker 提供
  (equity 按注入的参考价标记,连续亏损数暂为 0);
- Testnet Broker 手续费仅汇总计价货币(USDT)部分,其他资产(如 BNB 抵扣)
  的费用暂不计入;账户快照的 `daily_realized_pnl` 需成交明细汇总(阶段 7);
- Testnet 查询本身失败(断线)时不动本地状态、下轮再查;
  危险不一致(本地与交易所矛盾)会自动触发 Kill Switch 停止新开仓,
  恢复必须人工确认;
- 行情支持 CCXT 公共接口与 Binance Vision 归档镜像(直连被阻断的网络
  用 `EXCHANGE_PROXY_URL` 走代理,或用 Vision 归档),无 WebSocket 实时推送;
- 代理只支持 `http://`/`https://`(CCXT 异步用 `https_proxy` 属性注入;
  系统代理不会被自动读取——CCXT `trust_env=False` 是有意设计);
- Live 模式代码完备且通过离线测试,但**尚未对真实账户做网络联调**;
  首次启用请先用最小资金、`--once` 单轮观察;
- LLM 调用为同步阻塞式单轮,无流式输出;
- `models.yaml` 中未填的环境变量会在启动时报错,这是有意行为(快速失败);
- 单笔风险按 `assumed_stop_loss_pct` 估算(固定 2%),不是真实止损距离;
- 指标为进程内计数 + 快照导出(无 Prometheus 导出端点);
- Telegram 通知失败只记日志,不重试、不影响交易(旁路设计)。
