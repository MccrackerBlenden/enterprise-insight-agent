import { Component, lazy, Suspense, useEffect, useState } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import type { Overview } from './OverviewCharts'
import type { VerifiedAnalysis } from './VerifiedAnalysis'
import type { MetricArtifact } from './MetricAnalysis'
import MarkdownView from './MarkdownView'
import RunHistory from './RunHistory'
import TraceTimeline from './TraceTimeline'
import { clearRunHistory, loadRunHistory, saveRunHistory } from './history'
import type { EventRecord, RunSnapshot } from './history'
import { buildMarkdownReport, downloadReport } from './report'
import {
  ArrowRight, BarChart3, BookOpenText, Bot, ChevronRight, Truck,
  CircleAlert, Database, Download, GitBranch, History,
  LoaderCircle, Play, ShieldCheck, Sparkles, Workflow, XCircle, Clock3, CheckCircle2
} from 'lucide-react'

const OverviewCharts = lazy(() => import('./OverviewCharts'))
const VerifiedAnalysisPanel = lazy(() => import('./VerifiedAnalysis'))
const MetricAnalysisPanel = lazy(() => import('./MetricAnalysis'))
const OperationsWorkspace = lazy(() => import('./OperationsWorkspace'))
const OperationsWorkspaceP2 = lazy(() => import('./OperationsWorkspaceP2'))

// Keep the rest of the product available if the lazy-loaded operations view crashes.
class OperationsViewBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError(): { failed: boolean } { return { failed: true } }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Operations view failed:', error, info.componentStack)
  }
  render() {
    if (!this.state.failed) return this.props.children
    return <section role="alert" className="ops-error-boundary">
      <h2>运营工作区暂时无法显示</h2>
      <p>只有该页面发生了异常，原决策总览和分析工作台仍可使用。刷新后重试；若持续出现，请查看浏览器控制台的首条报错。</p>
      <button onClick={() => window.location.reload()}>刷新工作区</button>
    </section>
  }
}


type Health = { status: string; model_ready: boolean; synthetic_data: boolean }

const presets = [
  { icon: '01', title: '利润率下降分析', description: '定位下降幅度最大的企业及成本变化',
    question: '找出2025年相较2024年利润率下降最明显的三家企业，列出利润率及变化的百分点，并分析成本或费用的影响。' },
  { icon: '02', title: '企业经营排名', description: '识别盈利表现最优与最弱企业',
    question: '根据2025年全年数据，按利润率对12家企业排序，找出表现最好和最差的三家，并给出真实指标。' },
  { icon: '03', title: '行业趋势对比', description: '观察不同行业的经营表现',
    question: '比较2023年至2025年三个行业的营业收入和利润率变化，并指出趋势差异。' },
]

function compactValue(v: number): string { return (v / 10000).toFixed(2) + ' 亿' }
function pct(v: number): string { return v.toFixed(2) + '%' }
export default function App() {
  const [view, setView] = useState<'home'|'workspace'|'operations'>(
    () => { const params = new URLSearchParams(window.location.search); return params.has('opsIncident') || params.has('opsArea') ? 'operations' : 'home' })
  const [legacyOps, setLegacyOps] = useState(false)
  const [overview, setOverview] = useState<Overview|null>(null)
  const [health, setHealth] = useState<Health|null>(null)
  const [loading, setLoading] = useState(true)
  const [question, setQuestion] = useState(presets[0].question)
  const [events, setEvents] = useState<EventRecord[]>([])
  const [answer, setAnswer] = useState('')
  const [verified, setVerified] = useState<VerifiedAnalysis | null>(null)
  const [metricArtifacts, setMetricArtifacts] = useState<MetricArtifact[]>([])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [finished, setFinished] = useState(false)
  const [history, setHistory] = useState<RunSnapshot[]>(loadRunHistory)
  const [historyMode, setHistoryMode] = useState(false)
  const [storageError, setStorageError] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)

  useEffect(() => { if (!saveRunHistory(history)) setStorageError(true) }, [history])
  useEffect(() => {
    if (view === 'operations') return
    const url = new URL(window.location.href)
    if (url.searchParams.has('opsIncident') || url.searchParams.has('opsArea') || url.searchParams.has('opsStage')) {
      url.searchParams.delete('opsIncident'); url.searchParams.delete('opsArea'); url.searchParams.delete('opsStage')
      window.history.replaceState(null, '', url)
    }
  }, [view])
  useEffect(() => {
    if (!historyOpen) return
    const closeOnEscape = (e: KeyboardEvent) => { if (e.key === 'Escape') setHistoryOpen(false) }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [historyOpen])

  useEffect(() => {
    Promise.all([
      fetch('/api/overview').then(r => { if (!r.ok) throw new Error('数据库请求失败'); return r.json() as Promise<Overview> }),
      fetch('/api/health').then(r => { if (!r.ok) throw new Error('健康检查失败'); return r.json() as Promise<Health> })
    ]).then(([o, h]) => { setOverview(o); setHealth(h) })
      .catch(() => setError('无法连接后端，请确认 FastAPI 正在 8000 端口运行。'))
      .finally(() => setLoading(false))
  }, [])

  const latest = overview?.years.at(-1)

  function openSnapshot(snapshot: RunSnapshot) {
    if (running) return
    setQuestion(snapshot.question)
    setAnswer(snapshot.answer)
    setVerified(snapshot.verified)
    setMetricArtifacts(snapshot.metricArtifacts)
    setEvents(snapshot.events)
    setError('')
    setFinished(true)
    setHistoryMode(true)
    setHistoryOpen(false)
    setView('workspace')
  }

  function clearHistory() {
    if (running || !window.confirm('确定清空这台浏览器保存的历史任务快照吗？此操作不可撤销。')) return
    clearRunHistory()
    setHistory([])
    setStorageError(false)
  }

  async function run(task = question) {
    if (running || !task.trim()) return
    setQuestion(task)
    setHistoryMode(false)
    setView('workspace')
    setRunning(true)
    setFinished(false)
    setAnswer('')
    setVerified(null)
    setMetricArtifacts([])
    setError('')
    setEvents([])
    const captured: EventRecord[] = []
    const artifacts: MetricArtifact[] = []
    let audit: VerifiedAnalysis | null = null
    try {
      const response = await fetch('/api/run/stream', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: task.trim() })
      })
      if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）`)
      if (!response.body) throw new Error('浏览器不支持流式响应')
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let gotTerminal = false
      const processFrame = (frame: string) => {
        const lines = frame.split(/\r?\n/)
        const type = lines.find(line => line.startsWith('event:'))?.slice(6).trim() ?? 'status'
        const raw = lines.filter(line => line.startsWith('data:')).map(s => s.slice(5).trim()).join('\n')
        if (!raw) return
        const data = JSON.parse(raw) as Record<string, unknown>
        captured.push({ event: type, data, id: captured.length + 1, at: Date.now() })
        setEvents([...captured])
        if (type === 'verified_analysis' && data.kind === 'margin_decline_2024_2025') {
          audit = data as VerifiedAnalysis
          setVerified(audit)
        }
        if (type === 'metric_artifact' && data.kind === 'metric_analysis' && (data.audit as {status?: string}|undefined)?.status === 'passed') {
          artifacts.push(data as MetricArtifact)
          setMetricArtifacts([...artifacts])
        }
        if (type === 'done') {
          const completedAnswer = String(data.answer ?? '').trim()
          setAnswer(completedAnswer)
          setFinished(Boolean(completedAnswer))
          gotTerminal = true
          if (completedAnswer) {
            const snapshot: RunSnapshot = {
              id: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
              createdAt: new Date().toISOString(), question: task.trim(), answer: completedAnswer,
              verified: audit, metricArtifacts: [...artifacts], events: [...captured],
            }
            setHistory(old => [snapshot, ...old].slice(0, 8))
          }
        }
        if (type === 'error') { setError(String(data.message ?? 'Agent 执行失败')); gotTerminal = true }
      }
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
        let splitAt = buffer.indexOf('\n\n')
        while (splitAt !== -1) {
          processFrame(buffer.slice(0, splitAt))
          buffer = buffer.slice(splitAt + 2)
          splitAt = buffer.indexOf('\n\n')
        }
      }
      if (buffer.trim()) processFrame(buffer)
      if (!gotTerminal) setError('连接结束但未收到完成信号，请检查后端日志。')
    } catch (e) {
      setError(e instanceof Error ? e.message : '无法连接 Agent 后端')
    } finally { setRunning(false) }
  }

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><img className="brand-logo" src="/eia-brand-icon.png" alt="Enterprise Insight Agent 品牌标识"/>
        <div className="brand-copy"><strong>Enterprise Insight</strong><small>Agent · 企业经营洞察</small></div></div>
      <div className="nav-label">工作空间</div>
      <button className={'nav-link '+(view === 'home'?'active':'')} onClick={() => setView('home')}><BarChart3 size={18}/> 决策总览</button>
      <button className={'nav-link '+(view === 'workspace'?'active':'')} onClick={() => setView('workspace')}><Bot size={18}/> Agent 工作台</button>
      <button className={'nav-link '+(view === 'operations'?'active':'')} onClick={() => setView('operations')}><Truck size={18}/> 运营处置中心</button>
      <button className="nav-link history-nav" onClick={() => { setView('workspace'); setHistoryOpen(true) }}><History size={17}/> 任务历史 <span className="nav-count">{history.length}</span></button>
      <div className="nav-label second-label">数据环境</div>
      <div className="nav-static"><Database size={17}/> 企业经营数据 <span>12</span></div>
      <div className="nav-static"><GitBranch size={17}/> 分析工具链</div>
      <div className="sidebar-bottom">
        <div className="tiny-label">运行状态</div>
        <div className="status-line"><span className={'status-dot '+(health?.model_ready?'on':'off')}></span>
          {loading?'检测连接中…':health?.model_ready?'模型服务已配置':'模型 API 尚未配置'}</div>
        <div className="sidebar-note">仅使用合成数据<br/>本地演示环境</div>
      </div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="breadcrumb"><span>经营洞察</span><ChevronRight size={16}/><strong>{view==='home'?'决策总览':view==='operations'?'运营处置中心':'Agent 工作台'}</strong></div>
        <div className="top-meta"><span className="env-pill"><span className="status-dot on"/>本地演示</span></div></header>
      <div className="body">
        {error && view==='home' && <div className="alert"><CircleAlert size={18}/>{error}</div>}
        {view==='home' ? <>
          <section className="overview-command" aria-label="决策总览指挥台">
            <div className="overview-command-main">
              <div className="overview-meta"><span>企业经营 · 可信决策</span></div>
              <h1>经营决策，<br/><span>从可靠的数据开始。</span></h1>
              <p>用可追溯的数据构建洞察，让 AI 负责判断方向，让确定性工具负责计算结果。</p>
              <div className="overview-command-actions"><button className="primary" onClick={() => setView('workspace')}>启动 Agent 工作台 <ArrowRight size={16}/></button><button className="overview-quiet-action" onClick={() => setHistoryOpen(true)}><History size={16}/> 查看任务历史</button></div>
              <div className="overview-command-foot"><span>独立合成数据</span><span>只读计算</span><span>证据可追溯</span></div>
            </div>
            <div className="overview-system-card">
              <div className="overview-system-head"><span>服务状态</span></div>
              <div className="overview-system-state"><span className={'status-dot '+(health?.model_ready?'on':'off')}/><strong>{loading?'检测服务状态':health?.model_ready?'模型已就绪':'尚未配置模型'}</strong></div>
              <p>{health?.model_ready?'模型服务已配置，可以执行真实工具调用。':'仍可查看合成数据，执行 Agent 需要先配置模型。'}</p>
              <div className="overview-system-grid"><div><span>数据时间范围</span><strong>2023—2025</strong></div><div><span>演示企业</span><strong>{overview?.companies ?? '—'} <small>家</small></strong></div></div>
              <div className="overview-system-history"><span>最近分析</span>{history[0] ? <button type="button" onClick={()=>openSnapshot(history[0])}><span>{history[0].question}</span><ArrowRight size={14}/></button> : <small>尚无历史分析任务</small>}</div>
              <div className="overview-system-decoration" aria-hidden="true"><span/><span/><span/><span/><span/><span/><span/><span/></div>
            </div>
          </section>
          <section className="overview-data-section" aria-label="经营数据概览">
            <div className="overview-section-title"><div><span className="eyebrow">01 · 经营数据</span><h2>经营数据概览</h2><p>所有指标来自演示数据库，由程序计算，并非模型推测。</p></div><span className="overview-updated">计算口径可复现</span></div>
            <div className="overview-stats">
              <div className="overview-stat-feature"><span className="overview-stat-label">2025 年营业收入</span><strong>{latest ? compactValue(latest.revenue) : '—'}</strong><span className="overview-stat-sub">十二家虚构企业的年度合计营业收入</span></div>
              <div className="overview-stat"><div className="overview-stat-top"><span>企业数量</span></div><strong>{overview?.companies ?? '—'}</strong><small>3 个虚构行业</small></div>
              <div className="overview-stat"><div className="overview-stat-top"><span>加权净利润率</span></div><strong>{latest ? pct(latest.margin) : '—'}</strong><small>2025 年全年</small></div>
              <div className="overview-stat"><div className="overview-stat-top"><span>月度记录</span></div><strong>{overview?.records ?? '—'}</strong><small>2023—2025 年</small></div>
            </div>
          </section>
          <section className="overview-insight-section" aria-label="经营趋势图表">
            <div className="overview-section-title"><div><span className="eyebrow">02 · 趋势与风险</span><h2>趋势与风险观察</h2><p>年度营收与利润率变化，由后端汇总。</p></div><span className="overview-updated">来自演示数据库</span></div>
            <Suspense fallback={<div className="chart-loading">正在加载经营数据图表…</div>}><OverviewCharts data={overview} mode="dashboard"/></Suspense>
          </section>
          <section className="overview-launch-section" aria-label="快捷 Agent 分析">
            <div className="overview-section-title"><div><span className="eyebrow">03 · 发起分析</span><h2>将观察变为行动</h2><p>从真实业务问题开始，交给 Agent 查询、计算与解释。</p></div><span className="overview-updated">点击「运行」将调用模型 API</span></div>
            <div className="overview-mission-grid">{presets.map(p => <article key={p.icon} className="overview-mission-card"><span className="overview-mission-index">{p.icon} · 分析示例</span><div className="overview-mission-content"><h3>{p.title}</h3><p>{p.description}</p></div><div className="overview-mission-actions"><button type="button" onClick={()=>{setQuestion(p.question);setView('workspace')}}>编辑任务 <ArrowRight size={14}/></button><button type="button" className="overview-mission-run" onClick={()=>void run(p.question)} disabled={running}><Play size={13}/> 立即运行</button></div></article>)}</div>
          </section>
          <p className="disclaimer"><CircleAlert size={15}/> 演示数据库全部由程序生成，企业与数据均为虚构，不用于真实经营或投资决策。</p>
        </> : view === 'operations' ? <>
          <div className="ops-p2-fallback-bar"><span>运营处置中心 · {legacyOps ? '经典工作台' : '运营工作台'}</span>
            <button type="button" onClick={() => setLegacyOps(value => !value)}>
              {legacyOps ? '返回运营工作台' : '切换经典工作台'}
            </button></div>
          <OperationsViewBoundary key={legacyOps ? 'legacy' : 'p2'}>
            <Suspense fallback={<div className="chart-loading">正在加载运营处置中心…</div>}>
              {legacyOps ? <OperationsWorkspace/> : <OperationsWorkspaceP2/>}
            </Suspense>
          </OperationsViewBoundary></> : <>
          <section className="mission-header" aria-label="Agent 工作台概览">
            <div className="mission-copy">
              <div className="mission-overline"><span className="eyebrow">智能分析工作台</span></div>
              <h1>从问题出发，<span>找到有证据的答案。</span></h1>
              <p>一句话发起经营分析，实时观察 AI 的工具选择、计算依据与最终报告。所有演示企业均为虚构。</p>
              <div className="mission-facts"><span><Database size={14}/>{overview?.companies ?? '—'} 家演示企业</span><span><ShieldCheck size={14}/> 数据只读 · 可追溯</span><span><History size={14}/>{history.length} 条本地任务</span></div>
            </div>
            <div className="mission-control">
              <span className="mission-control-label">当前任务</span>
              <div className="mission-control-status"><span className={'status-dot '+(running?'pulse':finished?'on':'off')}/><strong>{running?'任务执行中':historyMode?'历史快照':finished?'本次任务已完成':'待命 · 可以开始'}</strong></div>
              <span className="mission-control-detail">{running ? '实时执行轨迹正在更新' : historyMode ? '已恢复先前执行结果，未重新调用模型' : finished ? '可查看分析成果与任务证据' : health?.model_ready ? '模型已就绪，等待提交分析任务' : '模型暂未就绪，请检查后端配置'}</span>
              <button className="mission-control-link" type="button" onClick={() => setHistoryOpen(true)}><Clock3 size={14}/> 查看历史任务 <ArrowRight size={15}/></button>
            </div>
          </section>
          <div className="workspace-grid">
            <div className="workspace-main">
              <section className="panel prompt-panel">
                <div className="task-stage-heading"><div className="task-stage-id">01</div><div><span className="eyebrow">新建分析</span><h2>描述你的分析任务</h2></div><span className="task-stage-extra">自然语言 → 工具调用</span></div>
                <label className="prompt-input-label" htmlFor="analysis-question"><BookOpenText size={15}/> 任务输入</label>
                <textarea id="analysis-question" aria-label="分析任务" value={question} onChange={(e: { target: { value: string } }) => setQuestion(e.target.value)} placeholder="例如：分析 2025 年利润率下降最严重的企业…" rows={4} maxLength={500} disabled={running}/>
                <div className="prompt-actions"><span>{question.length}/500 字 · 数据来自独立合成数据库</span><button className="primary" onClick={()=>run()} disabled={running || question.trim().length<5}>{running?<LoaderCircle size={17} className="spin"/>:<Play size={17}/>} {running?'执行中…':'执行分析'} <ArrowRight size={16}/></button></div>
                <div className="task-suggestions"><div className="task-suggestions-heading"><span>常用分析任务</span><small>选择后填入任务，不会自动调用模型</small></div>
                  <div className="task-suggestion-grid">{presets.map(p => <button key={p.icon} type="button" className="task-suggestion" disabled={running} onClick={() => setQuestion(p.question)}><span className="task-suggestion-no">{p.icon}</span><span>{p.title}</span><ArrowRight size={14}/></button>)}</div>
                </div>
              </section>
              {historyMode && <div className="snapshot-banner"><History size={17}/> 正在查看本地历史快照。这不是新的 Agent 执行；如需更新结果，请点击上方“运行 Agent”。</div>}
              <section className="panel answer-panel"><div className="panel-heading result-panel-heading"><div className="result-stage-title"><div className="task-stage-id">02</div><div><span className="eyebrow">分析结果</span><h3>{historyMode ? '历史任务成果' : '分析与证据'}</h3></div></div>
                {finished && answer && <button className="outline" onClick={() => downloadReport(buildMarkdownReport({ question, answer, verified, metricArtifacts, events }))}><Download size={15}/> 下载完整报告</button>}</div>
                {(answer || running) && <div className="result-trust-line"><span><Sparkles size={13}/> AI 解释</span><span><ShieldCheck size={13}/> 程序校验结果独立呈现</span><span>{events.length} 条执行事件</span></div>}
                {running && !answer && <div className="empty-result"><LoaderCircle className="spin" size={30}/><strong>Agent 正在执行</strong><span>工具调用与结果将在右侧实时显示</span></div>}
                {error && <div className="alert"><XCircle size={20}/><span>{error}</span></div>}
                {answer && <div className="answer-text"><MarkdownView text={answer}/></div>}
                {!running && !answer && !error && <div className="empty-result empty-report">
                  <div className="empty-report-symbol"><Workflow size={36}/><span className="empty-report-glow"/></div>
                  <div className="empty-report-copy"><span className="eyebrow">等待分析结果</span><strong>让结论有据可查</strong><p>任务执行后，这里会展示 Agent 的自然语言分析；经过程序核验的图表和证据表会独立出现。</p>
                    <div className="empty-report-steps"><span><CheckCircle2 size={14}/> 查询真实数据</span><span><CheckCircle2 size={14}/> 验证计算依据</span><span><CheckCircle2 size={14}/> 导出任务报告</span></div>
                  </div>
                </div>}
              </section>
              <Suspense fallback={<div className="chart-loading">正在载入核验图表…</div>}>
                {finished && verified && <VerifiedAnalysisPanel result={verified}/>}
                {finished && metricArtifacts.length > 0 && <MetricAnalysisPanel items={metricArtifacts}/>}
              </Suspense>
              <Suspense fallback={<div className="chart-loading">正在加载全局参考图表…</div>}><OverviewCharts data={overview} mode="reference"/></Suspense>
              {storageError && <p className="storage-alert">浏览器阻止了本地历史存储，当前结果仍可导出，但刷新后可能无法恢复。</p>}
            </div>
            <TraceTimeline events={events}/>
          </div>
        </>}
      </div>
      <footer><span>Enterprise Insight Agent</span><span>本地优先 · 合成演示数据 · 非真实企业信息</span><span>作品演示版本</span></footer>
    </main>
    {historyOpen && <div className="history-overlay"><button type="button" className="history-scrim" aria-label="关闭任务历史" onClick={() => setHistoryOpen(false)}/><section className="history-drawer" role="dialog" aria-modal="true" aria-label="本地任务历史"><div className="history-drawer-head"><div><span className="eyebrow">最近任务</span><h2>任务历史</h2><p>本地保存 · 最多八次已完成任务</p></div><button className="history-close" type="button" onClick={() => setHistoryOpen(false)} aria-label="关闭"><XCircle size={20}/></button></div><RunHistory items={history} busy={running} onOpen={openSnapshot} onRerun={(q) => { setHistoryOpen(false); void run(q) }} onClear={clearHistory}/></section></div>}
  </div>
}
