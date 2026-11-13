# V0.4 Agent 评测集（独立后端工具）

本评测不修改已经验收的 Agent 主循环、SQL 校验、合成数据库或 React 页面。默认**绝不调用模型**。

## 1. 无费用离线基线

```bash
cd ~/projects/enterprise-insight-agent/backend
uv run pytest -q
uv run python -m evals.run offline
```

- 用固定种子在临时目录重新生成数据库，不修改现有 `backend/data/demo.sqlite`。
- `cases.jsonl` 包含 32 例：15 个通用指标、2 个利润率下降核验、4 个合法 SQL、6 个危险 SQL、5 个非法参数。
- 参考答案由原始 SQLite 记录重新聚合（独立 Python 计算），并比较排名、数值、覆盖月数、图表和证据表的一致性。
- `offline 32/32 PASS` 仅说明**确定性后端能力**通过对应测试，不是 LLM 任务成功率，更不是生产部署验收。
- 结果保存在 `backend/evals/results/offline-*.json`，此目录被 Git 忽略。

## 2. 真实模型抽样（可能计费）

确保 `.env` 中已配置百炼/其他兼容服务的 `OPENAI_API_KEY`、`OPENAI_MODEL`、`OPENAI_BASE_URL`，在 `backend` 目录执行：

```bash
uv run python -m evals.run list
uv run python -m evals.run live --limit 1 --confirm-billable
```

第一例顺利完成后，可扩大到至多 3 例：

```bash
uv run python -m evals.run live --limit 3 --confirm-billable
```

重要：

- 每例真实调用现有 `run_agent_stream` **一次**；不自动重试。`--limit` 最大为 5，防止意外产生大量费用。
- 如果没有 `--confirm-billable`，工具拒绝运行真实模型；无法连接模型会报告失败，不会伪造成功记录。
- 根据工具返回的**完整结构化指标成果**验证分组、年份、排名、数值和图表一致性；完成状态与执行工具数来自真实事件。
- 一条通过的 live 用例表示「完成 + 工具调用 + 结构化证据校验通过」，**不代表** AI 自然语言中的所有推论、句子都正确。`prose_factual_accuracy` 明确标为 `not_automatically_verified`。
- 由于现有 SSE 事件尚未暴露准确的每次模型 Token/费用，报告中 `model_calls`、`tokens`、`cost` 都为 `null`，不是 0。模型轮次数与 token 成本将在后续独立的 Trace 增强中采集。
- `live` 只选 `cases.jsonl` 中带 `"live":true` 的案例。它不会将原始密钥、完整模型回答或模型提示词写入报告。
- 模型可能选择 `query_database` 而非 `analyze_metrics`；在严格的结构化证据基准中，这类任务会提示「未产生程序核验成果」，**不应误解成数据工具必然故障**。利用执行链路决定是否优化工具路由。

## 3. 解释分数

| 指标 | 含义 | 局限 |
|---|---|---|
| `pass_rate` | 测试范围内结构化验证的通过比例 | 离线与真实模型分母不同，不能混用 |
| `latency_ms_p50/p95` | 单案例墙钟耗时；P95 为最近秩样本分位数 | 1–5 个 live 样本不足以代表真实延迟分布 |
| `mean_tool_calls` | SSE 中实际观测到的工具调用平均数 | 没有工具调用之外的模型轮次 |
| `tool_errors` | 工具结果中明确返回错误的次数 | 部分截断结果可能无法完全判定错误 |
| `prose_factual_accuracy` | 文字分析事实核验状态 | 当前不自动覆盖全部叙述 |

建议第一轮人工核查 3 份 `live` 输出：具体企业/行业是否匹配、百分点是否混淆、相关性是否被写成因果、SQL 错误后是否有限重试。

如发生任何失败，请保留 `evals/results/live-*.json` 和后端日志中的**非敏感部分**，不要把 `.env` 或 API Key 上传到公开仓库。
