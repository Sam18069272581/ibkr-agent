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
6. **记录**（`journal.py`）：打分、Claude 观点、订单、拒单原因全部写进 `data/journal.sqlite`，方便复盘。

默认结果是“不交易”。只有每一关都通过，才会真的下单。

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                   # 跑单元测试

cp .env.example .env                     # 填 ANTHROPIC_API_KEY、SEC_USER_AGENT、IB 账号
cp config/settings.example.yaml config/settings.yaml
docker compose up -d ib-gateway          # 启动 Paper 模式的 IB Gateway

set -a; source .env; set +a
ibkr-agent --skip-research               # 先不调 Claude，只看报表基线的结果
ibkr-agent                               # 完整流程；dry_run: true 时只打印计划订单
```

确认演练结果合理后，再把 `config/settings.yaml` 里的 `dry_run` 改成 `false`，让它在 Paper 账户真实下单。上实盘时把 `mode` 改成 `live`、端口改成 `4001`，并把 `.env` 里的 `TRADING_MODE` 改成 `live`。

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
  broker.py        IBKR 连接（ib_async）
  journal.py       决策日志
  cli.py           一次完整运行
```
