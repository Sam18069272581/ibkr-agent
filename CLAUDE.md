# ibkr-agent 项目说明（给 Claude Code）

请用中文和用户交流。

## 目标与约束
- 在 IBKR 上无情绪地交易：根据财报、基线、仓位和手续费估算，选最理性的交易。
- 只做美股，持有 1 年以上，本金约 2000 欧（账户基础货币 EUR）。
- 先在模拟盘闭环跑，稳定性优先。达到转实盘门槛、用户同意后才转实盘。
- 门槛（`ibkr-agent graduate` 检查）：
  - 连续 12 次周运行成功
  - 对账 0 次不一致
  - 实际成本不超过模型估计的 1.5 倍
  - 没触发过回撤熔断
  - 落后 SPY 不超过 5 个百分点
  - 胜率和超额收益只报告，不作为门槛。

## 设计要点
- 流程：`src/ibkr_agent/cli.py` 的 `_cycle`：取数 → 估值 → Claude 研究 → 决策 → 风控 → 下单并等成交 → 对账 → 快照。
- 财报来自 SEC EDGAR XBRL（需要 `SEC_USER_AGENT`）。
- 估值基线 = FCF 收益率 + 营收增长（限制在 −5% 到 8%）− 杠杆惩罚，见 `valuation.py`。
- Claude 只在基线上做 ±3pp 的调整并给出信心，不决定仓位、不下单。调用失败就不调整，见 `research.py`。
- 决策是确定性的（`decision.py`）：
  - 买入线：扣掉摊薄的来回成本后，超额收益 ≥ 2%
  - 卖出线：超额收益 < 1%
  - 等权，最多 6 只，单笔最少 $200
- 风控是纯函数（`risk.py`）：
  - kill switch 文件 `data/KILL`
  - 回撤熔断
  - 不用保证金
  - 单只和行业上限
  - 报价异常检查
  - 限价不穿价
- 交易时段为美东 09:45–15:45。挂单最多等 300 秒，没成交的撤掉。
- 所有记录写在 SQLite 日志 `data/journal.sqlite`（`journal.py`）。成绩和门槛只统计非 dry run 的运行。
- 规则参数只在 `config/settings.yaml` 里改，模板是 `settings.example.yaml`。

## 币圈部分（2026-11-05 起）
- 代码在 `src/ibkr_agent/crypto/`，命令 `ibkr-agent crypto run` / `crypto report`，配置在 `settings.yaml` 的 `crypto:` 段。
- 用户的决定：交易所 OKX（币安在欧盟没有 MiCA 牌照）；币圈本金 €300；回撤熔断 35%；核心仓 BTC/ETH 再平衡。
- 计划带边界（金额上限、±0.5% 价格带、15 分钟过期），下单前重新取价，跑出带外不追。
- 卖出遵守德国一年持有期（FIFO 批次，`crypto_lots` / `crypto_disposals` 表）。
- 实盘需要 `sandbox: false`、`dry_run: false` 和 `.env` 里的 `CRYPTO_ALLOW_LIVE=yes` 三者同时满足。不经用户明确同意不要改。
- 第二阶段（`crypto.ai`，默认关）：`crypto envelope` 让 Opus 每周定边界（立场、±10pp 倾斜、买入区间、两种可检查的失效条件）；快哨兵 Haiku 5.5 在每笔买入前答三道封闭题，没答就不买；卖出不经过模型；`crypto report` 有校准表和本月 API 花费；`monthly_budget_usd` 默认 $8。
- 2026-11-05 已在用户电脑上用 `paper_local` 跑通（项目是新 clone 的，`.venv` 新建；那台电脑上的 Claude Code 自动模式不允许 pip install，要用户自己装）。
- 下一步：可视化页读真实日志。

## 在用户的 Windows 电脑上运行
- 项目目录：`C:\Users\COLORFUL\ibkr-agent`，虚拟环境 `.venv`。
- 测试：`.venv\Scripts\python -m pytest -q`
- 网关：Docker Desktop 里的 IB Gateway（`docker-compose.yml`）。模拟盘端口是 4002。
- 凭据只放在 `.env`，由用户自己填写。不要读出、打印或提交。模拟盘有自己单独的账号。
- 运行前设置编码：`$env:PYTHONIOENCODING="utf-8"`
- 运行：
  - `.venv\Scripts\ibkr-agent run --skip-research`：不调用 Claude
  - `.venv\Scripts\ibkr-agent run`：完整运行
  - `.venv\Scripts\ibkr-agent report`
- 电脑每周二 16:30（CET）开机。每周任务用 `deploy\install_task_windows.ps1` 安装，**还没装**。

## 当前状态（2026-10-29）
- 网关已登录模拟账户，程序能连上、能读到账户。模拟盘余额已重置，净值约 $3,468。
- `dry_run: true`，还没下过单。
- 已修复回撤熔断误报：历史最高净值只统计非 dry run 的运行。旧日志备份在 `data\journal.before-balance-reset.sqlite`。
- **未解决：IBKR Error 10197，所有股票都拿不到行情。**
  - 原因：模拟盘共享真实账户的行情，而真实账户在别处有登录会话。
  - 已试过但没解决：
    - 行情类型改成 4（延迟冻结）
    - 真实账户全部退出登录
    - 重启网关
  - 已加后备：快照拿不到价格时，改用历史日线收盘价（`broker.py` 的 `_historical_close`）。
  - 推荐的根本解决办法：在 Client Portal 里关闭“与模拟账户共享实时行情”（设置 → 账户设置 → 模拟交易账户）。

## 下一步
1. 让 `run --skip-research` 拿到价格，并且净值和美元现金看起来正确。如果美元现金为 0，需要先在模拟盘把 EUR 换成 USD。
2. 跑一次完整的 `run`（带 Claude 研究），检查打分和计划订单是否合理。
3. 用户同意后：
   - 安装每周任务
   - 把 `dry_run` 改为 `false`
   - 开始 12 周的模拟盘考核
4. 不经用户明确同意，不下单，也不改 `mode: live`。
