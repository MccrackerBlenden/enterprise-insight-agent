import { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'

export type Year = { year: number; revenue: number; profit: number; margin: number }
export type Ranked = { company: string; previous: number; current: number; delta: number }
export type Overview = { companies: number; records: number; years: Year[]; ranking: Ranked[]; synthetic: boolean }

export default function OverviewCharts({ data, mode }: { data: Overview | null; mode: 'dashboard' | 'reference' }) {
  const trend = useMemo(() => ({
    tooltip: { trigger: 'axis', backgroundColor: '#152234', borderColor: '#33435b', textStyle: { color: '#e6eef9' } },
    grid: { left: 44, right: 18, top: 22, bottom: 32 },
    xAxis: { type: 'category', data: data?.years.map(y => y.year.toString()) ?? [],
      axisLabel: { color: '#8191a9' }, axisLine: { lineStyle: { color: '#273549' } } },
    yAxis: { type: 'value', axisLabel: { color: '#8191a9', formatter: (value: number) => `${(value / 10000).toFixed(0)}亿` },
      splitLine: { lineStyle: { color: '#243247', type: 'dashed' } } },
    series: [{ type: 'line', smooth: .35, data: data?.years.map(y => y.revenue) ?? [],
      symbolSize: 10, lineStyle: { color: '#47d9b4', width: 3 }, itemStyle: { color: '#5fe6be' },
      areaStyle: { color: 'rgba(71,217,180,.09)' } }]
  }), [data])
  const rank = useMemo(() => ({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#152234',
      borderColor: '#33435b', textStyle: { color: '#e6eef9' }, valueFormatter: (v: number) => `${v} pp` },
    grid: { left: 88, right: 22, top: 14, bottom: 23 },
    xAxis: { type: 'value', axisLabel: { color: '#8191a9' }, splitLine: { lineStyle: { color: '#243247' } } },
    yAxis: { type: 'category', inverse: true, data: data?.ranking.slice(0, 6).map(r => r.company) ?? [],
      axisLabel: { color: '#b9c8db', fontSize: 12 }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{ type: 'bar', barMaxWidth: 18, data: data?.ranking.slice(0, 6).map(r => ({ value: r.delta,
      itemStyle: { color: r.delta < 0 ? '#e6a67d' : '#47d9b4', borderRadius: [3, 3, 3, 3] } })) ?? [] }]
  }), [data])

  if (mode === 'reference') return <section className="panel"><div className="panel-heading"><div><span className="eyebrow">全局经营参考</span><h3>全局参考：利润率变化</h3></div><span className="figure-tag">确定性计算</span></div>
    <ReactECharts option={rank} style={{ height: 270 }} notMerge lazyUpdate/><small className="chart-note">这张图直接来自合成数据库概览，不代表 Agent 针对当前任务生成的图表。</small></section>

  return <div className="chart-grid">
    <article className="panel"><div className="panel-heading"><div><span className="eyebrow">营收趋势</span><h3>企业整体营收趋势</h3></div><div className="figure-tag">2023–2025</div></div>
      <ReactECharts option={trend} style={{height:250}} notMerge lazyUpdate/><small className="chart-note">统计由后端直接计算，非 AI 生成或预测</small></article>
    <article className="panel"><div className="panel-heading"><div><span className="eyebrow">风险观察</span><h3>利润率下降排名</h3></div><div className="figure-tag">百分点</div></div>
      <ReactECharts option={rank} style={{height:250}} notMerge lazyUpdate/><small className="chart-note">2025 相对于 2024 的变化，负值表示下降</small></article>
  </div>
}
