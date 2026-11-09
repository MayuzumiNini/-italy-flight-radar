# 意大利机票雷达（Italy Flight Radar）

**已实现**：Amadeus 航班报价搜索（含开口程）、日期轮询、每方向最多一次转机、人民币 5000 元阈值、历史价格 JSON、响应式静态网页、GitHub Actions 每日两次运行。

**未实现**：从 GitHub Actions 直接向 ChatGPT 推送通知；ChatGPT 不能直接接收任意第三方 webhook。可以在 ChatGPT 另建定时检查任务，但它独立于本程序，不保证读取私有仓库数据。也没有自动购票功能。

## 1. 本地演示（不需要 API）

```bash
pip install -r requirements.txt
python radar.py --demo
python -m http.server 8000 --directory docs
```

浏览器打开 http://localhost:8000 。**演示价格为随机生成，不是实际报价。**

## 2. 接入真实报价

1. 注册 https://developers.amadeus.com/ ，创建 Self-Service App，获取 API Key 和 API Secret。
2. 先用测试环境验证程序；**测试环境数据有限且可能是缓存数据**。
3. 需要真实价格时，在 Amadeus 控制台申请 Production，并确认该 API 的可用性、免费额度、账单及条款。
4. 在终端设置环境变量（不要把密钥提交到 GitHub）：

```bash
export AMADEUS_CLIENT_ID='你的key'
export AMADEUS_CLIENT_SECRET='你的secret'
export AMADEUS_ENV='production'
python radar.py
```

Windows PowerShell 使用 `$env:AMADEUS_CLIENT_ID="..."` 等语法。

## 3. 自动运行 + 手机网页

1. 新建 GitHub 仓库，将本项目所有文件上传（包括 `.github/workflows/monitor.yml`）。**建议私人仓库**，若开启 GitHub Pages，公开页面会暴露你监测的航线和历史数据。
2. Settings → Secrets and variables → Actions → New repository secret，添加 `AMADEUS_CLIENT_ID`、`AMADEUS_CLIENT_SECRET`。
3. Actions → Variables，创建 `AMADEUS_ENV=production`（未配置则默认 test）。
4. Settings → Actions → General → Workflow permissions，允许 Read and write permissions。确认工作流可向仓库写入数据。
5. Actions → Italy flight radar → Run workflow 手动运行一次。
6. Settings → Pages → Build and deployment → Deploy from a branch，选择 main /docs，开启 GitHub Pages。
7. 用手机打开生成的 Pages URL，可添加到主屏幕。

默认每天 **北京时间 09:17 / 21:17** 运行（GitHub 定时任务可能延迟或被跳过）。每次查 8 组，逐步覆盖所有组合。每天约 16 个 API 查询，全部组合共 882 个；完整轮询约需 56 天。若需要更密集监控，可提高 `queries_per_run`，但要先核算配额。当前程序对失败批次会重试，API 失败不会产生虚假报价。

## 4. 重要限制

- Amadeus API 航空公司覆盖并不完整，尤其部分廉航/特定航司；不能承诺覆盖携程、飞猪、天巡全部报价。
- Amadeus 报价可能与实际出票价不同；需最终核价。若 `checked_bag_included=false`，仅表示未能证实全程包含托运行李，**不是一定不包含**。
- 当前提醒阈值写入 `docs/data.json` 的 `alerts`，网页展示低价标识，但**不会向 ChatGPT 账号推送**。可独立设置 ChatGPT 定时查询公开数据，或者后续集成邮件/Telegram 等通知渠道。
- 单独的机票报价不是最终购票链接，本软件不伪造订票链接。
- 页面仅显示每条路线最新记录和历史每日最低值，不是市场完整最低价。
- 轮询顺序是确定性的，日期组合会轮换；需要提高频率可修改配置。

## 5. 修改参数

编辑 `config.json`：`queries_per_run` 每次查询组数，`target_cny` 目标总价，`flex_days` 日期浮动天数，`require_checked_bag` 是否只显示确认含托运行李的报价。

## v2 高频监测与硬性预算保护（2026-11-09）

- GitHub Actions 每小时 UTC 第 17 分钟运行一次（GitHub 不保证准点）；每次最多 **4 个**日期/航线组合，优先靠近 2027-02-02 / 2027-02-10 的组合。
- 预算 **¥30 / UTC 自然月**；程序在每次请求前预留一次计费调用量（即使请求失败也计入），每月自动重置。达到预计预算后不再调用报价 API。运行并发限制为 1。
- **必须**在仓库 Settings → Secrets and variables → Actions → Variables 设置以下三个数值，否则生产/测试查询均会安全停止：
  - `AMADEUS_MONTHLY_FREE_CALLS`：你在 Amadeus 控制台看到的 **Flight Offers Search 对应环境**的每月免费请求次数（纯数字）。不知道时填 `0`。
  - `AMADEUS_PRICE_USD_PER_CALL`：该接口超过免费额度后**每次请求**的美元单价（如平台按千次计费，请先除以 1000）。不知道时不要启动付费查询。
  - `USD_CNY_BUDGET_RATE`：保守的美元兑人民币预算换算率，由你核对后设置（例如 8.0 只是示意，并非实时汇率）。
- `AMADEUS_ENV=production` 才能用生产环境；`test` 的价格不能当真实可售价格。
- 监测页面显示本月已预留请求次数、预计费用和 80% 警戒。价格低于 ¥5000 时产生事件；同一行程在报价进一步降低时才重复提示。事件只写入 JSON/网页，**不会直接推送 ChatGPT**。
- 费用计算是**保守的客户端估算**，不是供应商的账单或真实支付限制。Amadeus 可能按接口、账户、环境分别计费；其他 API、手工调用、汇率变化、税费或不同计费周期不在此计数器内。**请在供应商侧设置预算/额度警报，并定期核对账单。**
- GitHub Pages 公共页面展示价格历史；切勿将 API 密钥放在 `docs/` 或仓库文件里。GitHub Actions 的定时工作流可能延迟或跳过；如仓库持续不活动，定时任务可能被停用。
- 全部组合多于单次可查数量；每小时 4 组并不意味着所有日期组合都每小时刷新。程序使用排序后游标轮询，重点日期会更早被扫描，但完整轮询后才回到起点。
- 当前不支持自动核验票价、自动购票或 ChatGPT 私有推送。机票报价请到正规购票平台二次确认。

### 首次部署核对

1. 在 GitHub 创建仓库并上传项目文件，先保持 `AMADEUS_ENV=test`。
2. 设置密钥 `AMADEUS_CLIENT_ID`、`AMADEUS_CLIENT_SECRET`；再设置上述三个计费 Variables。
3. 在 Actions 页面手动执行一次；查看运行日志、`docs/data.json` 的 `billing` 字段和 `state.json` 的 `api_calls_reserved`。
4. 申请并核对 Amadeus 生产权限、接口定价及覆盖后再切换 `AMADEUS_ENV=production`。
5. GitHub Pages 选择 main 分支 `/docs` 目录。测试环境页面不是实时票价。
