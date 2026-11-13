import type { RunSnapshot, EventRecord } from './history'
import type { MetricArtifact } from './MetricAnalysis'
import type { VerifiedAnalysis } from './VerifiedAnalysis'

function safeCell(s: string): string { return s.replaceAll('|', '\\|').replace(/[\r\n]+/g, ' ') }
const decimal = (n: number) => Number.isFinite(n) ? n.toFixed(2) : '—'

function verifiedMarkdown(v: VerifiedAnalysis): string {
  return [
    `### ${v.title}`,
    '', `来源：${v.source}`, `计算口径：${v.method}`, `解释限制：${v.interpretation}`, '',
    '| 企业 | 2024 净利润率 | 2025 净利润率 | 变化(pp) | 成本率变化(pp) | 费用率变化(pp) |',
    '|---|---:|---:|---:|---:|---:|',
    ...v.companies.map(c => `| ${safeCell(c.company)} | ${decimal(c.margin_2024)}% | ${decimal(c.margin_2025)}% | ${decimal(c.delta_pp)} | ${decimal(c.cost_rate_change_pp)} | ${decimal(c.expense_rate_change_pp)} |`),
    '', `程序核验：${v.audit.status}；最大勾稽残差：${v.audit.max_identity_residual_pp} pp。`,
    '', '#### 可复核 SQL（仅供查看）', '', '```sql', v.query_sql, '```',
  ].join('\n')
}

function metricMarkdown(a: MetricArtifact): string {
  return [
    `### ${a.title}`, '', `来源：${a.source}`, `口径：${a.method}`, '',
    '| 系列 | 对象 / 年份 | 指标值 | 月度记录 |', '|---|---|---:|---:|',
    ...a.rows.map(r => `| ${safeCell(r.series ?? '全部')} | ${safeCell(r.label)} | ${decimal(r.value)} ${a.unit} | ${r.months} |`),
    '', `程序核验：${a.audit.status}；覆盖月度记录：${a.audit.records_covered}。`,
    '', '```sql', a.query_sql, '```',
  ].join('\n')
}

function traceSummary(events: EventRecord[]): string {
  const calls = events.filter(e => e.event === 'tool_call')
  const lines = calls.map((e, i) => `- ${i + 1}. ${String(e.data.name ?? 'tool')}（本次实际调用）`)
  return lines.length ? lines.join('\n') : '未检测到工具调用。'
}

export function buildMarkdownReport(run: Pick<RunSnapshot, 'question' | 'answer' | 'verified' | 'metricArtifacts' | 'events'>,
  generatedAt: string = new Date().toISOString()): string {
  return [
    '# 经营洞察 Agent · 分析报告', '',
    `生成时间：${generatedAt}`, '',
    '> 数据来源：固定种子的虚构企业合成数据库；并非真实公司数据，不构成投资建议。',
    '> 本报告中的“已核验”仅指对应工具返回的计算字段及勾稽规则，不能代表模型的全部自然语言推断已获验证。',
    '', '## 分析任务', '', run.question, '',
    '## Agent 分析结论（需与下方证据交叉检查）', '', run.answer, '',
    '## 程序计算与可追溯证据', '',
    ...(run.verified ? [verifiedMarkdown(run.verified), ''] : []),
    ...(run.metricArtifacts.length ? run.metricArtifacts.map(metricMarkdown) : []),
    ...(!run.verified && !run.metricArtifacts.length ? ['本次没有程序化核验产物，模型生成的数字仍需人工检查。'] : []),
    '', '## 执行摘要', '', traceSummary(run.events), '',
    '## 数据与方法局限', '',
    '- 所有数据都是合成演示数据，不能用于真实企业经营决策。',
    '- 成本率和费用率变化是会计分解，不等同于因果归因。',
    '- 图表由页面依据上述数据渲染；本 Markdown 报告保留其数值、表格和 SQL，不嵌入图片。',
    '- 记录中的模型解释、单位与证据适用范围仍应由使用者复核。', '',
  ].join('\n')
}

export function downloadReport(text: string): void {
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const element = document.createElement('a')
  element.href = url
  element.download = `enterprise-insight-report-${new Date().toISOString().slice(0, 10)}.md`
  element.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
