import ReactECharts from 'echarts-for-react'

export type VerifiedCompany = {
  company: string
  margin_2024: number
  margin_2025: number
  delta_pp: number
  cost_rate_change_pp: number
  expense_rate_change_pp: number
  revenue_growth_pct: number
  cost_growth_pct: number | null
  expense_growth_pct: number | null
  months_2024: number
  months_2025: number
}

export type VerifiedAnalysis = {
  kind: 'margin_decline_2024_2025'
  title: string
  source: string
  unit: string
  method: string
  interpretation: string
  query_sql: string
  compared_companies: number
  companies: VerifiedCompany[]
  audit: { status: string; max_identity_residual_pp: number; fully_compared: number }
}

function signed(value: number, unit = ''): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}${unit}`
}

export function toVerifiedMarkdown(result: VerifiedAnalysis): string {
  const heading = [
    `## ${result.title}`,
    '',
    `数据来源：${result.source}`,
    `口径：${result.method}`,
    `说明：${result.interpretation}`,
    '',
    '| 企业 | 2024 净利润率 | 2025 净利润率 | 变化（百分点） | 成本率变化（百分点） | 费用率变化（百分点） |',
    '| --- | ---: | ---: | ---: | ---: | ---: |',
  ]
  const rows = result.companies.map(c => `| ${c.company} | ${signed(c.margin_2024, '%')} | ${signed(c.margin_2025, '%')} | ${signed(c.delta_pp)} | ${signed(c.cost_rate_change_pp)} | ${signed(c.expense_rate_change_pp)} |`)
  const footer = [
    '',
    `核验：${result.audit.status}，完整比较企业数 ${result.compared_companies}，分解残差最大值 ${result.audit.max_identity_residual_pp.toExponential(3)} 个百分点。`,
    '', '### 可复核 SQL', '```sql', result.query_sql, '```',
    '', '> 所有数据均为合成数据。数据分解是会计恒等式，不能当作经营原因的因果证明。'
  ]
  return [...heading, ...rows, ...footer].join('\n')
}

export default function VerifiedAnalysisPanel({ result }: { result: VerifiedAnalysis }) {
  const names = result.companies.map(c => c.company)
  const margins = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', backgroundColor: '#122538', borderColor: '#365066', textStyle: { color: '#dbeaf4' } },
    legend: { top: 0, right: 0, textStyle: { color: '#a9bed0' } },
    grid: { left: 83, right: 22, top: 48, bottom: 20 },
    xAxis: { type: 'value', axisLabel: { color: '#9aadc1', formatter: (v: number) => `${v}%` }, splitLine: { lineStyle: { color: '#2b3d4f' } } },
    yAxis: { type: 'category', inverse: true, data: names, axisLabel: { color: '#d3e1ef', fontSize: 12 }, axisLine: { show: false }, axisTick: { show: false } },
    series: [
      { name: '2024 利润率', type: 'bar', barMaxWidth: 18, data: result.companies.map(c => c.margin_2024), itemStyle: { color: '#7189b3', borderRadius: 3 } },
      { name: '2025 利润率', type: 'bar', barMaxWidth: 18, data: result.companies.map(c => c.margin_2025), itemStyle: { color: '#45d2af', borderRadius: 3 } },
    ],
  }
  const attribution = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => `${value} pp`, backgroundColor: '#122538', borderColor: '#365066', textStyle: { color: '#dbeaf4' } },
    legend: { top: 0, right: 0, textStyle: { color: '#a9bed0' } },
    grid: { left: 83, right: 20, top: 46, bottom: 25 },
    xAxis: { type: 'value', axisLabel: { color: '#9aadc1', formatter: (v: number) => `${v}pp` }, splitLine: { lineStyle: { color: '#2b3d4f' } } },
    yAxis: { type: 'category', inverse: true, data: names, axisLabel: { color: '#d3e1ef', fontSize: 12 }, axisLine: { show: false }, axisTick: { show: false } },
    series: [
      { name: '成本率变化', type: 'bar', barMaxWidth: 18, data: result.companies.map(c => c.cost_rate_change_pp), itemStyle: { color: '#eda781', borderRadius: 3 } },
      { name: '费用率变化', type: 'bar', barMaxWidth: 18, data: result.companies.map(c => c.expense_rate_change_pp), itemStyle: { color: '#9e8bdf', borderRadius: 3 } },
    ],
  }
  return <section className="panel verified-panel" aria-label="独立核验分析">
    <div className="panel-heading">
      <div><span className="eyebrow">独立计算 · 可核验证据</span><h3>独立计算与证据核验</h3></div>
      <span className="verified-pill">{result.audit.status === 'passed' ? '计算核验通过' : '部分数据'}</span>
    </div>
    <p className="verified-lead">这是后端根据本次任务直接读取合成数据库得到的<strong>程序计算结果</strong>，不属于大模型生成的数据。下方 AI 文字解释需要与这里的数值相互核对。</p>
    {result.companies.length ? <>
      <div className="verified-kpi-grid">
        {result.companies.map(c => <div className="verified-kpi" key={c.company}>
          <span>{c.company}</span><strong>{signed(c.delta_pp)} <small>pp</small></strong><span>2025 年相较 2024 年</span>
        </div>)}
      </div>
      <div className="verified-chart-grid">
        <div className="verified-chart"><h4>净利润率对比</h4><ReactECharts option={margins} style={{ height: 240 }} notMerge lazyUpdate /></div>
        <div className="verified-chart"><h4>成本率 / 费用率变化</h4><ReactECharts option={attribution} style={{ height: 240 }} notMerge lazyUpdate /></div>
      </div>
      <div className="verified-table-wrap"><table className="verified-table">
        <thead><tr><th>企业</th><th>2024 利润率</th><th>2025 利润率</th><th>变化 pp</th><th>成本率 Δpp</th><th>费用率 Δpp</th></tr></thead>
        <tbody>{result.companies.map(c => <tr key={c.company}><td>{c.company}</td><td>{c.margin_2024.toFixed(2)}%</td><td>{c.margin_2025.toFixed(2)}%</td><td className="verified-negative">{signed(c.delta_pp)}</td><td>{signed(c.cost_rate_change_pp)}</td><td>{signed(c.expense_rate_change_pp)}</td></tr>)}</tbody>
      </table></div>
    </> : <p className="verified-lead">本次数据中未发现净利润率下降企业。</p>}
    <div className="verified-method">
      <strong>指标口径</strong><p>{result.method}</p>
      <strong>解释边界</strong><p>{result.interpretation}</p>
      <span>数据来源：{result.source}；完整比较企业数：{result.compared_companies}</span>
    </div>
    <details className="verified-sql"><summary>查看独立核验使用的 SQL</summary><pre>{result.query_sql}</pre></details>
  </section>
}
