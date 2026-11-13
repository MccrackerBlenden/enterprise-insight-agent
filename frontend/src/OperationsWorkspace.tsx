import { useCallback, useEffect, useState } from 'react'
import { Activity, ArrowRight, CheckCircle2, CircleAlert, ClipboardCheck,
  FileSearch, LoaderCircle, RefreshCw, ShieldCheck, Truck, Warehouse,
  ChevronDown, ArrowUpRight, Clock3 } from 'lucide-react'
import MarkdownView from './MarkdownView'
import './operations.css'

type Incident = {
  incident_id: string; order_id: string; severity: string; state: string; kind: string;
  summary: string; order_status: string; warehouse_id: string | null; sku_id: string;
  quantity: number; inventory?: Array<{ warehouse_id: string; on_hand: number; reserved: number; available: number }>
}
type Option = { warehouse_id: string; eligible: boolean; blockers: string[];
  simulated_cost_points: number | null; simulated_handling_hours: number | null;
  simulated_late_minutes: number | null; available: number }
type Quote = { order_id: string; snapshot_fingerprint: string; recommended_warehouse_id: string | null;
  policy_version: string; assumptions: string; options: Option[] }
type Decision = { synthetic: boolean; selected_warehouse_id: string | null;
  recommended_warehouse_id: string | null; selection_source: string;
  operator_reason: string | null; rationale: string; decision_sha256: string;
  evidence_sha256: string; policy_version: string; limitations: string;
  candidates: Array<{ warehouse_id: string; eligible: boolean; blockers: string[];
    simulated_cost_points: number | null; simulated_late_minutes: number | null }> }
type QueueEntry = {
  kind: 'incident' | 'task'; bucket: 'recovery' | 'approval' | 'attention' | 'ready' | 'new' | 'archive';
  task_id: string | null; incident_id: string; order_id: string; severity: string;
  incident_state: string; task_status: string | null; fulfillment_status: string | null;
  gate: string | null; label: string; reason: string; can_continue: boolean;
  updated_at: string | null;
}
type CaseHistory = {
  synthetic: boolean; incident_id: string; order_id: string;
  incident_state: string; order_status: string; shipment_status: string | null;
  consistency: string; has_more: boolean;
  source_counts: Record<string, number>;
  linked_tasks: Array<{ task_id: string; status: string; updated_at: string }>;
  items: Array<{ id: string; source: string; kind: string; description: string;
    created_at: string; task_id: string | null }>;
}
type TriageSnapshot = {
  synthetic: boolean; queue_rule_version: string; total_incidents: number;
  recent_task_count: number; task_limit: number; summary: Record<string, number>;
  items: QueueEntry[]; incidents: Incident[]; tasks: Task[];
}
const queueFilters = [
  ['active', '当前待办'], ['approval', '人工确认'], ['recovery', '恢复'],
  ['ready', '可推进'], ['new', '待调查'], ['attention', '人工处理'], ['archive', '已归档'],
] as const
const severityNames: Record<string, string> = { high: '高', medium: '中', low: '低' }

type InvestigationDossier = { investigation_id: string; model_status: string;
  model_summary: string | null; evidence_sha256: string; model_text_independently_verified: boolean;
  evidence: { incident: { order_id: string; state: string; order_status: string; quantity: number };
    inventory: Array<{ warehouse_id: string; available: number }>;
    signals: Array<{ signal_id: string; status: string; rule_code: string }>;
    quote: { recommended_warehouse_id: string | null; policy_version: string } } }
type ModelPlan = { plan_id: string; investigation_id: string; adopted_task_id: string | null;
  plan: { next_action: string; warehouse_id: string | null; rationale: string; risks: string[];
    policy_recommended_warehouse_id: string | null; plan_sha256: string;
    evidence_sha256: string; limitations: string; revalidated_from_plan_id?: string; model_reused_without_new_call?: boolean } }
type Fulfillment = { status: string; next_step: number; inflight_step: string | null;
  carrier: string; failure_code: string | null; handoff_reason: string | null }
type Playbook = { synthetic: boolean; gate: string; can_continue: boolean; next_action: string | null;
  reason: string; incident_state: string | null; order_status: string | null; requires_live_model: boolean }
type Task = { task_id: string; incident_id: string; status: string; warehouse_id: string | null;
  fulfillment?: Fulfillment | null; playbook?: { steps_executed: string[]; next: Playbook };
  proposal: { action: string; scope: string; quote: Quote;
    investigation_id?: string; model_plan_id?: string; evidence_sha256?: string; model_narrative_independently_verified?: boolean; decision?: Decision }; result: null | {
    verified: boolean; actual_order_status: string; warehouse_id: string; scope: string
  }; failure_code: string | null;
  events?: Array<{ event_id: number; kind: string; created_at: string; detail: Record<string, unknown> }> }

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(body.detail || `HTTP ${response.status}`)
  }
  return response.json() as Promise<T>
}

const statusNames: Record<string, string> = {
  open: '待调查', investigating: '调查中', escalated: '人工升级', resolved: '已核验解决',
  blocked: '无可执行方案', pending_approval: '等待批准', approved: '已批准',
  rejected: '已拒绝', executing: '执行中 / 可恢复', succeeded: '预留已核验',
  stale: '计划已过期', failed: '执行失败',
  awaiting_delivery_confirmation: '待确认演示签收',
  completed: '履约已核验闭环', handoff: '已转交人工',
}
const fulfillmentSteps = ['创建模拟运单', '更新模拟运输状态', '人工确认演示签收', '独立核验并关闭事件']

export default function OperationsWorkspace() {
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [selectedId, setSelectedId] = useState<string>('')
  const [detail, setDetail] = useState<Incident | null>(null)
  const [quote, setQuote] = useState<Quote | null>(null)
  const [decisionPreview, setDecisionPreview] = useState<Decision | null>(null)
  const [selectedWarehouse, setSelectedWarehouse] = useState('')
  const [selectionReason, setSelectionReason] = useState('')
  const [tasks, setTasks] = useState<Task[]>([])
  const [queue, setQueue] = useState<QueueEntry[]>([])
  const [queueSummary, setQueueSummary] = useState<Record<string, number>>({})
  const [queueFilter, setQueueFilter] = useState<string>('active')
  const [queueSearch, setQueueSearch] = useState('')
  const [caseTab, setCaseTab] = useState<'work' | 'timeline'>('work')
  const [lastRefreshed, setLastRefreshed] = useState('')
  const [caseHistory, setCaseHistory] = useState<CaseHistory | null>(null)
  const [caseHistoryLoading, setCaseHistoryLoading] = useState(false)
  const [caseHistoryError, setCaseHistoryError] = useState('')
  const [caseRevision, setCaseRevision] = useState(0)
  const [activeTaskId, setActiveTaskId] = useState<string>('')
  const [activeTask, setActiveTask] = useState<Task | null>(null)
  const [playbook, setPlaybook] = useState<Playbook | null>(null)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [investigation, setInvestigation] = useState('')
  const [investigationId, setInvestigationId] = useState('')
  const [dossier, setDossier] = useState<InvestigationDossier | null>(null)
  const [agentTrace, setAgentTrace] = useState<string[]>([])
  const [modelPlan, setModelPlan] = useState<ModelPlan | null>(null)
  const [linkedModelPlan, setLinkedModelPlan] = useState<ModelPlan | null>(null)
  const [deliveryAttested, setDeliveryAttested] = useState(false)
  const [handoffReason, setHandoffReason] = useState('')
  const [releaseReservation, setReleaseReservation] = useState(false)

  const refresh = useCallback(async (nextId?: string) => {
    const snapshot = await jsonRequest<TriageSnapshot>('/api/ops/agent/triage')
    setIncidents(snapshot.incidents)
    setTasks(snapshot.tasks)
    setQueue(snapshot.items)
    setQueueSummary(snapshot.summary)
    setLastRefreshed(new Date().toLocaleTimeString('zh-CN', {hour12: false}))
    setCaseRevision(v => v + 1)
    setSelectedId(previous => nextId || previous || snapshot.items.find(item => item.bucket !== 'archive')?.incident_id || snapshot.incidents[0]?.incident_id || '')
  }, [])

  useEffect(() => { void refresh().catch(e => setError(String(e))) }, [refresh])
  useEffect(() => {
    if (!selectedId) { setDetail(null); setQuote(null); setDecisionPreview(null); return }
    let cancelled = false
    setLoading(true)
    Promise.all([
      jsonRequest<Incident>(`/api/ops/incidents/${encodeURIComponent(selectedId)}`),
      incidents.find(i => i.incident_id === selectedId)?.order_id
        ? jsonRequest<Quote>(`/api/ops/orders/${encodeURIComponent(incidents.find(i => i.incident_id === selectedId)!.order_id)}/fulfillment-options`)
        : Promise.resolve(null),
      jsonRequest<Decision>(`/api/ops/agent/incidents/${encodeURIComponent(selectedId)}/decision-preview`),
    ]).then(([d, q, decision]) => { if (!cancelled) { setDetail(d); setQuote(q); setDecisionPreview(decision) } })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [selectedId, incidents])
  useEffect(() => {
    if (!selectedId || activeTaskId) return
    let cancelled = false
    void jsonRequest<{ item: ModelPlan | null }>(
      `/api/ops/agent/incidents/${encodeURIComponent(selectedId)}/latest-model-plan`)
      .then(({item}) => {
        if (!cancelled && item) {
          setModelPlan(existing => existing || item)
          setInvestigationId(existing => existing || item.investigation_id)
        }
      })
      .catch(() => {}) // No prior plan is a normal first-use state.
    return () => { cancelled = true }
  }, [selectedId, activeTaskId])

  useEffect(() => {
    if (!selectedId) { setCaseHistory(null); setCaseHistoryError(''); return }
    let cancelled = false
    setCaseHistory(null)
    setCaseHistoryError('')
    setCaseHistoryLoading(true)
    void jsonRequest<CaseHistory>(
      `/api/ops/incidents/${encodeURIComponent(selectedId)}/case-history?limit=100`)
      .then(result => { if (!cancelled) setCaseHistory(result) })
      .catch(e => { if (!cancelled) setCaseHistoryError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (!cancelled) setCaseHistoryLoading(false) })
    return () => { cancelled = true }
  }, [selectedId, caseRevision])

  useEffect(() => {
    if (!activeTaskId) { setActiveTask(null); return }
    let cancelled = false
    void jsonRequest<Task>(`/api/ops/agent/tasks/${encodeURIComponent(activeTaskId)}`)
      .then(result => { if (!cancelled) setActiveTask(result) })
      .catch(e => { if (!cancelled) setError(String(e)) })
    return () => { cancelled = true }
  }, [activeTaskId, tasks])

  useEffect(() => {
    const id = activeTask?.proposal.investigation_id
    if (!id) { setDossier(null); return }
    let cancelled = false
    void jsonRequest<InvestigationDossier>(`/api/ops/agent/investigations/${encodeURIComponent(id)}`)
      .then(result => { if (!cancelled) setDossier(result) })
      .catch(e => { if (!cancelled) setError(String(e)) })
    return () => { cancelled = true }
  }, [activeTask?.proposal.investigation_id])

  useEffect(() => {
    const id = activeTask?.proposal.model_plan_id
    if (!id) { setLinkedModelPlan(null); return }
    let cancelled = false
    void jsonRequest<ModelPlan>(`/api/ops/agent/model-plans/${encodeURIComponent(id)}`)
      .then(record => { if (!cancelled) setLinkedModelPlan(record) })
      .catch(e => { if (!cancelled) setError(String(e)) })
    return () => { cancelled = true }
  }, [activeTask?.proposal.model_plan_id])

  useEffect(() => {
    if (!activeTaskId) { setPlaybook(null); return }
    let cancelled = false
    void jsonRequest<Playbook>(`/api/ops/agent/tasks/${encodeURIComponent(activeTaskId)}/playbook`)
      .then(result => { if (!cancelled) setPlaybook(result) })
      .catch(e => { if (!cancelled) setError(String(e)) })
    return () => { cancelled = true }
  }, [activeTaskId, activeTask?.status, activeTask?.fulfillment?.status,
      activeTask?.fulfillment?.next_step, tasks])

  async function mutate(path: string, body: Record<string, unknown> = {}) {
    if (!token.trim()) { setError('请先输入本地演示写入令牌。令牌仅保存在当前页面内存中。'); return null }
    setBusy(true); setError(''); setMessage('')
    try {
      const result = await jsonRequest<Task>(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ops-Demo-Token': token.trim() },
        body: JSON.stringify(body),
      })
      setActiveTaskId(result.task_id)
      if (result.incident_id) setSelectedId(result.incident_id)
      setActiveTask(result)
      setDeliveryAttested(false)
      const advanced = result.playbook?.steps_executed
      setMessage(advanced ? `安全推进 ${advanced.length} 步；当前关口：${result.playbook?.next.gate}` :
        `任务 ${result.task_id}：${result.fulfillment ? (statusNames[result.fulfillment.status] || result.fulfillment.status) : (statusNames[result.status] || result.status)}`)
      await refresh()
      return result
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); return null }
    finally { setBusy(false) }
  }

  async function investigate() {
    if (!selectedId || !token.trim()) { setError('模型调查需要本地写入令牌，以防止未经授权的付费模型调用。'); return }
    setBusy(true); setError(''); setAgentTrace([]); setInvestigation(''); setInvestigationId(''); setModelPlan(null)
    try {
      const res = await fetch('/api/ops/agent/investigate/stream', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ops-Demo-Token': token.trim() },
        body: JSON.stringify({ incident_id: selectedId }),
      })
      if (!res.ok || !res.body) throw new Error(`模型请求失败：HTTP ${res.status}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''; let ended = false; let pendingInvestigationId = ''; const timeline: string[] = []
      const process = (frame: string) => {
        const kind = frame.split('\n').find(s => s.startsWith('event:'))?.slice(6).trim()
        const raw = frame.split('\n').filter(s => s.startsWith('data:')).map(s => s.slice(5).trim()).join('\n')
        if (!raw) return
        const data = JSON.parse(raw) as Record<string, unknown>
        if (kind === 'evidence') pendingInvestigationId = String(data.investigation_id || '')
        if (kind === 'tool_call' || kind === 'tool_result') {
          timeline.push(`${kind === 'tool_call' ? '调用' : '返回'}：${String(data.name || 'tool')}`)
          setAgentTrace([...timeline])
        }
        if (kind === 'done') { setInvestigation(String(data.answer || '')); setInvestigationId(pendingInvestigationId); ended = true }
        if (kind === 'error') { setError(String(data.message || '调查失败')); ended = true }
      }
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
        let pos = buffer.indexOf('\n\n')
        while (pos !== -1) { process(buffer.slice(0, pos)); buffer = buffer.slice(pos + 2); pos = buffer.indexOf('\n\n') }
      }
      if (buffer.trim()) process(buffer)
      if (!ended) throw new Error('流连接结束但没有完成事件')
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  async function requestModelPlan() {
    if (!investigationId || !token.trim()) { setError('请先完成模型调查并输入本地演示令牌'); return }
    setBusy(true); setError(''); setMessage('')
    try {
      const result = await jsonRequest<ModelPlan>(
        `/api/ops/agent/investigations/${encodeURIComponent(investigationId)}/model-plan`,
        { method: 'POST', headers: { 'X-Ops-Demo-Token': token.trim() } })
      setModelPlan(result)
      setSelectedWarehouse(result.plan.warehouse_id || '')
      setSelectionReason('')
      setMessage('模型建议已保存，尚未创建任务；需要人工确认与独立审批。')
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  async function refreshExpiredModelPlan() {
    if (!modelPlan || !token.trim() || modelPlan.adopted_task_id) return
    setBusy(true); setError(''); setMessage('')
    try {
      const updated = await jsonRequest<ModelPlan>(
        `/api/ops/agent/model-plans/${encodeURIComponent(modelPlan.plan_id)}/refresh`,
        { method: 'POST', headers: { 'X-Ops-Demo-Token': token.trim() } })
      setModelPlan(updated)
      setInvestigationId(updated.investigation_id)
      setSelectedWarehouse(updated.plan.warehouse_id || '')
      setSelectionReason('')
      setMessage(updated.plan_id === modelPlan.plan_id
        ? '本次方案仍处于有效窗口。可确认方案后创建待审批任务。'
        : '仅报价时间窗口变化；已免费重新核验业务数据和原模型建议。请重新审阅后采纳。')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }

  async function acceptModelPlan() {
    if (!modelPlan?.plan.warehouse_id || modelPlan.adopted_task_id) return
    const selected = modelPlan.plan.warehouse_id
    if (selected !== modelPlan.plan.policy_recommended_warehouse_id &&
        selectionReason.trim().length < 10) {
      setError('模型建议与程序推荐不同：请填写至少 10 字符的人工改选理由。')
      return
    }
    const task = await mutate(`/api/ops/agent/model-plans/${modelPlan.plan_id}/create-task`, {
      confirmed_warehouse_id: selected,
      ...(selected !== modelPlan.plan.policy_recommended_warehouse_id
        ? { selection_reason: selectionReason.trim() } : {}),
    })
    if (task) setModelPlan({ ...modelPlan, adopted_task_id: task.task_id })
  }

  const active = incidents.filter(i => i.state !== 'resolved')
  const inReview = queueSummary.approval || 0
  const visibleQueue = queue.filter(item => {
    if (queueFilter === 'active' && item.bucket === 'archive') return false
    if (queueFilter !== 'active' && item.bucket !== queueFilter) return false
    const search = queueSearch.trim().toLowerCase()
    return !search || [item.incident_id, item.order_id, item.task_id || '',
      item.label, item.reason].some(value => value.toLowerCase().includes(search))
  })
  const chooseQueueItem = (item: QueueEntry) => {
    setSelectedId(item.incident_id)
    setCaseTab('work')
    setDetail(null); setQuote(null); setDecisionPreview(null)
    setDeliveryAttested(false); setReleaseReservation(false); setHandoffReason('')
    setActiveTask(null) // never leave a previous task's approval controls on screen
    setActiveTaskId(item.task_id || '')
    setInvestigation(''); setInvestigationId(''); setModelPlan(null)
    setAgentTrace([]); setError(''); setSelectedWarehouse(''); setSelectionReason('')
  }
  const selectedQueueItem = queue.find(item => item.incident_id === selectedId &&
    (activeTaskId ? item.task_id === activeTaskId : item.kind === 'incident')) ||
    queue.find(item => item.incident_id === selectedId)
  const openQueueCount = queue.filter(item => item.bucket !== 'archive').length
  const closedCount = incidents.filter(item => item.state === 'resolved').length
  const nextActionTitle = activeTaskId
    ? (playbook?.gate === 'ready' ? '已具备执行条件，等待操作人推进' :
       playbook?.gate === 'reservation_approval' ? '等待库存预留审批' :
       playbook?.gate === 'fulfillment_approval' ? '等待独立履约审批' :
       playbook?.gate === 'delivery_attestation' ? '等待人工确认演示签收' :
       playbook?.gate === 'explicit_recovery' ? '当前需要人工恢复或接管' :
       playbook?.gate === 'completed' ? '履约任务已完成核验' :
       '查看任务状态及当前操作关口')
    : selectedQueueItem?.bucket === 'attention' ? '需要人工处理的异常' :
      '先核对业务事实，再制定可执行方案'

  return <div className="ops-ws">
    <section className="ops-heading">
      <div><span className="eyebrow">OPERATIONS INTELLIGENCE / INCIDENT RESPONSE</span>
        <h1>运营处置中心</h1>
        <p>将业务异常、调查证据、人工授权和履约执行集中在同一个事件工作空间。</p>
      </div>
      <div className="ops-heading-tools">
        <span className="ops-environment"><span className="ops-indicator"/> 合成业务环境 · 手动同步</span>
        <button className="ops-refresh" disabled={busy} onClick={() => void refresh().catch(e => setError(String(e)))}>
          <RefreshCw size={16}/> 刷新业务状态
        </button>
      </div>
    </section>
    <section className="ops-overview-strip" aria-label="运营关键状态">
      <div className="ops-overview-primary"><span>当前待办</span><strong>{openQueueCount}</strong><small>需关注的事件或任务</small></div>
      <div><span>等待人工确认</span><strong>{inReview}</strong><small>审批与签收关口</small></div>
      <div><span>活跃事件</span><strong>{active.length}</strong><small>来自 Ops 业务状态</small></div>
      <div><span>已核验事件</span><strong>{closedCount}</strong><small>不包含未完成的模拟处理</small></div>
    </section>
    <details className="ops-demo-access">
      <summary><span><ShieldCheck size={15}/> 本地演示权限 <strong>{token.trim() ? '已提供操作凭证' : '当前为只读模式'}</strong></span>
        <span>仅需执行业务操作时配置 <ChevronDown size={15}/></span></summary>
      <div className="ops-token"><div><strong>本地演示操作凭证</strong><small>令牌仅保留在当前浏览器页面内存中；不会进入历史记录。此方式不等于生产环境的身份认证。</small></div>
        <input aria-label="本地演示写入令牌" type="password" value={token} onChange={e => setToken(e.target.value)} placeholder="输入本地演示令牌" autoComplete="off"/>
      </div>
    </details>
    {error && <div className="ops-message error"><CircleAlert size={16}/>{error}
      {modelPlan && /stale|过期|expired/i.test(error) &&
        <a href="#ops-case-investigation">转到方案卡片，使用免费刷新</a>}
    </div>}
    {message && <div className="ops-message success"><CheckCircle2 size={16}/>{message}</div>}
    <div className="ops-layout">
      <section className="ops-surface ops-incidents">
        <div className="ops-panel-head"><div><span>事件队列</span><h2>优先处理</h2></div><small>{visibleQueue.length} / {queue.length} 项</small></div>
        <div className="ops-queue-tools">
          <label htmlFor="ops-queue-search">按事件、订单与执行任务检索</label>
          <input id="ops-queue-search" type="search" value={queueSearch}
            onChange={e => setQueueSearch(e.target.value)} placeholder="搜索事件 / 订单 / 任务" />
          <div className="ops-queue-filters" aria-label="运营队列筛选">
            {queueFilters.map(([code, label]) => <button key={code} type="button"
              aria-pressed={queueFilter === code} className={queueFilter === code ? 'active' : ''}
              onClick={() => setQueueFilter(code)}>{label}{code !== 'active' ? ` · ${queueSummary[code] || 0}` : ''}</button>)}
          </div>
          <small>来自持久化任务及审批关口 · 非实时推送</small>
        </div>
        <div className="ops-incident-list" aria-live="polite">
          {visibleQueue.map(item => <button key={`${item.kind}-${item.task_id || item.incident_id}`}
            type="button" className={'ops-incident ops-queue-item '+
              ((item.task_id ? activeTaskId === item.task_id : !activeTaskId && selectedId === item.incident_id) ? 'selected' : '')}
            onClick={() => chooseQueueItem(item)}>
            <span className="ops-incident-line"><strong>{item.incident_id}</strong>
              <span className={'ops-queue-bucket '+item.bucket}>{item.label}</span></span>
            <span className="ops-incident-name">{item.reason}</span>
            <span className="ops-incident-meta"><span className={'ops-priority-mark '+item.severity}>{severityNames[item.severity] || item.severity}风险</span>
              {item.order_id} {item.task_id && ` · ${item.task_id.slice(0, 12)}…`}</span>
          </button>)}
          {!visibleQueue.length && <div className="ops-queue-empty"><CheckCircle2 size={22}/><strong>当前视图没有待处理事项</strong><p>尝试切换筛选条件或清除搜索，查看其他事件。</p><button type="button" onClick={() => { setQueueFilter('active'); setQueueSearch('') }}>查看全部待办</button></div>}
        </div>
        <p className="ops-muted ops-queue-note">队列按服务端任务状态排序；已归档项目可通过筛选查看。{lastRefreshed && `最近同步 ${lastRefreshed}`}</p>
      </section>
      <section className="ops-case-workspace" aria-label="当前事件处置工作空间">
        {detail ? <header className="ops-case-hero">
          <div className="ops-case-hero-top"><span>INCIDENT RECORD / {detail.incident_id}</span>
            <span className={'ops-severity '+detail.severity}>{severityNames[detail.severity] || detail.severity}风险 · {statusNames[detail.state] || detail.state}</span></div>
          <h2>{detail.summary || '当前业务异常'}</h2>
          <div className="ops-case-context">
            <div><small>关联订单</small><strong>{detail.order_id}</strong></div>
            <div><small>业务对象</small><strong>{detail.sku_id} × {detail.quantity}</strong></div>
            <div><small>订单状态</small><strong>{statusNames[detail.order_status] || detail.order_status}</strong></div>
            <div><small>当前任务</small><strong>{activeTaskId ? (statusNames[activeTask?.fulfillment?.status || activeTask?.status || ''] || '正在读取任务') : '尚未建立'}</strong></div>
          </div>
          <div className="ops-case-next" aria-live="polite">
            <div><span>当前建议</span><strong>{nextActionTitle}</strong>
              <p>{playbook?.reason || (activeTaskId ? '所有变更必须经过服务端校验；具体可执行动作显示在右侧。' : '查看库存和报价，必要时运行 Agent 调查；生成方案不会自动执行库存写入。')}</p></div>
            <a href={activeTaskId ? '#ops-case-execution' : '#ops-case-investigation'}>{activeTaskId ? '查看处置关口' : '查看调查方案'}<ArrowUpRight size={15}/></a>
          </div>
        </header> : <div className="ops-case-hero ops-case-placeholder">
          <Clock3 size={21}/><strong>{loading ? '正在载入事件信息…' : '选择一项事件，查看完整处置上下文'}</strong>
          <p>从左侧队列进入事件；所有操作均依据当前业务状态，不会自动修改数据。</p>
        </div>}
        <div className="ops-case-tabs" role="tablist" aria-label="事件工作区视图">
          <button type="button" role="tab" aria-selected={caseTab === 'work'}
            className={caseTab === 'work' ? 'active' : ''} onClick={() => setCaseTab('work')}>调查与执行</button>
          <button type="button" role="tab" aria-selected={caseTab === 'timeline'}
            className={caseTab === 'timeline' ? 'active' : ''} onClick={() => setCaseTab('timeline')}>处置档案 <span>{caseHistory?.items.length || 0}</span></button>
        </div>
        {caseTab === 'work' && <div id="ops-case-work-view" role="tabpanel" className="ops-case-content">
      <section id="ops-case-investigation" className="ops-surface ops-investigate">
        <div className="ops-panel-head"><div><span>EVIDENCE / OPTIONS</span><h2>业务事实与履约方案</h2></div><FileSearch size={19}/></div>
        {loading && <p className="ops-muted"><LoaderCircle size={14}/> 正在读取业务数据…</p>}
        {detail && <>
          <div className="ops-fact"><small>订单</small><strong>{detail.order_id}</strong><span>{detail.sku_id} × {detail.quantity} · {detail.order_status}</span></div>
          <div className="ops-fact"><small>事件状态</small><strong>{statusNames[detail.state] || detail.state}</strong><span>必须满足程序校验才能继续执行</span></div>
          <div className="ops-stock"><div><Warehouse size={16}/> 仓库可用库存</div>
            {detail.inventory?.map(item => <div key={item.warehouse_id}><span>{item.warehouse_id}</span><strong>{item.available}</strong><small>现货 {item.on_hand} / 已占用 {item.reserved}</small></div>)}
          </div>
        </>}

        {quote && <div className="ops-options"><div><Truck size={16}/> 多仓履约评估 <small>模拟策略 · 不代表真实运费或 ETA</small></div>
          {quote.options.map(option => <div className={'ops-option '+(option.eligible?'eligible':'blocked')} key={option.warehouse_id}>
            <div><strong>{option.warehouse_id}</strong><span>{option.eligible?'可执行':'受阻'}</span></div>
            <small>{option.eligible?`模拟处理 ${option.simulated_handling_hours}h · 成本点 ${option.simulated_cost_points}`:option.blockers.join(', ')}</small>
          </div>)}
          <p>建议仓库：<strong>{quote.recommended_warehouse_id || '无；需人工干预'}</strong>。报价不是授权，执行前会重新校验。</p>
          {decisionPreview && <div className="ops-decision-preview">
            <strong>决策评估 · {decisionPreview.policy_version}</strong>
            <p>依据：可行性 → 模拟延误分钟 → 模拟成本点 → 仓库编号。自动推荐不等于操作授权。</p>
            <label htmlFor="ops-warehouse-select">待审批方案选择</label>
            <select id="ops-warehouse-select" value={selectedWarehouse}
              onChange={e => { setSelectedWarehouse(e.target.value); setSelectionReason('') }}>
              <option value="">采用程序推荐：{quote.recommended_warehouse_id || '无可行方案'}</option>
              {quote.options.filter(option => option.eligible && option.warehouse_id !== quote.recommended_warehouse_id)
                .map(option => <option value={option.warehouse_id} key={option.warehouse_id}>
                  改选 {option.warehouse_id}（成本点 {option.simulated_cost_points}）
                </option>)}
            </select>
            {selectedWarehouse && selectedWarehouse !== quote.recommended_warehouse_id && <>
              <label htmlFor="ops-selection-reason">人工改选理由（10—300 字符，写入持久化决策记录）</label>
              <textarea id="ops-selection-reason" maxLength={300} value={selectionReason}
                onChange={e => setSelectionReason(e.target.value)}
                placeholder="说明为何采用仍满足约束的备选仓库" />
            </>}
            <small>{decisionPreview.limitations}</small>
          </div>}
        </div>}
        <div className="ops-actions">
          <button disabled={busy||!detail} onClick={() => void investigate()}><FileSearch size={15}/> 调用真实模型调查</button>
          <button disabled={busy||!detail} onClick={() => void mutate('/api/ops/agent/tasks', {
            incident_id: selectedId,
            ...(investigationId ? {investigation_id: investigationId} : {}),
            ...(selectedWarehouse && selectedWarehouse !== quote?.recommended_warehouse_id
              ? {warehouse_id: selectedWarehouse, selection_reason: selectionReason.trim()} : {}),
          })}><ArrowRight size={15}/> 创建待审批方案</button>
        </div>
        <p className="ops-muted">模型调查需要已配置的 API 且可能产生费用；创建方案只使用确定性工具，无模型费用。</p>
        {!!agentTrace.length && <div className="ops-trace">{agentTrace.map((line,index)=><p key={index}>{line}</p>)}</div>}
        {investigationId && <p className="ops-muted">已保存调查档案：{investigationId}。创建方案时将绑定该档案；若业务状态变化则要求重新调查。</p>}
        {investigation && <div className="ops-analysis"><h3>模型调查摘要（未经独立文字核验）</h3><MarkdownView text={investigation}/></div>}
        {investigationId && <div className="ops-actions">
          <button disabled={busy || !!modelPlan} onClick={() => void requestModelPlan()}>
            <FileSearch size={15}/> 请求模型制定结构化处置建议（可能产生额外费用）
          </button>
        </div>}
        {modelPlan && <div className="ops-model-plan">
          <strong>模型处置建议 · 等待人工判断</strong>
          <p>模型建议：{modelPlan.plan.next_action === 'escalate_human' ? '升级人工处理' : `选择 ${modelPlan.plan.warehouse_id}`}</p>
          <p>程序排序第一：{modelPlan.plan.policy_recommended_warehouse_id || '无可执行仓库'}</p>
          <p>{modelPlan.plan.rationale}</p>
          {modelPlan.plan.risks.length > 0 && <ul>{modelPlan.plan.risks.map((risk,i) => <li key={i}>{risk}</li>)}</ul>}
          <small>{modelPlan.plan.limitations}</small>
          <small>建议指纹：{modelPlan.plan.plan_sha256}</small>
          {modelPlan.plan.model_reused_without_new_call && <p className="ops-muted">
            已免费重新校验当前报价；沿用原模型文字，未再次请求模型。仍须人工审核和独立审批。
          </p>}
          {modelPlan.plan.next_action === 'reserve_inventory' && !modelPlan.adopted_task_id &&
            <button type="button" disabled={busy || !token.trim()} onClick={() => void refreshExpiredModelPlan()}>
              <RefreshCw size={14}/> 免费刷新过期方案（不调用模型）
            </button>}
          {modelPlan.plan.next_action === 'reserve_inventory' && !modelPlan.adopted_task_id && <>
            {modelPlan.plan.warehouse_id !== modelPlan.plan.policy_recommended_warehouse_id && <>
              <label htmlFor="ops-model-reason">人工采纳不同于程序推荐的仓库，必须写明理由</label>
              <textarea id="ops-model-reason" value={selectionReason} maxLength={300}
                onChange={e => setSelectionReason(e.target.value)}
                placeholder="说明人工采纳该备选仓库的理由（10—300 字符）" />
            </>}
            <button disabled={busy || (modelPlan.plan.warehouse_id !== modelPlan.plan.policy_recommended_warehouse_id && selectionReason.trim().length < 10)}
              onClick={() => void acceptModelPlan()}>
              人工确认建议，创建待审批任务（不会执行）
            </button>
          </>}
          {modelPlan.adopted_task_id && <p>已关联任务：{modelPlan.adopted_task_id}；仍须单独审批。</p>}
          {modelPlan.plan.next_action === 'escalate_human' && <p>无自动预留任务；请使用现有人工处置渠道。</p>}
        </div>}
      </section>
      <section id="ops-case-execution" className="ops-surface ops-execute">
        <div className="ops-panel-head"><div><span>EXECUTION / HUMAN CONTROL</span><h2>任务进展与授权</h2></div><ShieldCheck size={19}/></div>
        {activeTask ? <>
          <div className="ops-task-id">{activeTask.task_id}</div>
          <div className={'ops-task-state '+activeTask.status}>{statusNames[activeTask.status] || activeTask.status}</div>
          <div className="ops-fact"><small>受控操作</small><strong>{activeTask.proposal.action}</strong><span>目标仓库：{activeTask.warehouse_id || '无'}</span></div>
          <p className="ops-scope">{activeTask.proposal.scope}</p>
          {activeTask.proposal.decision && <div className="ops-decision-record">
            <strong>持久化执行决策</strong>
            <p>程序推荐：{activeTask.proposal.decision.recommended_warehouse_id || '无'}；
              本次选择：{activeTask.proposal.decision.selected_warehouse_id || '人工处理'}</p>
            <p>来源：{activeTask.proposal.decision.selection_source === 'operator_override'
              ? '人工改选（仍需审批）' : '确定性规则推荐'} · {activeTask.proposal.decision.rationale}</p>
            {activeTask.proposal.decision.operator_reason && <p>改选原因：{activeTask.proposal.decision.operator_reason}</p>}
            <small>决策 SHA-256：{activeTask.proposal.decision.decision_sha256}</small>
            <small>证据 SHA-256：{activeTask.proposal.decision.evidence_sha256}</small>
          </div>}
          {activeTask.proposal.model_plan_id && <div className="ops-model-plan">
            <strong>关联的模型建议（非审批凭据）</strong>
            <small>计划编号：{activeTask.proposal.model_plan_id}</small>
            {linkedModelPlan?.plan_id === activeTask.proposal.model_plan_id && <>
              <p>模型选择：{linkedModelPlan.plan.warehouse_id || '人工升级'}</p>
              <p>建议理由：{linkedModelPlan.plan.rationale}</p>
              <small>计划指纹：{linkedModelPlan.plan.plan_sha256}</small>
            </>}
            <small>人工确认、审批和条件执行均独立于模型建议。</small>
          </div>}
          {activeTask.proposal.investigation_id && <div className="ops-evidence-reference">
            <strong>关联调查证据档案</strong>
            <span>{activeTask.proposal.investigation_id}</span>
            <small>决策证据 SHA-256：{activeTask.proposal.evidence_sha256} · 模型文字未经独立核验；执行仍由程序重新检查</small>
            {dossier?.investigation_id === activeTask.proposal.investigation_id && <details>
              <summary>查看持久化事实、检测信号与模型调查状态</summary>
              <p>关联订单：{dossier.evidence.incident.order_id} · 状态：{dossier.evidence.incident.order_status} · 业务事件：{dossier.evidence.incident.state}</p>
              <p>库存快照：{dossier.evidence.inventory.map(v => `${v.warehouse_id} 可用 ${v.available}`).join('；')}</p>
              <p>检测信号：{dossier.evidence.signals.length ? dossier.evidence.signals.map(v => `${v.rule_code} (${v.status})`).join('；') : '当前无关联信号'}</p>
              <p>策略版本：{dossier.evidence.quote.policy_version} · 推荐仓库：{dossier.evidence.quote.recommended_warehouse_id || '需人工处理'}</p>
              <p>模型调查：{dossier.model_status === 'completed' ? '已完成（文字未经程序逐条核验）' : dossier.model_status === 'not_run' ? '未调用模型，纯程序证据档案' : '未完成'}</p>
              {dossier.model_summary && <p className="ops-dossier-narrative">{dossier.model_summary}</p>}
            </details>}
          </div>}
          {playbook && <div className="ops-playbook" aria-live="polite">
            <div><Activity size={15}/><strong>自动编排 · 当前人工关口</strong></div>
            <span>{playbook.gate === 'ready' ? '可推进' :
              playbook.gate === 'reservation_approval' ? '需要预留审批' :
              playbook.gate === 'fulfillment_approval' ? '需要独立履约审批' :
              playbook.gate === 'delivery_attestation' ? '需要人工确认演示签收' :
              playbook.gate === 'explicit_recovery' ? '须人工决定是否恢复' :
              playbook.gate === 'completed' ? '已完成核验' : '请人工检查'}</span>
            <p>{playbook.reason}</p>
            {playbook.can_continue && <button disabled={busy} onClick={() => void mutate(
              `/api/ops/agent/tasks/${activeTask.task_id}/playbook/advance`)}>
              推进已授权步骤，直至下一人工关口 <ArrowRight size={14}/>
            </button>}
            <small>不会绕过审批、模拟签收确认或中断恢复；不调用付费模型。</small>
          </div>}
          <div className="ops-actions vertical">
            {activeTask.status === 'pending_approval' && <>
              <button disabled={busy} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/review`, {approved:true})}>批准指定操作</button>
              <button disabled={busy} className="subtle" onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/review`, {approved:false})}>拒绝本次方案</button>
            </>}
            {activeTask.status === 'approved' && <button disabled={busy} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/execute`)}>执行并独立核验 <ArrowRight size={15}/></button>}
            {activeTask.status === 'executing' && <button disabled={busy} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/recover`)}>恢复中断的执行 <RefreshCw size={15}/></button>}
          </div>
          {activeTask.result && <div className="ops-result"><CheckCircle2 size={16}/><strong>业务状态核验通过</strong><p>订单：{activeTask.result.actual_order_status} · 仓库：{activeTask.result.warehouse_id}</p><small>这里只证明 A4 的库存预留成功；后续履约及事件关闭须由下方独立执行链核验。</small></div>}
          {activeTask.failure_code && <p className="ops-message error">{activeTask.failure_code} · 需要重新调查并创建新方案</p>}
          {activeTask.status === 'succeeded' && <section className="ops-fulfillment">
            <div className="ops-fulfillment-header"><Truck size={16}/><strong>履约执行链</strong><span>独立授权 · 可中断恢复</span></div>
            <p>库存预留已经核验。发货与签收不是预留授权的一部分；本页仅操作合成 OMS / 物流数据库。</p>
            {!activeTask.fulfillment && <div className="ops-actions vertical">
              <button disabled={busy} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/fulfillment/prepare`)}>
                生成履约执行方案（不会立即发货）</button>
            </div>}
            {activeTask.fulfillment && <>
              <div className={'ops-task-state '+activeTask.fulfillment.status}>
                {statusNames[activeTask.fulfillment.status] || activeTask.fulfillment.status}
              </div>
              <div className="ops-steps">{fulfillmentSteps.map((step,index) => {
                const workflow = activeTask.fulfillment!
                const completed = index < workflow.next_step
                const current = index === workflow.next_step && workflow.status !== 'completed'
                return <div key={step} className={completed?'done':current?'current':''}>
                  <span>{completed ? '✓' : index + 1}</span><strong>{step}</strong>
                </div>
              })}</div>
              {activeTask.fulfillment.status === 'pending_approval' && <div className="ops-actions vertical">
                <button disabled={busy} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/fulfillment/review`, {approved: true})}>
                  批准合成环境的后续履约步骤</button>
                <button className="subtle" disabled={busy} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/fulfillment/review`, {approved: false})}>
                  拒绝发货与物流操作</button>
              </div>}
              {activeTask.fulfillment.status === 'approved' && <div className="ops-actions vertical">
                <button disabled={busy} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/fulfillment/advance`, {confirm_synthetic_delivery: false})}>
                  {activeTask.fulfillment.next_step === 0 ? '执行模拟发货并核验' :
                   activeTask.fulfillment.next_step === 1 ? '推进为模拟运输中' :
                   activeTask.fulfillment.next_step === 3 ? '独立核验并关闭事件' : '继续当前步骤'}
                </button>
              </div>}
              {activeTask.fulfillment.status === 'awaiting_delivery_confirmation' && <div className="ops-delivery">
                <label><input type="checkbox" checked={deliveryAttested} onChange={e=>setDeliveryAttested(e.target.checked)}/>
                  我确认这是合成业务环境中的演示签收事件，不代表真实承运商已完成交付。</label>
                <button disabled={busy || !deliveryAttested} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/fulfillment/advance`, {confirm_synthetic_delivery: true})}>
                  记录演示签收并核验</button>
              </div>}
              {activeTask.fulfillment.status === 'executing' && <div className="ops-actions vertical">
                <button disabled={busy} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/fulfillment/recover`)}>
                  使用原幂等键恢复中断步骤 <RefreshCw size={14}/></button>
              </div>}
              {activeTask.fulfillment.status === 'completed' && <div className="ops-result">
                <CheckCircle2 size={16}/><strong>履约和事件闭环已独立核验</strong>
                <p>合成订单已签收，运单已签收，关联事件已关闭；步骤审计保存在数据库。</p>
                <small>所有物流事件为模拟数据，不代表真实履约。</small>
              </div>}
              {activeTask.fulfillment.status === 'blocked' && <p className="ops-message error">
                步骤被业务规则阻断：{activeTask.fulfillment.failure_code}。系统不会虚报成功，请人工接管。</p>}
              {activeTask.fulfillment.status === 'handoff' && <p className="ops-muted">已移交人工处理：{activeTask.fulfillment.handoff_reason}。已发货库存不能自动回滚。</p>}
              {['pending_approval','approved','awaiting_delivery_confirmation','blocked'].includes(activeTask.fulfillment.status) && <div className="ops-handoff">
                <label>转人工原因</label>
                <input value={handoffReason} maxLength={300} placeholder="说明业务原因（至少五个字符）" onChange={e=>setHandoffReason(e.target.value)}/>
                {activeTask.fulfillment.next_step === 0 && <label className="ops-check"><input type="checkbox" checked={releaseReservation} onChange={e=>setReleaseReservation(e.target.checked)}/>
                  同意释放尚未发货的库存预留（操作不可用于已发货订单）</label>}
                <button disabled={busy || handoffReason.trim().length < 5} onClick={() => void mutate(`/api/ops/agent/tasks/${activeTask.task_id}/fulfillment/handoff`, {
                  reason: handoffReason.trim(), release_reservation: activeTask.fulfillment!.next_step === 0 && releaseReservation,
                })}>停止自动推进并转交人工</button>
              </div>}
            </>}
          </section>}
          <div className="ops-journal"><span>持久化任务时间线</span>
            {activeTask.events?.map(e => <div key={e.event_id}><b>{e.kind}</b><small>{e.created_at.slice(0,19).replace('T',' ')}</small></div>)}
          </div>
        </> : <div className="ops-empty"><ClipboardCheck size={26}/><strong>当前事件尚无执行任务</strong><p>核对左侧业务事实与仓库方案后，可以创建待审批任务；审批前不会产生库存写入。</p><a href="#ops-case-investigation">前往调查与方案 <ArrowRight size={14}/></a></div>}
      </section>
        </div>}
        {caseTab === 'timeline' && <div id="ops-case-timeline-view" className="ops-case-audit" role="tabpanel" aria-label="处置档案">        {detail && <section className="ops-case-history" aria-label="事件关联执行时间线">
          <div className="ops-case-history-head">
            <div><strong>事件处置档案</strong><small>检测信号 · 实际业务写入 · 持久化任务日志</small></div>
            <button type="button" disabled={caseHistoryLoading} onClick={() => setCaseRevision(v => v + 1)}>
              <RefreshCw size={13}/> 刷新档案
            </button>
          </div>
          {caseHistoryLoading && <p className="ops-muted">正在合并只读审计记录…</p>}
          {caseHistoryError && <p role="alert" className="ops-message error">档案读取失败：{caseHistoryError}</p>}
          {caseHistory && <>
            <div className="ops-case-status">
              <span>订单 {statusNames[caseHistory.order_status] || caseHistory.order_status}</span>
              <span>物流 {caseHistory.shipment_status || '尚无运单'}</span>
              <span>事件 {statusNames[caseHistory.incident_state] || caseHistory.incident_state}</span>
            </div>
            <div className="ops-case-counts">
              <small>检测 {caseHistory.source_counts.detection || 0}</small>
              <small>业务审计 {caseHistory.source_counts.business_audit || 0}</small>
              <small>任务记录 {caseHistory.source_counts.task_event || 0}</small>
            </div>
            <div className="ops-case-peek" aria-label="最近事件记录">
              {caseHistory.items.slice(0, 3).map(event => <div key={event.id} className="ops-case-peek-item">
                <span className={'ops-case-origin '+event.source}>{event.source === 'detection' ? '检测信号' : event.source === 'business_audit' ? '业务操作' : '任务进展'}</span>
                <div><strong>{event.description}</strong><time>{event.created_at}</time></div>
              </div>)}
              {!caseHistory.items.length && <p className="ops-muted">此事件尚无可追溯的执行历史，可以先调查业务事实。</p>}
            </div>
            <details>
              <summary>查看全部跨系统记录（{caseHistory.items.length} 条）</summary>
              {caseHistory.items.length === 0 ? <p className="ops-muted">尚无执行记录。此事件还未产生业务审计或任务日志。</p> :
                <ol className="ops-case-events">{caseHistory.items.map(event => <li key={event.id}>
                  <div><span className={'ops-case-origin '+event.source}>{event.source === 'detection' ? '检测' : event.source === 'business_audit' ? '已记录业务命令' : '任务流程'}</span>
                    <time>{event.created_at}</time></div>
                  <p>{event.description}</p>
                  {event.task_id && <button type="button" onClick={() => {
                    setActiveTask(null); setActiveTaskId(event.task_id!); setCaseTab('work'); setError('')
                  }}>打开关联任务</button>}
                </li>)}</ol>}
              {caseHistory.has_more && <p className="ops-muted">显示最近 100 条记录；更早记录仍保存在数据库中。</p>}
              <small className="ops-case-disclaimer">此视图汇合两个独立数据库的只读记录，不保证跨库原子快照；日志中的角色标签不代表已验证的个人身份。</small>
            </details>
          </>}
        </section>}</div>}
      </section>
    </div>
    <p className="ops-footnote">合成运营数据 · 独立业务状态 · 本地授权不等于生产身份认证</p>
  </div>
}
