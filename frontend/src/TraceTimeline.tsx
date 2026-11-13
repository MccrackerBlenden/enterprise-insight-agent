import { useMemo, useState } from 'react'
import { Activity, CheckCircle2, ChevronDown, Clock3, Database, ShieldCheck, Terminal, XCircle } from 'lucide-react'
import type { EventRecord } from './history'

type Filter = 'all' | 'tools' | 'issues'

function kindLabel(event: string, data: Record<string, unknown>): string {
  switch (event) {
    case 'tool_call': return `调用 ${String(data.name ?? '工具')}`
    case 'tool_result': return `${String(data.name ?? '工具')} 返回结果`
    case 'metric_artifact': return `指标计算：${String(data.title ?? '已核验')}`
    case 'verified_analysis': return '利润率专项核验'
    case 'done': return '任务执行完成'
    case 'error': return '执行异常'
    default: return '执行状态'
  }
}

function brief(event: EventRecord): string {
  const data = event.data
  if (event.event === 'tool_call') {
    const args = data.args
    if (args && typeof args === 'object' && 'sql' in args && typeof (args as Record<string, unknown>).sql === 'string') {
      return String((args as Record<string, unknown>).sql).replace(/\s+/g, ' ').slice(0, 115)
    }
    return Object.keys(args && typeof args === 'object' ? args : {}).length ? '已向工具传递参数，点击展开查看' : '无需参数'
  }
  if (event.event === 'tool_result') {
    const message = String(data.message ?? '')
    try {
      const parsed = JSON.parse(message)
      if (parsed.error) return `工具报告错误：${String(parsed.error).slice(0, 120)}`
      if (Array.isArray(parsed.rows)) return `查询成功，返回 ${parsed.rows.length} 行数据`
      if (parsed.kind === 'metric_analysis') return '指标已完成程序化计算'
    } catch { /* result may be truncated or plain text */ }
    return message.length > 120 ? message.slice(0, 120) + '…' : message || '已收到响应'
  }
  if (event.event === 'metric_artifact') {
    const audit = data.audit as { status?: string; records_covered?: number } | undefined
    return `${String(audit?.status ?? '待检查')} · ${String(audit?.records_covered ?? '—')} 条记录`
  }
  if (event.event === 'verified_analysis') return '基于合成数据库的独立数值核验已生成'
  if (event.event === 'done') return '已收到最终分析结果'
  return String(data.message ?? '').slice(0, 180) || '收到运行事件'
}

function getDuration(events: EventRecord[], index: number): string | null {
  const target = events[index]
  if (target.event !== 'tool_result') return null
  const name = String(target.data.name ?? '')
  for (let i = index - 1; i >= 0; i--) {
    if (events[i].event === 'tool_call' && String(events[i].data.name ?? '') === name) {
      // Approximate observed interval; not the backend's precise tool execution duration.
      const diff = target.at - events[i].at
      return diff >= 0 ? diff < 1000 ? `约 ${diff}ms` : `约 ${(diff / 1000).toFixed(1)}s` : null
    }
  }
  return null
}

export default function TraceTimeline({ events }: { events: EventRecord[] }) {
  const [filter, setFilter] = useState<Filter>('all')
  const filtered = useMemo(() => events.map((event, index) => ({ event, index })).filter(({ event }) =>
    filter === 'all' || (filter === 'tools' && ['tool_call', 'tool_result', 'metric_artifact', 'verified_analysis'].includes(event.event)) ||
    (filter === 'issues' && event.event === 'error' || filter === 'issues' && event.event === 'tool_result' && brief(event).startsWith('工具报告错误'))
  ), [events, filter])
  return <aside className="trace-panel panel" aria-label="Agent 执行轨迹">
    <div className="trace-title"><Terminal size={18}/> 执行链路 <span>{events.length} 条真实事件</span></div>
    <div className="trace-intro">默认显示摘要；展开后可查看实际参数与结果。时间为浏览器接收时间，工具耗时为近似观测间隔。</div>
    <div className="trace-filters" role="group" aria-label="筛选执行事件">
      {(['all', 'tools', 'issues'] as Filter[]).map(f => <button type="button" key={f} className={filter === f ? 'selected' : ''} onClick={() => setFilter(f)}>{f === 'all' ? '全部' : f === 'tools' ? '工具与核验' : '异常'}</button>)}
    </div>
    <div className="trace-list">
      {!filtered.length ? <div className="trace-placeholder"><Activity size={24}/><span>{events.length ? '当前筛选没有事件' : '暂无执行事件'}</span></div> : filtered.map(({ event: e, index }) => {
        const bad = e.event === 'error' || brief(e).startsWith('工具报告错误')
        const relative = events[0] ? Math.max(0, e.at - events[0].at) / 1000 : 0
        const duration = getDuration(events, index)
        return <div className="trace-event" key={e.id}>
          <div className={`trace-event-icon ${bad ? 'bad' : e.event === 'done' ? 'good' : ''}`}>
            {bad ? <XCircle size={14}/> : e.event === 'tool_call' ? <Database size={14}/> : e.event === 'metric_artifact' || e.event === 'verified_analysis' ? <ShieldCheck size={14}/> : e.event === 'done' || e.event === 'tool_result' ? <CheckCircle2 size={14}/> : <Activity size={14}/>}
          </div>
          <div className="trace-event-body">
            <div className="trace-event-heading"><strong>{kindLabel(e.event, e.data)}</strong><small>+{relative.toFixed(1)}s</small></div>
            <p className={bad ? 'trace-error-text' : ''}>{brief(e)}</p>
            {duration && <span className="trace-duration"><Clock3 size={12}/>{duration}</span>}
            {['tool_call', 'tool_result', 'metric_artifact', 'verified_analysis'].includes(e.event) &&
              <details className="trace-details"><summary>查看原始事件 <ChevronDown size={12}/></summary><pre>{JSON.stringify(e.data, null, 2).slice(0, 14000)}</pre></details>}
          </div>
        </div>
      })}
    </div>
  </aside>
}
