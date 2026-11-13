import ReactECharts from 'echarts-for-react'

export type MetricArtifact = {
  kind: 'metric_analysis'
  title: string
  metric: string
  metric_label: string
  group_by: string
  unit: string
  period: { start_year: number; end_year: number }
  rows: Array<{
    label: string; series: string | null; value: number
    revenue: number; net_profit: number; months: number; company_count: number
  }>
  chart: { kind: 'bar' | 'line'; series: Array<{ name: string; points: Array<{ label: string; value: number }> }> }
  audit: { status: string; records_covered: number; max_identity_residual_10k_cny: number }
  method: string
  source: string
  query_sql: string
  query_parameters: number[]
}

export function metricToMarkdown(a: MetricArtifact): string {
  const heading = `### ${a.title}\n\n> ${a.source}。${a.method}\n\n`
  const rows = [
    '| 分组 | 年份 / 分类 | 指标值 | 覆盖月份记录 |',
    '|---|---|---:|---:|',
    ...a.rows.map(r => `| ${r.series ?? '全部'} | ${r.label} | ${r.value.toFixed(2)} ${a.unit} | ${r.months} |`),
  ].join('\n')
  return `${heading}${rows}\n\n核验状态：${a.audit.status}；财务勾稽最大残差 ${a.audit.max_identity_residual_10k_cny} 万元。\n\n查询参数：${a.query_parameters.join('–')}\n\n\`\`\`sql\n${a.query_sql}\n\`\`\`\n`
}

function makeOptions(a: MetricArtifact) {
  const colors = ['#51d9b1', '#88a6ea', '#efa980', '#b49dea']
  const labels = [...new Set(a.chart.series.flatMap(s => s.points.map(p => p.label)))]
  const horizontal = a.chart.kind === 'bar'
  const axis = { axisLabel: { color: '#91a8bc', fontSize: 12 }, axisLine: { lineStyle: { color: '#37516a' } } }
  const categoryAxis = { type: 'category', data: labels, ...axis }
  const numericAxis = {
    type: 'value',
    axisLabel: { color: '#91a8bc', formatter: (value: number) => `${value}${a.unit}` },
    splitLine: { lineStyle: { color: '#25394a', type: 'dashed' } },
  }
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', backgroundColor: '#112335', borderColor: '#365066', textStyle: { color: '#e1edf7' } },
    legend: { show: a.chart.series.length > 1, top: 0, textStyle: { color: '#a6bcd0' } },
    grid: { left: horizontal ? 105 : 63, right: 26, top: a.chart.series.length > 1 ? 45 : 24, bottom: 45, containLabel: false },
    xAxis: horizontal ? numericAxis : categoryAxis,
    yAxis: horizontal ? { ...categoryAxis, inverse: true } : numericAxis,
    series: a.chart.series.map((s, index) => {
      const data = labels.map(label => s.points.find(p => p.label === label)?.value ?? null)
      return {
        name: s.name, type: a.chart.kind, data, barMaxWidth: 23,
        smooth: a.chart.kind === 'line' ? 0.25 : undefined,
        connectNulls: false, symbolSize: 7,
        itemStyle: { color: colors[index % colors.length], borderRadius: horizontal ? 4 : 0 },
        lineStyle: { width: 3 },
      }
    }),
  }
}

export default function MetricAnalysisPanel({ items }: { items: MetricArtifact[] }) {
  return <section className="panel metric-panel" aria-label="Agent 任务专属图表">
    <div className="panel-heading">
      <div><span className="eyebrow">Agent 选择 · 程序核验</span><h3>任务专属指标与动态图表</h3></div>
      <span className="verified-pill">{items.length} 组真实工具结果</span>
    </div>
    <p className="verified-lead">Agent 选择指标、维度与时间，Python 从合成数据库汇总计算并校验后返回图表数据。这里不是模型自行编写的图表代码。</p>
    <div className="metric-results">
      {items.map((a, index) => <article key={`${a.metric}-${a.group_by}-${index}`} className="metric-result">
        <div className="metric-head"><div><strong>{a.title}</strong><small>分组：{a.group_by} · {a.audit.records_covered} 条月度记录 · {a.audit.status === 'passed' ? '数据校验通过' : '数据不完整'}</small></div><span>{a.unit}</span></div>
        <ReactECharts option={makeOptions(a)} style={{ height: Math.max(255, Math.min(410, 180 + a.rows.length * (a.chart.kind === 'bar' ? 22 : 8))) }} notMerge lazyUpdate />
        <div className="metric-table-wrap"><table className="metric-table"><thead><tr><th>分类</th><th>对象 / 年份</th><th>{a.metric_label}（{a.unit}）</th><th>数据月份数</th></tr></thead>
          <tbody>{a.rows.map((r, i) => <tr key={`${r.series ?? 'all'}-${r.label}-${i}`}><td>{r.series ?? '全部'}</td><td>{r.label}</td><td>{r.value.toFixed(2)}</td><td>{r.months}</td></tr>)}</tbody>
        </table></div>
        <details className="verified-sql"><summary>展开计算口径与只读 SQL</summary><p>{a.method}</p><p>查询参数：{a.query_parameters.join('、')}；来源：{a.source}</p><pre>{a.query_sql}</pre></details>
      </article>)}
    </div>
  </section>
}
