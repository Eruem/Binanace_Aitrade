# 运维手册

## 启动(Paper 模式)

```bash
source .venv/Scripts/activate
python scripts/check_config.py   # 先确认配置
python main.py
```

## 停止与恢复

`python main.py` 打印横幅后退出(当前为配置校验入口,无常驻进程)。
启用 Docker 时:

```bash
docker compose stop       # 停止 app(保留容器与数据卷)
docker compose start      # 恢复
docker compose down       # 停止并移除容器(数据卷保留,数据库不丢)
docker compose down -v    # 连数据卷一起清理(会丢数据库!)
```

停止 → 恢复的原则(全阶段通用):
1. 任何状态不明(订单 UNKNOWN、本地与交易所不一致)先按对应章节
   对账,**绝不盲目重启后直接下单**;
2. Kill Switch 触发后恢复必须人工 `release(reason)`,不会自动恢复;
3. 数据库迁移(`scripts/migrate_db.py`)幂等,重启前后运行都安全。

## Docker(常驻部署)

服务:`loop`(交易循环,`--loop --interval 1800`,`restart: unless-stopped`)
+ `dashboard`(只读面板,发布到宿主机 `127.0.0.1:8501`);`postgres` 为
可选 profile(当前 `DATABASE_URL` 为空 = 仅 JSONL 审计,默认不启动)。

```bash
docker compose up -d                # 启动 loop + dashboard
docker compose logs -f loop         # 跟踪循环日志(JSON 行,同 stderr)
docker compose logs -f dashboard    # 面板日志
docker compose restart loop         # 重启循环
docker compose stop / down          # 停止(down 不删宿主机 var/ 数据)
docker compose ps                   # 状态与健康检查
docker compose --profile db up -d   # 需要数据库时连 postgres 一起启动
```

- **密钥**:镜像不含 `.env`(`.dockerignore` 排除,已验证);
  运行时经 compose `env_file: .env` 注入,横幅/日志依旧全量脱敏;
- **代理**:容器内 `127.0.0.1` 是容器自己,compose 把
  `EXCHANGE_PROXY_URL` 覆盖为 `http://host.docker.internal:10808`
  (前置:代理客户端允许来自 Docker 网络的连接);
- **数据**:`./var` 读写挂载(审计/订单状态/循环日志)、`./config`
  只读挂载(交易对与风控配置改完 `restart loop` 生效);
- **时区**:容器 `TZ=Asia/Shanghai`,日志时间戳与日亏损跨日边界
  与宿主机一致;
- **面板**:容器内监听 `0.0.0.0`(端口映射需要),宿主机只发布
  `127.0.0.1:8501`,外部机器不可达(与 config.toml 约束等价);
- **升级**:改代码后 `docker compose up -d --build`;
- **基础镜像拉取**:Docker Hub 被阻断时可经国内镜像拉取后本地打标签:
  `docker pull docker.m.daocloud.io/library/python:3.14-slim &&`
  `docker tag docker.m.daocloud.io/library/python:3.14-slim python:3.14-slim`。

## 排错

| 现象 | 处理 |
|---|---|
| `缺少必需配置: 环境变量 XXX 未设置` | 在本地 `.env` 设置该变量(参考 `.env.example`);错误信息只含变量名 |
| `trading.mode 只允许 paper/testnet` | 检查 `TRADING_MODE`;live 被禁止 |
| `ENABLE_LIVE_TRADING=true 被拒绝` | 本版本不做真实交易;把该开关改回 false |
| `TRADING_MODE=testnet 要求同时设置 ENABLE_BINANCE_TESTNET=true` | 两个开关成对设置 |
| YAML 解析失败 | 检查对应 config/*.yaml 的缩进与冒号 |
| Pydantic 校验失败 | 错误信息会指出字段路径;extra 字段不被接受 |

## Kill Switch(阶段 4 已实现)

- 状态持久化在本地 JSON(路径见 `risk/kill_switch.py`);
- 触发方式:`engage`(人工紧急停止)/ `auto_pause`(系统异常自动暂停);
- 触发后只禁止新开仓,SELL 平仓不受限,不自动清仓;
- 恢复:只能人工调用 `release(reason)`;`auto_pause` 不会自行恢复;
- 状态文件损坏时按“已触发”处理(宁可停),需人工检查后 release;
- 风控自审流水与 cycle 审计分别落盘(`risk/*.jsonl` 与 `audit/*.jsonl`)。

## Paper Broker(阶段 5)

- 状态文件:`var/paper_broker.json`(订单/成交/余额)与
  `var/order_intents.json`(订单意图);原子写入,tmp + 替换;
- **文件损坏 = 拒绝交易**:构造时立即抛错,不会半初始化启动;
  排查后只能人工决定:修复 JSON 或删除文件重置模拟账户(会丢失模拟持仓!);
- 重启后订单/持仓/成交仍在;同一 Client Order ID 重复提交依旧幂等;
- 部分成交订单(PARTIALLY_FILLED)会锁定可用余额;
  `complete_fill` 补足或 `cancel_order` 撤销后释放;
- 订单状态 UNKNOWN(提交超时/断线)处置顺序:
  1. 调用 `OrderManager.recover(client_order_id)` 查询执行层;
  2. 查到原单 → 意图同步为交易所状态;
  3. 确认无此单 → 意图回退到 RISK_APPROVED,可用**同一** ID 重试;
  4. 本地状态与执行层不一致 → 停止,人工对账,绝不盲目重新下单。

## Binance Spot Testnet(阶段 6,可选启用)

启用(密钥只放本地 `.env`,不要粘贴到聊天/代码/提交):

```dotenv
TRADING_MODE=testnet
ENABLE_BINANCE_TESTNET=true
BINANCE_TESTNET_API_KEY=<testnet.binance.vision 申请的独立密钥>
BINANCE_TESTNET_API_SECRET=<对应 Secret>
```

- 密钥在 [testnet.binance.vision](https://testnet.binance.vision/) 申请,
  与正式账户密钥完全分开;
- 启动前 `python scripts/check_config.py` 应显示
  `execution.mode=testnet` 且密钥已脱敏;
- 首个请求前强制 sandbox 模式 + URL 校验(`testnet.binance.vision`),
  不满足即拒绝连接;
- 下单精度:BUY 按 quoteOrderQty(精确花费 N USDT,8 位取整);
  SELL 按 ticker 估算数量、LOT_SIZE 步长向下取整;
  低于交易所最小名义 → 本地 REJECTED,不发单(日志含原因)。

### 订单对账(`exchange/order_reconciler.py`)

- 定期运行 `OrderReconciler.reconcile()`(调度器在阶段 7 接入;
  当前可手动/脚本调用);输出 `ReconcileReport`(checked/synced/
  released/divergences/errors)可审计;
- SUBMITTING/UNKNOWN:查到 → 同步;确认无单 → 释放,允许同 ID 重试;
- 在途/终态与交易所矛盾(divergence)→ Kill Switch 自动
  `auto_pause`(**停止新开仓**);恢复流程:
  1. 人工核对交易所该订单的真实状态;
  2. 必要时用 `OrderManager.sync_from_exchange` / `confirm_absent`
     修正本地意图;
  3. `KillSwitch.release("人工确认...")` 恢复;
- 查询失败(断线)只记入 errors,状态一律不动,下轮再查。

### 常见错误

| 现象 | 处理 |
|---|---|
| `testnet 模式缺少 Testnet API Key/Secret` | 在 `.env` 设置两个 `BINANCE_TESTNET_*` 变量 |
| `未能确认连接的是 Testnet` | 检查 CCXT 版本/代理是否改写了 URL;系统已拒绝请求(安全默认) |
| `testnet=False 需要显式 allow_live=True` | 代码层默认拒绝真实资金连接;Live 只经工厂(四重校验)构建 |
| `EXCHANGE_PROXY_URL 只允许 http:// 或 https://` | 代理只支持 http/https;socks 需自备转换(不在本版本范围) |
| 订单对账 divergences 非空、新开仓全被拒 | 按上节人工对账后 release Kill Switch |

## 运行交易循环(阶段 8)

`scripts/run_agent.py` 是唯一受支持的运行入口(配置 → 行情 → Agent →
风控 → 执行 → 审计全链路):

```bash
python scripts/run_agent.py                 # 单轮(每个配置的交易对各一轮)
python scripts/run_agent.py --loop --interval 300   # 常驻循环
python scripts/run_agent.py --symbol BTC/USDT      # 覆盖交易对
```

- 配置错误立即退出(fail-safe,绝不降级为其他模式);
- 风控引擎与执行层强制挂载(图装配保证:无风控绝不执行);
- 行情/交易流量走 `EXCHANGE_PROXY_URL`(若配置);
  CCXT 不读系统代理(`trust_env=False`),直连被阻断的网络必须显式配置;
- 数据库/Telegram 按阶段 7 配置可选启用,失败不影响交易主流程;
- 停止:Ctrl+C(先等当前轮结束;清理行情与交易所客户端后退出)。

## Binance Live 真实资金(阶段 8,可选启用,高风险)

> ⚠️ 真实资金,损失由账户所有者承担。首次启用建议:
> 最小资金 + `--once` 单轮观察 + 交易所侧确认每笔订单。

前置(缺一不可,任一不满足启动拒绝):

```dotenv
TRADING_MODE=live
ENABLE_LIVE_TRADING=true
BINANCE_LIVE_API_KEY=<正式账户 API Key(只勾现货交易权限)>
BINANCE_LIVE_API_SECRET=<对应 Secret>
EXCHANGE_PROXY_URL=http://127.0.0.1:10808   # 本网络直连被阻断,按需配置
```

- `risk.enabled=true` 强制(配置文件 risk.yaml;live 模式下设 false 启动拒绝);
- 启动横幅显示 `LIVE TRADING: *** ENABLED` 警告;
- Live 客户端绝不启用 sandbox,URL 必须指向 `api.binance.com`;
- 账户快照自动跳过**无现货市场**的资产(理财收据如 LDBTTC、
  已下架币,标记行情返回 Invalid symbol);其余行情失败(断线/
  权限)照常让风控 fail-safe 否决;
- 其余(下单精度/对账/UNKNOWN 恢复)与 Testnet 同一套代码与流程。

| 现象 | 处理 |
|---|---|
| `ENABLE_LIVE_TRADING=true 仅允许配合 TRADING_MODE=live` | paper/testnet 模式必须把它设为 false |
| `TRADING_MODE=live 要求同时设置 ENABLE_LIVE_TRADING=true` | 双开关必须同时显式开启 |
| `live 模式缺少 Live API Key/Secret` | 设置两个 `BINANCE_LIVE_*` 变量(与 Testnet 分开) |
| `live(真实资金)模式强制要求 risk.enabled=true` | risk.yaml 中 `enabled: true`(不允许关闭) |
| `Live 连接的 URL 指向 Testnet` | 客户端拒绝请求(安全默认);检查注入的 exchange 对象 |
| `-1021 InvalidNonce: Timestamp ... ahead of the server's time` | 本机时钟与币安偏差超过 ±1s(Windows 未做 NTP 同步时会漂移,源=主板 CMOS 时钟)。客户端已在每个签名请求前对拍服务器时间(每 300 秒,ccxt `load_time_difference`),通常自愈;若仍持续,用**管理员**执行 `w32tm /resync /force`,或 Windows 设置 → 日期和时间 → 立即同步 |

## 数据库(阶段 7,可选启用)

启用:`.env` 设 `DATABASE_URL`(参考 `.env.example`),然后:

```bash
python scripts/migrate_db.py           # 应用迁移(幂等)
python scripts/migrate_db.py --status  # 查看已应用/待应用版本
```

- DATABASE_URL 为空 = 数据库禁用,系统仅用本地 JSONL 审计,正常运行;
- 本地无 PostgreSQL 时可用 `DATABASE_URL=sqlite:///var/agent.db`;
- 迁移入口在 `scripts/migrate_db.py`;schema 定义在 `database/models.py`,
  迁移列表在 `database/migrations/__init__.py`(新增迁移追加元组即可);
- 决策全链路查询(决策→Agent 输出→风控→订单→成交):

```bash
python - <<'PY'
from config import load_config
from database.repository import build_recorder
cfg = load_config()
rec = build_recorder(cfg.settings.database.url)
trace = rec.trace_decision("cycle-<你的cycle_id>")
print({k: len(v) if isinstance(v, list) else type(v).__name__
       for k, v in trace.items()})
PY
```

- 数据库故障时交易主流程不受影响:record_node 捕获异常、写
  db_write_failed 系统事件(若 DB 部分可用)并降级为仅 JSONL;
- 常见错误:

| 现象 | 处理 |
|---|---|
| `DATABASE_URL 未设置,数据库未启用` | 在 .env 设置后重试;或不启用(仅 JSONL) |
| `database.url 只允许 postgresql/SQLite` | scheme 拼写错误;报错只含 scheme 名 |
| 连接失败/认证失败 | 检查 postgres 服务(`docker compose up -d postgres`)与 URL 口令 |

## 监控与通知(阶段 7)

- 日志:JSON 行(stderr),含 cycle_id/decision_id/agent_name/latency_ms
  等结构化字段;API Key/Secret/Authorization/Telegram Token/数据库口令
  一律脱敏(测试断言);
- 指标:`monitoring.metrics.collector` 进程内计数(模型延迟/失败、
  行情延迟、风控否决、订单状态、Kill Switch 状态等),
  `collector.snapshot()` 可编程导出;
- Telegram(可选):`.env` 同时设置 `TELEGRAM_BOT_TOKEN` 与
  `TELEGRAM_CHAT_ID` 即启用;**只发通知**(executed / exec_rejected /
  exec_unknown / risk_rejected 四种终态发摘要,纯 HOLD 静默);
  发送失败只记日志,不影响交易;本系统**绝不执行**聊天文本中的任何指令;
- Telegram 网络:`api.telegram.org` 在被阻断网络同样走
  `EXCHANGE_PROXY_URL` 代理(装配时显式注入通知器,
  不依赖本机系统代理状态);
- 隐私:Telegram 通知只含 cycle_id/symbol/状态/金额,不含密钥;
  chat_id 建议用私聊,避免把交易信息暴露到群聊。

## 只读监控面板(阶段 9)

```bash
# 项目根目录执行;浏览器自动打开 http://localhost:8501
python -m streamlit run scripts/dashboard.py
```

- 展示:最新周期(决策/理由/风控检查)、周期历史表、风控审计表、
  订单与意图状态、循环日志尾部;30 秒自动刷新(fragment);
- 循环状态:按 `var/agent_loop.log` mtime 判断"🟢活跃/🔴已停止"
  (窗口 = 间隔 + 单轮耗时余量;只是迹象,不是强一致判定);
- 安全边界:**只读**——无下单/改配置入口,不加载任何密钥;
  `monitoring/dashboard_data.py` 纯只读函数,离线可单测;
  `.streamlit/config.toml` 固定仅监听 `127.0.0.1`、遥测关闭;
- 无数据时页面显示占位提示,不报错;文件损坏行自动跳过;
- 停止:Ctrl+C(前台)或结束对应进程;与交易循环互不影响,
  面板只是读文件,循环未运行也能看历史。

## 恢复原则

- 任何配置错误都应在启动时快速失败,不留半初始化状态;
- 订单状态 UNKNOWN 时先对账(recover),绝不盲目重新下单,
  绝不生成新 Client Order ID 重发;
- Kill Switch 只禁止新开仓,不自动清仓;恢复必须人工确认;
- 日亏损/风控/Paper 状态文件损坏时不放宽限制,先排查再恢复。
