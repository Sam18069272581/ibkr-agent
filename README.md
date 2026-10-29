# ibkr-agent

一个长线、小资金、无情绪的 IBKR 美股组合 Agent。Claude 负责读报表做研究，买不买、买多少由代码按固定规则决定，最后再过一道风控。

> 本项目不构成投资建议。任何策略都可能亏钱。先用 Paper 账户跑至少 2–3 个月。

## 它怎么做决定

每次运行（建议每周或每月一次）：

1. **取数**：从 IBKR 取账户、持仓和价格；从 SEC EDGAR 取年报数据（营收、经营现金流、资本开支、负债、股本）。
2. **报表基线**（`valuation.py`）：预期年化收益 ≈ 自由现金流收益率 + 营收增长（限制在 −5% 到 8%）− 高杠杆惩罚。
3. **Claude 研究**（`research.py`）：Claude 先写多方论点，再写空方论点，最后给一个 ±3 个百分点以内的调整和信心等级。低信心的调整不计入，中信心按一半计入。Claude 报错、拒答或输出格式不对时，一律当作没有提案。
4. **决策**（`decision.py`）：
   - 打分 = 基线 + Claude 调整 × 信心权重。
   - 新买入要求：打分 − 大盘基线（默认 7%）− 摊薄后的来回成本 ≥ 2%。
   - 已持有的只在超额收益跌破 1% 时卖出。买入线和卖出线之间留一段缓冲，避免来回折腾。
   - 选中的股票等权分配，最多 6 只。单笔订单不低于 $200，买不起一股的直接跳过。
5. **风控**（`risk.py`）：总开关、回撤熔断、不用融资、单只/行业上限、报价异常、订单数上限，任何一条不过就拒单。只下限价单，限价不会越过对手价。
6. **执行**（`broker.py`）：只在美东 09:45–15:45 下限价单，最多等 5 分钟成交，没成交的撤单。
7. **对账**（`reconcile.py`）：运行开始时，把 IBKR 持仓和上次记录比对，发现有人手动交易过会告警；下单后，按成交重新算持仓，再和 IBKR 比对。
8. **记录**（`journal.py`）：打分、Claude 观点、订单、拒单原因、成交、真实佣金、持仓快照全部写进 `data/journal.sqlite`。

默认结果是“不交易”。只有每一关都通过，才会真的下单。出错或对账不一致时，会通过 ntfy 推送到手机（可选）。

## 模拟盘闭环：从 Paper 到实盘

### 1. 准备 Paper 账户（一次性）

- 在 IBKR Client Portal 的 Paper 账户设置里，把模拟资金重置成和实盘接近的金额（例如 2200 美元）。默认的模拟资金很大，跑出来的成绩不能代表实盘。
- 在实盘账户设置里打开“与 Paper 账户共享行情”，否则模拟盘只能拿到延迟行情。
- 在模拟盘里先把欧元换成美元。本系统不用融资，没有美元现金就不会买入。

### 2. 安装并演练

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                   # 跑单元测试

cp .env.example .env                     # 填 ANTHROPIC_API_KEY、SEC_USER_AGENT、IB 账号（Paper）
cp config/settings.example.yaml config/settings.yaml
docker compose up -d ib-gateway          # 启动 Paper 模式的 IB Gateway

set -a; source .env; set +a
ibkr-agent run --skip-research           # 先不调 Claude，只看报表基线的结果
ibkr-agent run                           # 完整流程；dry_run: true 时只打印计划订单
```

### 3. 开始模拟盘交易并定时运行

确认演练结果合理后，把 `config/settings.yaml` 里的 `dry_run` 改成 `false`，然后按 `deploy/crontab.example` 设置每周运行一次。运行机器和 IB Gateway 必须在那个时间开着。

### 4. 看成绩，判断能否转实盘

```bash
ibkr-agent report      # 收益、基准、回撤、成本、胜率（仅参考）、门槛状态
ibkr-agent graduate    # 门槛全部通过时退出码为 0
```

转实盘门槛（在 `graduation` 里配置），只统计非演练的运行：

| 门槛 | 默认 |
| --- | --- |
| 连续成功运行（出错、对账不一致都会让计数清零） | 12 次（每周一次约 3 个月） |
| 对账一致 | 0 次不一致 |
| 实际手续费和滑点 / 模型估计 | ≤ 1.5 |
| 回撤熔断 | 没有触发过 |
| 收益相对 SPY | 落后不超过 5 个百分点 |

胜率和超额收益会写进报告，但不作为门槛：长线持有的策略在 3 个月里样本太少，结果主要是运气。

### 5. 转实盘

把 `mode` 改成 `live`、端口改成 `4001`，把 `.env` 里的 `TRADING_MODE` 改成 `live`，并换一个新的 `journal_path`，让实盘成绩单独统计。第一次先用 `dry_run: true` 演练一次。

紧急停止：`touch data/KILL`，之后每次运行都不会下任何单。

## 小资金需要注意的几件事

- **手续费**：IBKR Pro 固定费率每单最低约 $1。$300 的仓位来回一次约 0.9%，所以本系统只做长线、少调仓。费率是近似值，请以 IBKR 官网为准，并在 `costs` 里调整。
- **欧元账户买美股**：本系统不使用融资。要先在 IBKR 里把欧元换成美元，否则风控会以“美元现金不足”拒单。小额换汇也有手续费。
- **整股限制**：每只约 $350，股价超过这个数的股票一股都买不起，会被跳过。可以考虑 IBKR 的碎股交易，但需要先确认 API 是否支持你的账户。
- **欧盟散户买不了美国 ETF**：受 PRIIPs 规定限制，SPY 这类美国 ETF 通常无法买入。系统里的大盘基线只用来比较，不作为可买入的标的。
- **股息税**：记得在 IBKR 填 W-8BEN 表。

## 已知局限

- 报表基线模型刻意保持简单，没有经过回测验证。上线前应该先用历史数据回测，并和“直接买大盘”做对比。
- 多类股公司（如 GOOGL）用的是稀释后加权平均股数估算市值，结果是近似值。
- Claude 的调整没有经过校准。建议先在 Paper 账户积累记录，再根据实际效果调整各信心等级的权重。

## 代码结构

```
src/ibkr_agent/
  config.py        配置（只读）
  fundamentals.py  SEC EDGAR 年报数据
  valuation.py     报表基线预期收益
  research.py      Claude 研究员（结构化输出）
  costs.py         佣金、点差、滑点
  decision.py      打分、选股、目标仓位（纯函数）
  risk.py          风控与熔断（纯函数）
  broker.py        IBKR 连接、下单、等成交（ib_async）
  reconcile.py     对账
  journal.py       决策与成交日志
  report.py        成绩与转实盘门槛
  alerts.py        手机告警（ntfy）
  cli.py           run / report / graduate
deploy/            定时运行脚本与 crontab 示例
```
