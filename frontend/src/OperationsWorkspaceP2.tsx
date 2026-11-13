import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, ArrowRight, CheckCircle2, CircleAlert, ClipboardCheck,
  FileSearch, LoaderCircle, RefreshCw, ShieldCheck, Truck, Warehouse,
  ChevronDown, ArrowUpRight, Clock3, BookOpen } from 'lucide-react'
import MarkdownView from './MarkdownView'
import { OpsShell, type OpsArea } from './operations/OpsShell'
import { OPS_STAGES, resolveOpsStage, type OpsStage } from './operations/stageResolver'
import { OpsFirstUseGuide, type GuideStep } from './operations/OpsFirstUseGuide'
import { getStageHelp, guideWasCompleted, rememberGuideDismissal } from './operations/guideContent'
import { resolveOperatorAction, type OperatorAction } from './operations/operatorAction'
import { initialIncidentContext, taskForIncident } from './operations/contextSelection'
import { describeOpsError } from './operations/opsErrors'
import { stockBrief, planView, mutationReceipt, auditHeadline, type Receipt } from './operations/workflowBrief'
import { createRequestEpoch, canRestoreRefreshSelection } from './operations/requestEpoch'
import { describeCaseVerdict, caseCoverageNote } from './operations/caseVerdict'
import './operations.css'
import './operations/p2c3-responsive.css'

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
type SeededDemoResponse = { synthetic: true; version: string; scenarios: Array<{
  kind: string; title: string; incident_id: string; order_id: string; task_id: string;
  incident_state: string; order_status: string; task_status: string; fulfillment_status: string | null
}> }
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
type Playbook = { synthetic: boolean; task_id: string; gate: string; can_continue: boolean; next_action: string | null;
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
    const body = await response.json().catch(() => ({})) as { detail?: unknown }
    throw new Error(describeOpsError(response.status, body.detail,
      init?.method?.toUpperCase() === 'POST' ? 'write' : 'read'))
  }
  return response.json() as Promise<T>
}

const statusNames: Record<string, string> = {
  open: '待调查', investigating: '调查中', escalated: '人工升级', resolved: '已核验解决',
  pending: '待处理', reserved: '库存已预留', shipped: '已模拟发货',
  in_transit: '模拟运输中', delivered: '已模拟签收',
  blocked: '无可执行方案', pending_approval: '等待批准', approved: '已批准',
  rejected: '已拒绝', executing: '执行中 / 可恢复', succeeded: '预留已核验',
  stale: '计划已过期', failed: '执行失败',
  awaiting_delivery_confirmation: '待确认演示签收',
  completed: '履约已核验闭环', handoff: '已转交人工',
}
const fulfillmentSteps = ['创建模拟运单', '更新模拟运输状态', '人工确认演示签收', '独立核验并关闭事件']

export default function OperationsWorkspace() {
  const [seededDemo, setSeededDemo] = useState<SeededDemoResponse | null>(null)
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
  const [journalSearch, setJournalSearch] = useState('')
  const [guideStep, setGuideStep] = useState<GuideStep | null>(() => {
    let storage: Storage | null = null
    try { storage = window.localStorage } catch { /* browser may deny access */ }
    return guideWasCompleted(storage) ? null : 0
  })
  const [area, setArea] = useState<OpsArea>(() => {
    const params = new URLSearchParams(window.location.search)
    const requested = params.get('opsArea')
    return requested === 'overview' || requested === 'incident' || requested === 'journal'
      ? requested : params.has('opsIncident') ? 'incident' : 'overview'
  })
  const [manualStage, setManualStage] = useState<OpsStage | null>(() => {
    const stage = new URLSearchParams(window.location.search).get('opsStage')
    return OPS_STAGES.find(item => item.id === stage)?.id || null
  })
  const initialSelection = useRef(false)
  const refreshRequestId = useRef(0)
  const selectionRevision = useRef(0)
  const taskReadEpoch = useRef(createRequestEpoch())
  const playbookReadEpoch = useRef(createRequestEpoch())
  const [lastRefreshed, setLastRefreshed] = useState('')
  const [caseHistory, setCaseHistory] = useState<CaseHistory | null>(null)
  const [caseHistoryLoading, setCaseHistoryLoading] = useState(false)
  const [caseHistoryError, setCaseHistoryError] = useState('')
  const [caseRevision, setCaseRevision] = useState(0)
  const [activeTaskId, setActiveTaskId] = useState<string>('')
  const [activeTask, setActiveTask] = useState<Task | null>(null)
  const [taskLoading, setTaskLoading] = useState(false)
  const [playbook, setPlaybook] = useState<Playbook | null>(null)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const mutateLock = useRef(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [lastReceipt, setLastReceipt] = useState<(Receipt & { incidentId: string; taskId: string }) | null>(null)
  const [investigation, setInvestigation] = useState('')
  const [investigationId, setInvestigationId] = useState('')
  const [dossier, setDossier] = useState<InvestigationDossier | null>(null)
  const [agentTrace, setAgentTrace] = useState<string[]>([])
  const [modelPlan, setModelPlan] = useState<ModelPlan | null>(null)
  const [linkedModelPlan, setLinkedModelPlan] = useState<ModelPlan | null>(null)
  const [deliveryAttested, setDeliveryAttested] = useState(false)
  const [handoffReason, setHandoffReason] = useState('')
  const [releaseReservation, setReleaseReservation] = useState(false)
  const [escalationReason, setEscalationReason] = useState('')
  const escalationAttempt = useRef<{ incidentId: string; reason: string; key: string } | null>(null)
  const demoAccessRef = useRef<HTMLDetailsElement | null>(null)
  const stageTabsRef = useRef<HTMLDivElement | null>(null)

  const openDemoAccess = () => {
    if (!demoAccessRef.current) return
    demoAccessRef.current.open = true
    demoAccessRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    demoAccessRef.current.querySelector('input')?.focus({ preventScroll: true })
  }

  const refresh = useCallback(async (nextId?: string) => {
    // Ignore responses from an older refresh that completes after a newer request.
    const requestId = ++refreshRequestId.current
    const selectionWhenStarted = selectionRevision.current
    // Freeze stale authorization/quote views while the business snapshot is being re-read.
    taskReadEpoch.current.invalidate()
    playbookReadEpoch.current.invalidate()
    setActiveTask(null)
    setPlaybook(null)
    setTaskLoading(false)
    setQuote(null)
    setDecisionPreview(null)
    const snapshot = await jsonRequest<TriageSnapshot>('/api/ops/agent/triage')
    if (requestId !== refreshRequestId.current) return
    setIncidents(snapshot.incidents)
    setTasks(snapshot.tasks)
    setQueue(snapshot.items)
    setQueueSummary(snapshot.summary)
    setLastRefreshed(new Date().toLocaleTimeString('zh-CN', {hour12: false}))
    setCaseRevision(v => v + 1)
    if (!initialSelection.current) {
      const linkedId = new URLSearchParams(window.location.search).get('opsIncident')
      const initial = initialIncidentContext(snapshot.incidents, snapshot.items, snapshot.tasks, nextId || linkedId)
      setSelectedId(initial.incidentId)
      setActiveTaskId(initial.taskId)
      initialSelection.current = true
    } else if (nextId && canRestoreRefreshSelection(selectionWhenStarted, selectionRevision.current, nextId)) {
      setSelectedId(nextId)
    }
  }, [])

  useEffect(() => { void refresh().catch(e => setError(String(e))) }, [refresh])
  // The normal app returns 404 and never exposes a demo tour or local paths.
  // Fetch read-only source IDs after a refresh; never mint demo records in UI.
  useEffect(() => {
    let cancelled = false
    void fetch('/api/ops/demo/scenarios').then(async response => {
      if (!response.ok) { if (!cancelled) setSeededDemo(null); return }
      const payload = await response.json() as SeededDemoResponse
      if (!cancelled) setSeededDemo(payload.synthetic && payload.version === 'P2-C.2' ? payload : null)
    }).catch(() => { if (!cancelled) setSeededDemo(null) })
    return () => { cancelled = true }
  }, [lastRefreshed])


  useEffect(() => {
    if (!selectedId) { setDetail(null); setQuote(null); setDecisionPreview(null); return }
    let cancelled = false
    setDetail(null); setQuote(null); setDecisionPreview(null)
    setLoading(true)
    // The actual order state decides whether a new quote may be requested.
    // Previously the UI requested quotes for shipped orders and rendered
    // order_not_pending as a current, red failure card.
    void jsonRequest<Incident>(`/api/ops/incidents/${encodeURIComponent(selectedId)}`)
      .then(async current => {
        if (cancelled) return
        setDetail(current)
        if (current.order_status !== 'pending' || ['resolved', 'escalated'].includes(current.state)) return
        const [quoteResult, decisionResult] = await Promise.allSettled([
          jsonRequest<Quote>(`/api/ops/orders/${encodeURIComponent(current.order_id)}/fulfillment-options`),
          jsonRequest<Decision>(`/api/ops/agent/incidents/${encodeURIComponent(selectedId)}/decision-preview`),
        ])
        if (cancelled) return
        if (quoteResult.status === 'fulfilled') {
          if (!quoteResult.value.options.some(option => option.blockers.includes('order_not_pending'))) {
            setQuote(quoteResult.value)
          } else {
            setError('订单状态在获取报价时已发生变化。旧报价不会作为当前可执行方案，请刷新业务状态。')
          }
        }
        if (decisionResult.status === 'fulfilled') setDecisionPreview(decisionResult.value)
        if (quoteResult.status === 'rejected') setError('当前报价不可读取，请重新核对订单状态。')
      })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [selectedId, incidents])
  // A refreshed quote must not inherit an operator's selection from an older snapshot.
  useEffect(() => {
    setSelectedWarehouse('')
    setSelectionReason('')
  }, [quote?.snapshot_fingerprint])

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
    const ticket = taskReadEpoch.current.begin()
    if (!activeTaskId) { setActiveTask(null); setTaskLoading(false); return }
    let cancelled = false
    // A refresh must not retain an old task response as the current approval decision.
    setActiveTask(null)
    setTaskLoading(true)
    void jsonRequest<Task>(`/api/ops/agent/tasks/${encodeURIComponent(activeTaskId)}`)
      .then(result => {
        if (!cancelled && taskReadEpoch.current.isCurrent(ticket) && result.task_id === activeTaskId) setActiveTask(result)
      })
      .catch(e => { if (!cancelled && taskReadEpoch.current.isCurrent(ticket)) setError(String(e)) })
      .finally(() => { if (!cancelled && taskReadEpoch.current.isCurrent(ticket)) setTaskLoading(false) })
    return () => { cancelled = true; taskReadEpoch.current.invalidate() }
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
    const ticket = playbookReadEpoch.current.begin()
    if (!activeTaskId) { setPlaybook(null); return }
    let cancelled = false
    setPlaybook(null)
    void jsonRequest<Playbook>(`/api/ops/agent/tasks/${encodeURIComponent(activeTaskId)}/playbook`)
      .then(result => {
        if (!cancelled && playbookReadEpoch.current.isCurrent(ticket) && result.task_id === activeTaskId) setPlaybook(result)
      })
      .catch(e => { if (!cancelled && playbookReadEpoch.current.isCurrent(ticket)) setError(String(e)) })
    return () => { cancelled = true; playbookReadEpoch.current.invalidate() }
  }, [activeTaskId, activeTask?.status, activeTask?.fulfillment?.status,
      activeTask?.fulfillment?.next_step, tasks])

  async function mutate(path: string, body: Record<string, unknown> = {}) {
    if (mutateLock.current) return null
    if (!token.trim()) { setError('请先输入本地演示写入令牌。令牌仅保存在当前页面内存中。'); return null }
    mutateLock.current = true
    // Invalidate any older GET before the POST. A late read must not roll the UI back.
    taskReadEpoch.current.invalidate()
    playbookReadEpoch.current.invalidate()
    setTaskLoading(false)
    setPlaybook(null)
    setBusy(true); setError(''); setMessage(''); setLastReceipt(null)
    try {
      const result = await jsonRequest<Task>(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ops-Demo-Token': token.trim() },
        body: JSON.stringify(body),
      })
      setActiveTaskId(result.task_id)
      if (result.incident_id) setSelectedId(result.incident_id)
      setActiveTask(result)
      setPlaybook(null) // wait for the server's new gate; never offer an old approval button
      setDeliveryAttested(false)
      // Receipt describes only what this POST returned; it never asserts event closure.
      setLastReceipt({ ...mutationReceipt(result), incidentId: result.incident_id, taskId: result.task_id })
      try {
        await refresh()
      } catch {
        // Never label a successful write as a failed write: blind retry could duplicate the business action.
        setError('操作已被服务端接受，但后续状态同步失败。请先核对任务记录后再继续，切勿重复提交。')
      }
      return result
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); return null }
    finally { mutateLock.current = false; setBusy(false) }
  }

  // An existing Ops command records a human escalation for an active incident.
  // It does not create a task, replenish inventory, or claim that a human accepted it.
  async function escalateIncident(reason: string) {
    if (!selectedId || !detail || !['open', 'investigating'].includes(detail.state)) {
      setError('当前事件已不处于可升级状态，请刷新事件记录。'); return
    }
    if (!token.trim()) { setError('升级事件需要本地演示写入令牌。'); openDemoAccess(); return }
    if (reason.trim().length < 5 || reason.trim().length > 300) {
      setError('人工升级原因需要 5—300 个字符。'); return
    }
    if (!window.confirm(`将事件 ${selectedId} 标记为“人工升级”并写入审计。该事件将停止自动预留，后续须人工重新评估；不代表有人接单或问题已经解决。\n\n原因：${reason.trim()}\n\n确定提交吗？`)) return
    if (mutateLock.current) return
    const previous = escalationAttempt.current
    const key = previous?.incidentId === selectedId && previous.reason === reason.trim()
      ? previous.key : crypto.randomUUID()
    escalationAttempt.current = { incidentId: selectedId, reason: reason.trim(), key }
    mutateLock.current = true
    setBusy(true); setError(''); setMessage('')
    try {
      await jsonRequest<{ incident_id: string; state: string }>('/api/ops/commands/escalate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Ops-Demo-Token': token.trim(), 'X-Idempotency-Key': key },
        body: JSON.stringify({ incident_id: selectedId, reason: reason.trim() }),
      })
      escalationAttempt.current = null
      setMessage('已记录“人工升级”并写入业务审计。实际库存协调仍须由人工完成；此事件尚未解决。')
      try { await refresh(selectedId) }
      catch { setError('人工升级已被服务端接受，但刷新档案失败。请先核对最新记录，切勿重复升级。') }
      chooseStage('review')
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { mutateLock.current = false; setBusy(false) }
  }

  async function investigate() {
    if (!selectedId || !token.trim()) { setError('模型调查需要本地写入令牌，以防止未经授权的付费模型调用。'); return }
    setBusy(true); setError(''); setAgentTrace([]); setInvestigation(''); setInvestigationId(''); setModelPlan(null)
    try {
      const res = await fetch('/api/ops/agent/investigate/stream', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ops-Demo-Token': token.trim() },
        body: JSON.stringify({ incident_id: selectedId }),
      })
      if (!res.ok || !res.body) throw new Error(describeOpsError(res.status, null, 'model'))
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
    if (task) {
      setModelPlan({ ...modelPlan, adopted_task_id: task.task_id })
      chooseStage('execution')
    }
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
  const navigateArea = (next: OpsArea) => {
    setArea(next)
    const url = new URL(window.location.href)
    url.searchParams.set('opsArea', next)
    window.history.replaceState(null, '', url)
  }
  const chooseQueueItem = (item: QueueEntry) => {
    // Invalidate a pending refresh's old navigation target immediately on user choice.
    selectionRevision.current += 1
    taskReadEpoch.current.invalidate()
    playbookReadEpoch.current.invalidate()
    setSelectedId(item.incident_id)
    navigateArea('incident')
    const url = new URL(window.location.href)
    url.searchParams.set('opsIncident', item.incident_id)
    url.searchParams.delete('opsStage')
    window.history.replaceState(null, '', url)
    setManualStage(null)
    setDetail(null); setQuote(null); setDecisionPreview(null)
    setDeliveryAttested(false); setReleaseReservation(false); setHandoffReason('')
    setPlaybook(null); setActiveTask(null) // never leave a previous task's approval controls on screen
    setActiveTaskId(taskForIncident(item.incident_id, queue, tasks, item.task_id))
    setInvestigation(''); setInvestigationId(''); setModelPlan(null)
    setAgentTrace([]); setError(''); setSelectedWarehouse(''); setSelectionReason('')
    setLastReceipt(null); setMessage('')
    setEscalationReason(''); escalationAttempt.current = null
    // If a learner picks a real event from the queue, start at its read-only facts.
    if (guideStep === 1) {
      setManualStage('summary')
      url.searchParams.set('opsStage', 'summary')
      window.history.replaceState(null, '', url)
      setGuideStep(2)
    }
  }
  const openQueueCount = queue.filter(item => item.bucket !== 'archive').length
  const closedCount = incidents.filter(item => item.state === 'resolved').length
  const contextTask = activeTask?.task_id === activeTaskId && activeTask?.incident_id === selectedId
    ? activeTask : null
  const contextPlaybook = playbook?.task_id === activeTaskId && activeTaskId ? playbook : null
  const resolved = resolveOpsStage(detail, contextTask, contextPlaybook, {
    investigationId, modelPlanId: modelPlan?.adopted_task_id ? undefined : modelPlan?.plan_id,
  })
  const activeStage = manualStage || resolved.stage
  const operatorAction = resolveOperatorAction(contextTask, contextPlaybook, resolved.action)
  const canPlan = !!detail && resolved.canPlan
  const stockOverview = detail ? stockBrief(detail.quantity, detail.inventory) : null
  const decisionState = planView(canPlan, Boolean(contextTask || activeTaskId), quote)
  const quoteBlocked = canPlan && !!quote && !quote.options.some(option => option.eligible)
  const stockShortage = quoteBlocked && quote!.options.length > 0 && quote!.options.every(option => option.blockers.includes('insufficient_stock'))
  const bestAvailable = quoteBlocked ? Math.max(0, ...quote!.options.map(option => option.available)) : 0
  const missingQuantity = quoteBlocked && detail ? Math.max(0, detail.quantity - bestAvailable) : 0
  const defaultEscalationReason = detail
    ? `订单 ${detail.order_id} 需求 ${detail.quantity} 件，当前候选仓库均不符合单仓履约条件，请人工协调后续方案。`
    : ''
  const actualEscalationReason = escalationReason.trim() || defaultEscalationReason
  const chooseStage = (stage: OpsStage) => {
    setManualStage(stage)
    const url = new URL(window.location.href)
    url.searchParams.set('opsStage', stage)
    window.history.replaceState(null, '', url)
    window.requestAnimationFrame(() => stageTabsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
  }
  const openCurrentStage = () => {
    setManualStage(null)
    const url = new URL(window.location.href)
    url.searchParams.delete('opsStage')
    window.history.replaceState(null, '', url)
  }
  const stageHelp = getStageHelp(activeStage, {
    canPlan, quoteBlocked: decisionState === 'blocked', hasTask: Boolean(contextTask),
    incidentState: detail?.state || '',
    viewingOtherStage: Boolean(manualStage && manualStage !== resolved.stage),
  })
  const guideExample = queue.find(item => item.kind === 'incident' && item.bucket === 'new') ||
    queue.find(item => item.bucket !== 'archive') || queue[0]
  const dismissGuide = () => {
    let storage: Storage | null = null
    try { storage = window.localStorage } catch { /* unavailable in private browser */ }
    rememberGuideDismissal(storage)
    setGuideStep(null)
  }
  const showGuide = () => {
    setGuideStep(0)
    navigateArea('overview')
    window.scrollTo({top: 0, behavior: 'smooth'})
  }
  const openGuideExample = () => {
    if (!guideExample) return
    chooseQueueItem(guideExample)
    // The initial learning step is always read-only, even for an in-flight task.
    setManualStage('summary')
    const url = new URL(window.location.href)
    url.searchParams.set('opsStage', 'summary')
    window.history.replaceState(null, '', url)
    setGuideStep(2)
    window.scrollTo({top: 0, behavior: 'smooth'})
  }
  const finishGuide = () => {
    dismissGuide()
    // This only opens a view: no writes, approvals, or billable model calls.
    if (detail) chooseStage(decisionState === 'blocked' ? 'decision' : resolved.stage)
  }
  const journalItems = queue.filter(row => row.kind === 'task' &&
    [row.incident_id, row.order_id, row.task_id || '', row.label, row.reason]
      .some(value => value.toLowerCase().includes(journalSearch.trim().toLowerCase())))
  const nextQueueItem = queue.find(row => row.bucket !== 'archive' && row.incident_id !== selectedId)
  const currentHistory = caseHistory?.incident_id === selectedId && caseHistory.order_id === detail?.order_id ? caseHistory : null
  const caseVerdict = currentHistory ? describeCaseVerdict(currentHistory, detail) : null
  const openHistoryTask = (taskId: string) => {
    if (busy || !currentHistory || !(
      currentHistory.linked_tasks.some(item => item.task_id === taskId) ||
      currentHistory.items.some(item => item.task_id === taskId)
    )) return
    taskReadEpoch.current.invalidate()
    playbookReadEpoch.current.invalidate()
    setActiveTask(null); setPlaybook(null); setActiveTaskId(taskId)
    setDeliveryAttested(false); setReleaseReservation(false); setHandoffReason('')
    setLastReceipt(null); setError('')
    chooseStage('execution')
  }

  const executeOperatorAction = (action: OperatorAction, approved = true) => {
    if (!contextTask || !contextPlaybook || busy) return
    if (!token.trim()) { setError('请先配置本地演示写入令牌。'); openDemoAccess(); return }
    if (action.requiresAttestation && !deliveryAttested) {
      setError('必须先人工勾选模拟签收确认。'); return
    }
    const id = encodeURIComponent(contextTask.task_id)
    const paths: Record<OperatorAction['kind'], string> = {
      reserve_review: `/api/ops/agent/tasks/${id}/review`,
      fulfillment_review: `/api/ops/agent/tasks/${id}/fulfillment/review`,
      delivery_attestation: `/api/ops/agent/tasks/${id}/fulfillment/advance`,
      reservation_recovery: `/api/ops/agent/tasks/${id}/recover`,
      fulfillment_recovery: `/api/ops/agent/tasks/${id}/fulfillment/recover`,
      advance: `/api/ops/agent/tasks/${id}/playbook/advance`,
    }
    const confirmation = approved ? action.buttonLabel : '拒绝本次申请'
    if (!window.confirm(`事件：${detail?.incident_id || selectedId}\n订单：${detail?.order_id || '待复核'}\n目标仓库：${contextTask.warehouse_id || '无'}\n操作：${confirmation}\n授权范围：${contextTask.proposal.scope}\n\n所有写入仍由服务端重检。确认操作吗？`)) return
    const body = action.kind === 'reserve_review' || action.kind === 'fulfillment_review'
      ? { approved }
      : action.kind === 'delivery_attestation' ? { confirm_synthetic_delivery: true } : {}
    void mutate(paths[action.kind], body)
  }

  return <div className="ops-ws ops-p2">
    <section className="ops-heading">
      <div><span className="eyebrow">异常响应 · 人工可控</span>
        <h1>运营处置中心</h1>
        <p>将业务异常、调查证据、人工授权和履约执行集中在同一个事件工作空间。</p>
      </div>
      <div className="ops-heading-tools">
        <span className="ops-environment"><span className="ops-indicator"/> 合成业务环境 · 手动同步</span>
        <button type="button" className="ops-p2-help-launch" disabled={busy} onClick={showGuide}>
          <BookOpen size={15}/> 新手指导
        </button>
        <button className="ops-refresh" disabled={busy} onClick={() => void refresh().catch(e => setError(String(e)))}>
          <RefreshCw size={16}/> 刷新业务状态
        </button>
      </div>
    </section>
    <OpsShell area={area} onChange={next => { if (!busy) navigateArea(next) }} />
    {guideStep !== null && <OpsFirstUseGuide
      step={guideStep} openCount={openQueueCount} approvalCount={inReview}
      hasExample={Boolean(guideExample)} selectedSummary={detail?.summary || null}
      selectedOrderId={detail?.order_id || null}
      currentStageLabel={detail ? (OPS_STAGES.find(s => s.id === resolved.stage)?.label || '查看事件') : '正在确认当前业务状态'}
      currentAction={detail ? (decisionState === 'blocked' ? '没有可执行仓库，查看缺口后决定是否人工升级。' : resolved.nextLabel) : '请先确认事件详情。'}
      onStart={() => {setGuideStep(1); navigateArea('overview')}}
      onOpenExample={openGuideExample} onShowCurrent={finishGuide} onDismiss={dismissGuide}
    />}
    {area === 'overview' && <section className="ops-overview-strip" aria-label="运营关键状态">
      <div className="ops-overview-primary"><span>当前待办</span><strong>{openQueueCount}</strong><small>需关注的事件或任务</small></div>
      <div><span>等待人工确认</span><strong>{inReview}</strong><small>审批与签收关口</small></div>
      <div><span>活跃事件</span><strong>{active.length}</strong><small>来自 Ops 业务状态</small></div>
      <div><span>已核验事件</span><strong>{closedCount}</strong><small>不包含未完成的模拟处理</small></div>
    </section>}
    <details ref={demoAccessRef} className="ops-demo-access">
      <summary><span><ShieldCheck size={15}/> 本地演示权限 <strong>{token.trim() ? '已提供操作凭证' : '当前为只读模式'}</strong></span>
        <span>仅需执行业务操作时配置 <ChevronDown size={15}/></span></summary>
      <div className="ops-token"><div><strong>本地演示操作凭证</strong><small>令牌仅保留在当前浏览器页面内存中；不会进入历史记录。此方式不等于生产环境的身份认证。</small></div>
        <input aria-label="本地演示写入令牌" type="password" value={token} onChange={e => setToken(e.target.value)} placeholder="输入本地演示令牌" autoComplete="off"/>
      </div>
    </details>
    {error && <div role="alert" className="ops-message error"><CircleAlert size={16}/>{error}
      {canPlan && modelPlan && /stale|过期|expired/i.test(error) &&
        <button type="button" onClick={() => {navigateArea('incident'); chooseStage('decision')}}>前往方案核验</button>}
      <button type="button" disabled={busy} onClick={() => void refresh(selectedId).then(() => setError('')).catch(e => setError(String(e)))}>
        <RefreshCw size={14}/> 重新同步业务事实
      </button>
    </div>}
    {message && <div role="status" className="ops-message success"><CheckCircle2 size={16}/>{message}</div>}
    {lastReceipt && selectedId === lastReceipt.incidentId && <section className="ops-p2-operation-receipt" role="status" aria-label="最近操作结果">
      <div><CheckCircle2 size={18}/><strong>{lastReceipt.title}</strong><p>{lastReceipt.detail}</p>
        <small>来源：服务端任务响应 · {lastReceipt.taskId}。当前界面仍应刷新后端关口核对。</small>
      </div>
      <button type="button" onClick={() => { navigateArea('incident'); chooseStage(lastReceipt.nextStage); setLastReceipt(null) }}>
        {lastReceipt.button} <ArrowRight size={15}/>
      </button>
      <button className="ops-p2-receipt-dismiss" type="button" onClick={() => setLastReceipt(null)} aria-label="关闭操作结果提示">×</button>
    </section>}
    {area === 'overview' && seededDemo && <section className="ops-p2-seeded-demo" aria-label="独立合成演示案例">
      <div className="ops-p2-seeded-demo-head"><div><span>独立演示环境</span><h2>三条模拟业务状态演示</h2>
        <p>这些任务由独立演示数据库中的业务服务生成。仅查看不会写入；恢复或审批仍需本地授权与服务端校验。</p></div>
        <small>独立实例 · 全部合成</small></div>
      <div className="ops-p2-seeded-demo-items">{seededDemo.scenarios.map(example => {
        const entry = queue.find(item => item.task_id === example.task_id) ||
          queue.find(item => item.incident_id === example.incident_id)
        return <div key={example.kind} className="ops-p2-seeded-demo-item">
          <div><strong>{example.title}</strong><p>{example.incident_id} · {example.order_id}</p>
            <small>事件：{statusNames[example.incident_state] || example.incident_state} · 订单：{statusNames[example.order_status] || example.order_status} · 任务：{statusNames[example.fulfillment_status || example.task_status] || example.fulfillment_status || example.task_status}</small></div>
          {entry ? <button type="button" disabled={busy} onClick={() => {
            chooseQueueItem(entry)
            chooseStage('summary')
            window.scrollTo({top: 0, behavior: 'smooth'})
          }}>查看案例 <ArrowRight size={15}/></button> : <span>当前队列未返回对应事件，请刷新业务状态</span>}
        </div>
      })}</div>
    </section>}
    {area === 'overview' && <div className="ops-p2-overview">
      <section className="ops-surface ops-incidents">
        <div className="ops-panel-head"><div><span>待办队列</span><h2>优先处理</h2></div><small>{visibleQueue.length} / {queue.length} 项</small></div>
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
            type="button" disabled={busy} className={'ops-incident ops-queue-item '+
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
    </div>}
    {area === 'incident' && <section className="ops-case-workspace" aria-label="当前事件处置工作空间">
        {detail ? <header className="ops-case-hero">
          <button className="ops-p2-back" type="button" onClick={() => navigateArea('overview')}>← 返回运营态势</button>
          <div className="ops-case-hero-top"><span>事件编号 · {detail.incident_id}</span>
            <span className={'ops-severity '+detail.severity}>{severityNames[detail.severity] || detail.severity}风险 · {statusNames[detail.state] || detail.state}</span></div>
          <h2>{detail.summary || '当前业务异常'}</h2>
          <div className="ops-case-context">
            <div><small>关联订单</small><strong>{detail.order_id}</strong></div>
            <div><small>业务对象</small><strong>{detail.sku_id} × {detail.quantity}</strong></div>
            <div><small>订单状态</small><strong>{statusNames[detail.order_status] || detail.order_status}</strong></div>
            <div><small>当前任务</small><strong>{activeTaskId ? (statusNames[activeTask?.fulfillment?.status || activeTask?.status || ''] || '正在读取任务') : '尚未建立'}</strong></div>
          </div>
          <div className="ops-case-next" aria-live="polite">
            <div><span>当前业务关口 · {OPS_STAGES.find(s => s.id === resolved.stage)?.label}</span>
              <strong>{decisionState === 'blocked' ? '当前无可执行仓库，建议人工核查' : resolved.nextLabel}</strong>
              <p>{decisionState === 'blocked' ? '订单不满足现有仓库履约条件，当前不会生成库存预留任务。' : resolved.explanation}</p></div>
            {decisionState === 'blocked' && activeStage !== 'decision'
              ? <button type="button" onClick={() => chooseStage('decision')}>查看缺口与处置建议 <ArrowUpRight size={15}/></button>
              : manualStage && manualStage !== resolved.stage
              ? <button type="button" onClick={openCurrentStage}>返回当前阶段 <ArrowUpRight size={15}/></button>
              : <span className="ops-p2-gate-note">{activeTaskId && !contextPlaybook ? '正在读取服务端关口' : '写入操作仍由后端审批与校验'}</span>}
          </div>
        </header> : <div className="ops-case-hero ops-case-placeholder">
          <Clock3 size={21}/><strong>{loading ? '正在载入事件信息…' : '选择一项事件，查看完整处置上下文'}</strong>
          <p>从左侧队列进入事件；所有操作均依据当前业务状态，不会自动修改数据。</p>
        </div>}
        <div ref={stageTabsRef} className="ops-case-tabs ops-p2-stage-tabs" role="tablist" aria-label="事件五阶段">
          {OPS_STAGES.map(item => <button key={item.id} type="button" role="tab"
            aria-selected={activeStage === item.id} aria-controls="ops-p2-active-panel"
            disabled={busy} className={activeStage === item.id ? 'active' : ''} onClick={() => chooseStage(item.id)}>
            {item.label}{resolved.stage === item.id && <span>当前</span>}
          </button>)}
        </div>
        {detail && <details className="ops-p2-stage-hint ops-p2-stage-hint-compact">
          <summary>本阶段操作提示 · {stageHelp.title}</summary>
          <div><p>{stageHelp.instruction}</p><small>{stageHelp.boundary}</small></div>
        </details>}
        <div id="ops-p2-active-panel" role="tabpanel" className="ops-p2-panel">
          {activeStage === 'summary' && <section className="ops-surface ops-p2-summary">
            <div className="ops-panel-head"><div><span>当前业务事实</span><h2>事件概览与下一步</h2></div></div>
            {detail ? <>
              <p className="ops-p2-lead">{detail.summary}</p>
              <div className="ops-p2-facts">
                <div><small>订单</small><strong>{detail.order_id}</strong><span>{statusNames[detail.order_status] || detail.order_status}</span></div>
                <div><small>业务对象</small><strong>{detail.sku_id} × {detail.quantity}</strong><span>合成演示数据</span></div>
                <div><small>事件</small><strong>{statusNames[detail.state] || detail.state}</strong><span>风险：{severityNames[detail.severity] || detail.severity}</span></div>
              </div>
              <div className="ops-p2-recommend"><strong>{decisionState === 'blocked' ? '没有符合条件的仓库，自动履约暂时受阻' : resolved.nextLabel}</strong>
                <p>{decisionState === 'blocked' ? `订单需要 ${detail.quantity} 件，当前没有可单独满足该订单的仓库。先核对缺口，再决定是否升级人工处理。` : resolved.explanation}</p>
                <button type="button" onClick={() => chooseStage(decisionState === 'blocked' ? 'decision' : resolved.stage === 'summary' ? 'investigation' : resolved.stage)}>
                  {decisionState === 'blocked' ? '查看库存缺口与处理建议' : resolved.stage === 'summary' ? '核对调查证据' : '前往当前业务关口'} <ArrowRight size={15}/>
                </button>
              </div>
            </> : <p className="ops-muted">{loading ? '正在读取服务端业务事实…' : '请先从运营态势选择事件。'}</p>}
          </section>}
      {activeStage === 'investigation' && <section id="ops-case-investigation" className="ops-surface ops-investigate">
        <div className="ops-panel-head"><div><span>事实调查</span><h2>事实与调查</h2></div></div>
        {loading && <p className="ops-muted"><LoaderCircle size={14}/> 正在读取业务数据…</p>}
        {detail && stockOverview && <>
          <div className={'ops-p2-evidence-brief '+stockOverview.kind}>
            <span>程序事实 · 当前订单与库存快照</span>
            <strong>{stockOverview.title}</strong>
            <div className="ops-p2-evidence-numbers">
              <span>订单需求 <b>{detail.quantity} 件</b></span>
              <span>最高单仓可用 <b>{stockOverview.maxAvailable === null ? '待读取' : `${stockOverview.maxAvailable} 件`}</b></span>
              <span>订单状态 <b>{statusNames[detail.order_status] || detail.order_status}</b></span>
            </div>
            <p>{stockOverview.detail}</p>
          </div>
          <details className="ops-p2-tech"><summary>查看原始仓库库存与占用（{detail.inventory?.length || 0} 个仓库）</summary>
            <div className="ops-stock"><div><Warehouse size={16}/> 实际库存明细</div>
              {detail.inventory?.map(item => <div key={item.warehouse_id}><span>{item.warehouse_id}</span><strong>可用 {item.available} 件</strong><small>现货 {item.on_hand} / 已占用 {item.reserved}</small></div>)}
              {!detail.inventory?.length && <p className="ops-muted">尚未取得可用仓库的完整库存明细，不能据此判定可执行。</p>}
            </div>
          </details>
        </>}


        {canPlan ? <>
          <div className="ops-p2-step-action">
            <small>下一步 · 无需调用模型</small>
            <strong>{quoteBlocked ? '库存不足，查看人工处理方案' : '查看可行仓库与履约方案'}</strong>
            <p>已使用系统规则核对订单及库存。模型调查是可选的付费补充，不是继续流程的必经步骤。</p>
            <button type="button" onClick={() => chooseStage('decision')}>
              {quoteBlocked ? '查看缺口与人工处置' : '查看仓库方案'} <ArrowRight size={15}/>
            </button>
          </div>
          <details className="ops-p2-tech"><summary>高级：让模型辅助调查（可能产生 API 费用）</summary>
            <p>仅补充分析，不会绕过库存和人工审批约束。</p>
            <div className="ops-actions"><button disabled={busy || !token.trim()}
              onClick={() => { if (window.confirm('将调用真实模型调查，可能产生 API 费用。确定继续吗？')) void investigate() }}>
                <FileSearch size={15}/> 调用真实模型调查
              </button></div>
            {!token.trim() && <button type="button" className="ops-p2-subtle-action" onClick={openDemoAccess}>先配置本地演示令牌</button>}
          </details>
        </> : <div className="ops-p2-handoff"><strong>当前订单不允许重新创建库存预留方案</strong>
          <p>调查信息可以继续查看，但实际处理应依据最新任务或人工升级记录。</p>
          <button type="button" onClick={() => chooseStage(resolved.stage)}>返回当前业务阶段 <ArrowRight size={14}/></button>
        </div>}
        {!!agentTrace.length && <div className="ops-trace">{agentTrace.map((line,index)=><p key={index}>{line}</p>)}</div>}
        {investigationId && <p className="ops-muted">已保存调查档案：{investigationId}。创建方案时将绑定该档案；若业务状态变化则要求重新调查。</p>}
        {investigation && <div className="ops-analysis"><h3>模型调查摘要（未经独立文字核验）</h3><MarkdownView text={investigation}/></div>}

        {dossier && <details className="ops-p2-tech"><summary>查看已持久化调查证据</summary>
          <p>订单：{dossier.evidence.incident.order_id}；状态：{dossier.evidence.incident.order_status}</p>
          <p>库存快照：{dossier.evidence.inventory.map(item => `${item.warehouse_id}: ${item.available}`).join('，')}</p>
          <p>模型结论与确定性事实相互独立。</p>
        </details>}
      </section>}
      {activeStage === 'decision' && <section className="ops-surface ops-investigate">
        <div className="ops-panel-head"><div><span>方案与授权</span><h2>方案与人工选择</h2></div></div>
        {!canPlan && <div className="ops-p2-historical"><strong>{detail?.state === 'escalated' ? '事件已记录人工升级' : '仓库方案已转为历史记录'}</strong>
          <p>{detail?.state === 'escalated' ? '人工协调尚不代表已完成履约。此事件不可再自动创建库存预留任务，请在“复盘”查看升级记录。' : `订单状态是 ${detail?.order_status || '未知'}；不再请求新报价。请在“执行”核查正在发生的业务操作，或在“复盘”查看当时决策。`}</p></div>}
        {canPlan && <>
        {decisionState === 'existing_task' && <div className="ops-p2-handoff">
          <strong>{contextTask ? '当前事件已有执行任务' : '正在核对已关联的任务上下文'}</strong>
          <p>先核对原任务的审批和执行状态；若任务不存在，请返回待办队列重新同步。不能盲目重复创建。</p>
          <button type="button" onClick={() => chooseStage('execution')}>前往现有任务 <ArrowRight size={14}/></button>
        </div>}
        {decisionState === 'not_loaded' && <div className="ops-p2-handoff">
          <strong>尚未取得有效履约方案</strong>
          <p>这不是“无可行仓库”的结论；需要先读取最新订单和服务端报价，不会调用付费模型。</p>
          <button type="button" disabled={busy || loading} onClick={() => void refresh(selectedId).catch(e => setError(String(e)))}><RefreshCw size={14}/> 免费重新核对业务状态</button>
        </div>}
        {decisionState === 'blocked' && quote && detail && <section className="ops-p2-blocked" aria-label="无可执行方案时的人工处理路径">
          <div className="ops-p2-blocked-head"><CircleAlert size={22}/><div>
            <small>需要人工判断 · 自动履约在此停止</small>
            <h3>{stockShortage ? '没有仓库能独立满足本次订单' : '没有符合当前约束的履约仓库'}</h3>
            <p>这不是程序故障，也不能通过模型建议绕过库存约束。</p>
          </div></div>
          <div className="ops-p2-capacity-summary">
            <div><small>订单需求</small><strong>{detail.quantity}<em>件</em></strong></div>
            <div><small>最高单仓可用</small><strong>{quote.options.length ? bestAvailable : '—'}<em>{quote.options.length ? '件' : '无候选'}</em></strong></div>
            <div><small>{stockShortage ? '单仓最少缺口' : '履约可行仓库'}</small>
              <strong>{stockShortage ? missingQuantity : 0}<em>{stockShortage ? '件' : '个'}</em></strong></div>
          </div>
          <div className="ops-p2-capacity" aria-label="候选仓库可用库存与订单需求对比">
            {quote.options.map(option => <div className="ops-p2-capacity-row" key={option.warehouse_id}>
              <span>{option.warehouse_id}</span>
              <div className="ops-p2-capacity-track" role="img" aria-label={`${option.warehouse_id} 可用 ${option.available} 件，订单需要 ${detail.quantity} 件`}>
                <span style={{width: `${Math.min(100,Math.max(0,option.available / Math.max(1, detail.quantity) * 100))}%`}} />
              </div>
              <strong>{option.available} / {detail.quantity}</strong>
              <small>{option.blockers.includes('insufficient_stock') ? '库存不足' : '受履约约束阻断'}</small>
            </div>)}
          </div>
          <div className="ops-p2-blocked-next">
            <strong>现在应该怎么做？</strong>
            <p>核对缺口后，可将事件正式标记为人工升级，由运营人员线下协调补货、调拨或订单调整。升级后会停止该事件的自动预留，<b>不代表已有人接单，也不代表问题解决</b>。</p>
            {['open','investigating'].includes(detail.state) ? <>
              <details className="ops-p2-tech"><summary>修改人工升级原因（可选）</summary>
                <textarea value={escalationReason} maxLength={300} onChange={e => setEscalationReason(e.target.value)}
                  placeholder={defaultEscalationReason} aria-label="人工升级原因" />
              </details>
              <div className="ops-p2-blocked-actions">
                {!token.trim() && <label className="ops-p2-token-inline">本地演示写入令牌（不是模型密钥）
                  <input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)}
                    placeholder="填入 backend/.env 中配置的 EIA_OPS_WRITE_TOKEN" />
                </label>}
                <button type="button" disabled={busy || !token.trim()} onClick={() => void escalateIncident(actualEscalationReason)}>
                  <ArrowRight size={15}/> 记录人工升级
                </button>
                <button type="button" className="subtle" disabled={busy}
                  onClick={() => void refresh(selectedId).catch(e => setError(String(e)))}>
                  <RefreshCw size={15}/> 库存变化后重新检查
                </button>
              </div>
            </> : <p>事件已不处于可升级状态；请核对当前事件状态及审计记录。</p>}
          </div>
          <details className="ops-p2-tech"><summary>查看阻断原因与策略细节</summary>
            {quote.options.map(option => <p key={option.warehouse_id}>{option.warehouse_id}：{option.blockers.join('、') || '无阻断代码'}；当前可用 {option.available} 件</p>)}
            <p>策略版本：{quote.policy_version}。单仓履约与其他经营决策均以服务端规则为准。</p>
          </details>
        </section>}
        {decisionState === 'ready' && quote && <div className="ops-options ops-p2-plan-focus">
          <div><Truck size={16}/> 服务端履约方案 <small>合成规则，不是实际物流报价</small></div>
          <div className="ops-p2-plan-recommend">
            <span>程序推荐仓库</span>
            <strong>{quote.recommended_warehouse_id || '尚无推荐'}</strong>
            <p>{quote.options.filter(option => option.eligible).length} / {quote.options.length} 个仓库满足当前服务端候选约束。创建任务只表示提交审批，不会立即扣减库存或发货。</p>
          </div>
          <details className="ops-p2-tech"><summary>展开查看所有仓库的比较与模拟参数（{quote.options.length} 个）</summary>
            {quote.options.map(option => <div className={'ops-option '+(option.eligible?'eligible':'blocked')} key={option.warehouse_id}>
              <div><strong>{option.warehouse_id}</strong><span>{option.eligible?'当前可行':'受约束阻断'}</span></div>
              <small>{option.eligible?`模拟处理 ${option.simulated_handling_hours}h · 模拟成本点 ${option.simulated_cost_points}`:option.blockers.join(', ')}</small>
            </div>)}
          </details>
          {!quote.recommended_warehouse_id && <p className="ops-p2-plan-warning">当前候选数据未给出推荐仓库，暂不可创建审批任务。请免费刷新业务事实。</p>}
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
        {decisionState === 'ready' && quote?.recommended_warehouse_id && !token.trim() && <div className="ops-p2-auth-hint">
          <ShieldCheck size={16}/><label className="ops-p2-token-inline">创建审批任务前需填入本地演示写入令牌（不是模型密钥）
            <input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)}
              placeholder="backend/.env 中的 EIA_OPS_WRITE_TOKEN" />
          </label>
        </div>}
        {decisionState === 'ready' && modelPlan && !modelPlan.adopted_task_id && <div className="ops-p2-handoff">
          <strong>已有模型建议，当前优先处理这份建议</strong>
          <p>可在下方审核并采纳，也可明确不采用这份模型建议、改走程序规则方案；已保存的模型建议仍保留在历史记录中。</p>
          <button type="button" disabled={busy} onClick={() => { setModelPlan(null); setSelectionReason('') }}>
            不采用模型建议，改用程序方案 <ArrowRight size={14}/>
          </button>
        </div>}
        {decisionState === 'ready' && <div className="ops-actions">
          <button disabled={busy||!token.trim()||!canPlan || !!activeTaskId || !!modelPlan || !quote?.recommended_warehouse_id ||
            (!!selectedWarehouse && selectedWarehouse !== quote?.recommended_warehouse_id && selectionReason.trim().length < 10)}
            onClick={() => { void (async () => {
              const created = await mutate('/api/ops/agent/tasks', {
                incident_id: selectedId,
                ...(investigationId ? {investigation_id: investigationId} : {}),
                ...(selectedWarehouse && selectedWarehouse !== quote?.recommended_warehouse_id
                  ? {warehouse_id: selectedWarehouse, selection_reason: selectionReason.trim()} : {}),
              })
              if (created) chooseStage('execution')
            })() }}><ArrowRight size={15}/> 创建待审批方案并进入审批</button>
        </div>}
        </>}
        {decisionState === 'ready' && investigationId && <details className="ops-p2-tech">
          <summary>可选高级能力：请模型生成结构化处置建议（可能产生额外费用）</summary>
          <p>普通方案与审批完全不需要模型；如主动调用模型，还需人工采纳与后续独立审批。</p>
          <button className="ops-p2-tech-cta" disabled={busy || !!modelPlan} onClick={() => { if (window.confirm('将请求模型生成结构化处置建议，可能产生额外费用。确定继续吗？')) void requestModelPlan() }}>
            <FileSearch size={15}/> 请求模型建议（可能计费）
          </button>
        </details>}

        {modelPlan && <div className="ops-model-plan">
          <strong>{canPlan ? '模型处置建议 · 等待人工判断' : '历史模型建议 · 只读'}</strong>
          <p>模型建议：{modelPlan.plan.next_action === 'escalate_human' ? '升级人工处理' : `选择 ${modelPlan.plan.warehouse_id}`}</p>
          <p>程序排序第一：{modelPlan.plan.policy_recommended_warehouse_id || '无可执行仓库'}</p>
          <p>{modelPlan.plan.rationale}</p>
          {modelPlan.plan.risks.length > 0 && <ul>{modelPlan.plan.risks.map((risk,i) => <li key={i}>{risk}</li>)}</ul>}
          <small>{modelPlan.plan.limitations}</small>
          <details className="ops-p2-tech"><summary>查看模型建议指纹</summary><small>{modelPlan.plan.plan_sha256}</small></details>
          {modelPlan.plan.model_reused_without_new_call && <p className="ops-muted">
            已免费重新校验当前报价；沿用原模型文字，未再次请求模型。仍须人工审核和独立审批。
          </p>}
          {canPlan && modelPlan.plan.next_action === 'reserve_inventory' && !modelPlan.adopted_task_id &&
            <button type="button" className="ops-p2-subtle-action" disabled={busy || !token.trim()} onClick={() => void refreshExpiredModelPlan()}>
              <RefreshCw size={14}/> 免费刷新过期方案（不调用模型）
            </button>}
          {canPlan && modelPlan.plan.next_action === 'reserve_inventory' && !modelPlan.adopted_task_id && <>
            {modelPlan.plan.warehouse_id !== modelPlan.plan.policy_recommended_warehouse_id && <>
              <label htmlFor="ops-model-reason">人工采纳不同于程序推荐的仓库，必须写明理由</label>
              <textarea id="ops-model-reason" value={selectionReason} maxLength={300}
                onChange={e => setSelectionReason(e.target.value)}
                placeholder="说明人工采纳该备选仓库的理由（10—300 字符）" />
            </>}
            <button disabled={busy || !!activeTaskId || (modelPlan.plan.warehouse_id !== modelPlan.plan.policy_recommended_warehouse_id && selectionReason.trim().length < 10)}
              onClick={() => void acceptModelPlan()}>
              人工确认建议，创建待审批任务（不会执行）
            </button>
          </>}
          {modelPlan.adopted_task_id && <p>已关联任务：{modelPlan.adopted_task_id}；仍须单独审批。</p>}
          {modelPlan.plan.next_action === 'escalate_human' && <p>无自动预留任务；请使用现有人工处置渠道。</p>}
        </div>}

        {activeTask?.proposal.decision && <details className="ops-p2-tech"><summary>查看已保存的仓库选择与决策证据</summary>
          <p>原选择：{activeTask.proposal.decision.selected_warehouse_id || '人工处理'}；来源：{activeTask.proposal.decision.selection_source}</p>
          <p>{activeTask.proposal.decision.rationale}</p>
        </details>}
      </section>}
      {activeStage === 'execution' && <section id="ops-case-execution" className="ops-surface ops-execute">
        <div className="ops-panel-head"><div><span>执行与复核</span><h2>任务进展与授权</h2></div></div>
        {contextTask ? <>
          <section className="ops-p2-action-focus" aria-label="当前可处理的业务关口">
            <span>当前应处理 · 服务端业务关口</span>
            <h3>{operatorAction?.title || (contextPlaybook ? resolved.nextLabel : '正在核对服务端关口')}</h3>
            <p>{operatorAction?.explanation || (contextPlaybook ? resolved.explanation : '关口返回前仅允许查看信息，不提供写入操作。')}</p>
            <div className="ops-p2-action-target">
              <span>订单 <strong>{detail?.order_id || '核对中'}</strong></span>
              <span>仓库 <strong>{contextTask.warehouse_id || '尚无'}</strong></span>
              <span>任务 <strong>{statusNames[contextTask.fulfillment?.status || contextTask.status] || contextTask.status}</strong></span>
            </div>
            {operatorAction && <>
              {operatorAction.requiresAttestation && <label className="ops-p2-action-attest">
                <input type="checkbox" checked={deliveryAttested} onChange={event => setDeliveryAttested(event.target.checked)} />
                我已核对：这是合成环境中的模拟签收，不代表真实承运商交付。
              </label>}
              {!token.trim() && <button type="button" className="ops-p2-auth-open" onClick={openDemoAccess}>
                <ShieldCheck size={15}/> 配置本地演示操作令牌
              </button>}
              <div className="ops-p2-action-buttons">
                <button type="button" disabled={busy || !token.trim() || (operatorAction.requiresAttestation && !deliveryAttested)}
                  onClick={() => executeOperatorAction(operatorAction)}>
                  {busy ? '正在提交…' : operatorAction.buttonLabel} <ArrowRight size={15}/>
                </button>
                {operatorAction.allowReject && <button type="button" className="ops-p2-action-reject" disabled={busy || !token.trim()}
                  onClick={() => executeOperatorAction(operatorAction, false)}>拒绝本次申请</button>}
              </div>
            </>}
            {!operatorAction && <button type="button" className="ops-p2-auth-open" disabled={busy}
              onClick={() => void refresh(selectedId).catch(e => setError(String(e)))}>
              <RefreshCw size={15}/> 刷新并核对当前任务
            </button>}
            <small>审批、恢复与模拟签收均不能由模型自动替你确认；最终结果以服务端和审计记录为准。</small>
          </section>
          <details className="ops-p2-tech"><summary>查看任务编号、授权范围与详细技术信息</summary>
            <div className="ops-task-id">{contextTask.task_id}</div>
            <div className="ops-fact"><small>受控操作</small><strong>{contextTask.proposal.action}</strong><span>目标仓库：{contextTask.warehouse_id || '无'}</span></div>
            <p className="ops-scope">{contextTask.proposal.scope}</p>
          </details>
          {contextTask.proposal.decision && <details className="ops-p2-tech"><summary>历史仓库选择与决策指纹</summary><div className="ops-decision-record">
            <strong>持久化执行决策</strong>
            <p>程序推荐：{contextTask.proposal.decision.recommended_warehouse_id || '无'}；
              本次选择：{contextTask.proposal.decision.selected_warehouse_id || '人工处理'}</p>
            <p>来源：{contextTask.proposal.decision.selection_source === 'operator_override'
              ? '人工改选（仍需审批）' : '确定性规则推荐'} · {contextTask.proposal.decision.rationale}</p>
            {contextTask.proposal.decision.operator_reason && <p>改选原因：{contextTask.proposal.decision.operator_reason}</p>}
            <small>决策 SHA-256：{contextTask.proposal.decision.decision_sha256}</small>
            <small>证据 SHA-256：{contextTask.proposal.decision.evidence_sha256}</small>
          </div></details>}
          {contextTask.proposal.model_plan_id && <details className="ops-p2-tech"><summary>历史模型建议（非审批凭据）</summary><div className="ops-model-plan">
            <strong>关联的模型建议（非审批凭据）</strong>
            <small>计划编号：{contextTask.proposal.model_plan_id}</small>
            {linkedModelPlan?.plan_id === contextTask.proposal.model_plan_id && <>
              <p>模型选择：{linkedModelPlan.plan.warehouse_id || '人工升级'}</p>
              <p>建议理由：{linkedModelPlan.plan.rationale}</p>
              <small>计划指纹：{linkedModelPlan.plan.plan_sha256}</small>
            </>}
            <small>人工确认、审批和条件执行均独立于模型建议。</small>
          </div></details>}
          {contextTask.proposal.investigation_id && <details className="ops-p2-tech"><summary>历史调查档案与证据</summary><div className="ops-evidence-reference">
            <strong>关联调查证据档案</strong>
            <span>{contextTask.proposal.investigation_id}</span>
            <small>决策证据 SHA-256：{contextTask.proposal.evidence_sha256} · 模型文字未经独立核验；执行仍由程序重新检查</small>
            {dossier?.investigation_id === contextTask.proposal.investigation_id && <details>
              <summary>查看持久化事实、检测信号与模型调查状态</summary>
              <p>关联订单：{dossier.evidence.incident.order_id} · 状态：{dossier.evidence.incident.order_status} · 业务事件：{dossier.evidence.incident.state}</p>
              <p>库存快照：{dossier.evidence.inventory.map(v => `${v.warehouse_id} 可用 ${v.available}`).join('；')}</p>
              <p>检测信号：{dossier.evidence.signals.length ? dossier.evidence.signals.map(v => `${v.rule_code} (${v.status})`).join('；') : '当前无关联信号'}</p>
              <p>策略版本：{dossier.evidence.quote.policy_version} · 推荐仓库：{dossier.evidence.quote.recommended_warehouse_id || '需人工处理'}</p>
              <p>模型调查：{dossier.model_status === 'completed' ? '已完成（文字未经程序逐条核验）' : dossier.model_status === 'not_run' ? '未调用模型，纯程序证据档案' : '未完成'}</p>
              {dossier.model_summary && <p className="ops-dossier-narrative">{dossier.model_summary}</p>}
            </details>}
          </div></details>}
          {contextPlaybook && <div className="ops-playbook" aria-live="polite">
            <div><Activity size={15}/><strong>自动编排 · 当前人工关口</strong></div>
            <span>{contextPlaybook.gate === 'ready' ? '可推进' :
              contextPlaybook.gate === 'reservation_approval' ? '需要预留审批' :
              contextPlaybook.gate === 'fulfillment_approval' ? '需要独立履约审批' :
              contextPlaybook.gate === 'delivery_attestation' ? '需要人工确认演示签收' :
              contextPlaybook.gate === 'explicit_recovery' ? '须人工决定是否恢复' :
              contextPlaybook.gate === 'completed' ? '已完成核验' : '请人工检查'}</span>
            <p>{resolved.explanation}</p>
            <details className="ops-p2-tech"><summary>查看服务器关口原始说明</summary><p>{contextPlaybook.reason}</p></details>
            <small>不会绕过审批、模拟签收确认或中断恢复；不调用付费模型。</small>
          </div>}
          {contextTask.result && <div className="ops-result"><CheckCircle2 size={16}/><strong>业务状态核验通过</strong><p>订单：{contextTask.result.actual_order_status} · 仓库：{contextTask.result.warehouse_id}</p><small>这里只证明 A4 的库存预留成功；后续履约及事件关闭须由下方独立执行链核验。</small></div>}
          {contextTask.failure_code && <p className="ops-message error">历史任务记录：{contextTask.failure_code}。请核对当前业务事实和恢复关口，不能重复预留。</p>}
          {contextTask.status === 'succeeded' && <section className="ops-fulfillment">
            <div className="ops-fulfillment-header"><Truck size={16}/><strong>履约执行链</strong><span>独立授权 · 可中断恢复</span></div>
            <p>库存预留已经核验。发货与签收不是预留授权的一部分；本页仅操作合成 OMS / 物流数据库。</p>
            {contextTask.fulfillment && <>
              <div className={'ops-task-state '+contextTask.fulfillment.status}>
                {statusNames[contextTask.fulfillment.status] || contextTask.fulfillment.status}
              </div>
              <div className="ops-steps">{fulfillmentSteps.map((step,index) => {
                const workflow = contextTask.fulfillment!
                const completed = index < workflow.next_step
                const current = index === workflow.next_step && workflow.status !== 'completed'
                return <div key={step} className={completed?'done':current?'current':''}>
                  <span>{completed ? '✓' : index + 1}</span><strong>{step}</strong>
                </div>
              })}</div>
              {contextTask.fulfillment.status === 'completed' && <div className="ops-result">
                <CheckCircle2 size={16}/><strong>{resolved.stage === 'review' ? '履约和事件闭环已核验' : '履约任务已完成，事件状态仍待核对'}</strong>
                <p>{resolved.stage === 'review'
                  ? '订单、运单及事件状态已符合闭环条件，详细记录可在复盘中查看。'
                  : '任务显示已完成，但当前订单或事件尚未满足全部闭环条件；请刷新事实与审计，不要重复发货。'}</p>
                <small>所有物流事件均为合成数据，不代表真实履约。</small>
              </div>}
              {contextTask.fulfillment.status === 'blocked' && <p className="ops-message error">
                步骤被业务规则阻断：{contextTask.fulfillment.failure_code}。系统不会虚报成功，请人工接管。</p>}
              {contextTask.fulfillment.status === 'handoff' && <p className="ops-muted">已移交人工处理：{contextTask.fulfillment.handoff_reason}。已发货库存不能自动回滚。</p>}
              {contextPlaybook && ['pending_approval','approved','awaiting_delivery_confirmation','blocked'].includes(contextTask.fulfillment.status) && <details className="ops-p2-tech"><summary>高级操作：转交人工</summary><div className="ops-handoff">
                <label>转人工原因</label>
                <input value={handoffReason} maxLength={300} placeholder="说明业务原因（至少五个字符）" onChange={e=>setHandoffReason(e.target.value)}/>
                {contextTask.fulfillment.next_step === 0 && <label className="ops-check"><input type="checkbox" checked={releaseReservation} onChange={e=>setReleaseReservation(e.target.checked)}/>
                  同意释放尚未发货的库存预留（操作不可用于已发货订单）</label>}
                <button disabled={busy || handoffReason.trim().length < 5} onClick={() => {
                  if (window.confirm(`确认停止自动推进并转交人工？${releaseReservation ? '同时请求释放尚未发货的库存预留。' : ''}`)) {
                    void mutate(`/api/ops/agent/tasks/${contextTask.task_id}/fulfillment/handoff`, {
                      reason: handoffReason.trim(), release_reservation: contextTask.fulfillment!.next_step === 0 && releaseReservation,
                    })
                  }
                }}>停止自动推进并转交人工</button>
              </div></details>}
            </>}
          </section>}
          {!contextPlaybook && <p className="ops-muted">正在读取服务端执行关口；关口未确认前不会提供写入按钮。若一直未恢复，请使用上方“刷新业务状态”。</p>}
          <div className="ops-journal"><span>持久化任务时间线</span>
            {contextTask.events?.map(e => <div key={e.event_id}><b>{e.kind}</b><small>{e.created_at.slice(0,19).replace('T',' ')}</small></div>)}
          </div>
        </> : activeTaskId ? <div className="ops-empty" role="status"><LoaderCircle size={25}/>
          <strong>{taskLoading ? '正在读取已有任务与授权状态' : '已关联任务暂时无法确认'}</strong>
          <p>未核实原任务前不允许重新建立或执行任务。请只读刷新，避免重复写入。</p>
          {!taskLoading && <button type="button" disabled={busy} onClick={() => void refresh(selectedId).catch(e => setError(String(e)))}>
            <RefreshCw size={14}/> 重新核对服务端任务
          </button>}
        </div> : <div className="ops-empty"><ClipboardCheck size={26}/><strong>当前事件尚无执行任务</strong><p>请先核实该事件是否已有历史任务；只有待处理订单才允许创建新方案。</p><button type="button" onClick={() => chooseStage('decision')}>查看方案与历史 <ArrowRight size={14}/></button></div>}
      </section>}
        {activeStage === 'review' && <div id="ops-case-timeline-view" className="ops-case-audit" aria-label="处置档案">        {detail && <section className="ops-case-history" aria-label="事件关联执行时间线">
          <div className="ops-case-history-head">
            <div><strong>事件处置档案</strong><small>检测信号 · 实际业务写入 · 持久化任务日志</small></div>
            <button type="button" disabled={caseHistoryLoading} onClick={() => setCaseRevision(v => v + 1)}>
              <RefreshCw size={13}/> 刷新档案
            </button>
          </div>
          {caseHistoryLoading && <p className="ops-muted">正在合并只读审计记录…</p>}
          {caseHistoryError && <p role="alert" className="ops-message error">档案读取失败：{caseHistoryError}</p>}
          {currentHistory && caseVerdict && <>
            <div className={'ops-p2-review-verdict '+(caseVerdict.kind === 'closed' ? 'verified' : 'open')}>
              <small>业务记录结论 · 合成环境</small>
              <strong>{caseVerdict.title}</strong>
              <p>{caseVerdict.explanation}</p>
              <p><b>建议下一步：</b>{caseVerdict.nextStep}</p>
              <small>{caseCoverageNote(currentHistory)}。跨数据库读取并非同一事务快照。</small>
              {caseVerdict.kind !== 'closed' && <button type="button" onClick={() => {openCurrentStage(); navigateArea('incident')}}>
                返回当前业务关口 <ArrowRight size={14}/>
              </button>}
            </div>
            <div className="ops-case-status">
              <span>订单 {statusNames[currentHistory.order_status] || currentHistory.order_status}</span>
              <span>物流 {currentHistory.shipment_status || '尚无运单'}</span>
              <span>事件 {statusNames[currentHistory.incident_state] || currentHistory.incident_state}</span>
            </div>
            <div className="ops-case-counts">
              <small>检测 {currentHistory.source_counts.detection || 0}</small>
              <small>业务审计 {currentHistory.source_counts.business_audit || 0}</small>
              <small>任务记录 {currentHistory.source_counts.task_event || 0}</small>
            </div>
            <div className="ops-case-peek" aria-label="最近事件记录">
              {currentHistory.items.slice(0, 3).map(event => <div key={event.id} className="ops-case-peek-item">
                <span className={'ops-case-origin '+event.source}>{event.source === 'detection' ? '检测信号' : event.source === 'business_audit' ? '业务操作' : '任务进展'}</span>
                <div><strong>{auditHeadline(event.source, event.kind)}</strong><p>{event.description}</p><time>{event.created_at}</time></div>
              </div>)}
              {!currentHistory.items.length && <p className="ops-muted">此事件尚无可追溯的执行历史，可以先调查业务事实。</p>}
            </div>
            <details>
              <summary>查看全部跨系统记录（{currentHistory.items.length} 条）</summary>
              {currentHistory.items.length === 0 ? <p className="ops-muted">尚无执行记录。此事件还未产生业务审计或任务日志。</p> :
                <ol className="ops-case-events">{currentHistory.items.map(event => <li key={event.id}>
                  <div><span className={'ops-case-origin '+event.source}>{event.source === 'detection' ? '检测' : event.source === 'business_audit' ? '已记录业务命令' : '任务流程'}</span>
                    <time>{event.created_at}</time></div>
                  <p><strong>{auditHeadline(event.source, event.kind)}</strong> · {event.description}</p>
                  {event.task_id && <button type="button" disabled={busy} onClick={() => openHistoryTask(event.task_id!)}>打开关联任务</button>}
                </li>)}</ol>}
              {currentHistory.has_more && <p className="ops-muted">显示最近 100 条记录；更早记录仍保存在数据库中。</p>}
              <small className="ops-case-disclaimer">此视图汇合两个独立数据库的只读记录，不保证跨库原子快照；日志中的角色标签不代表已验证的个人身份。</small>
            </details>
          </>}
          {contextTask?.proposal.decision && <details className="ops-p2-tech"><summary>历史决策与技术证据</summary>
            <p>选中仓库：{contextTask.proposal.decision.selected_warehouse_id || '人工处理'}；操作理由：{contextTask.proposal.decision.operator_reason || '采用程序选择'}</p>
            <p>决策指纹：{contextTask.proposal.decision.decision_sha256}</p>
            <p>证据指纹：{contextTask.proposal.decision.evidence_sha256}</p>
          </details>}
          {(linkedModelPlan || modelPlan) && <details className="ops-p2-tech"><summary>历史模型建议（只读）</summary>
            <p>{(linkedModelPlan || modelPlan)?.plan.rationale}</p><p>{(linkedModelPlan || modelPlan)?.plan.plan_sha256}</p>
          </details>}
        </section>}
        {nextQueueItem && <div className="ops-p2-next-case">
          <div><small>下一件事</small><strong>{nextQueueItem.reason}</strong><p>返回待办队列时也可以重新选择事件；这里不会自动进行业务写入。</p></div>
          <button type="button" disabled={busy} onClick={() => chooseQueueItem(nextQueueItem)}>处理下一事件 <ArrowRight size={14}/></button>
        </div>}</div>}
        </div>
      </section>}
    {area === 'journal' && <section className="ops-surface ops-p2-journal" aria-label="任务运行记录与质量">
      <div className="ops-panel-head"><div><span>只读运行记录</span><h2>运行记录与质量</h2></div></div>
      <p>来自持久化任务队列的只读摘要。原始审批、恢复、执行和结果核验可进入对应事件查看；这里不计算未经验证的 SLO 或模型费用。</p>
      <label className="ops-p2-journal-search">检索历史任务
        <input type="search" value={journalSearch} onChange={event => setJournalSearch(event.target.value)}
          placeholder="按事件、订单、任务或原因检索" />
      </label>
      {journalItems.length ? <div className="ops-p2-journal-list">
        {journalItems.map(row => <button type="button" key={row.task_id} disabled={busy} onClick={() => { chooseQueueItem(row); chooseStage('review') }}>
          <strong>{row.incident_id} · {row.order_id}</strong>
          <span>{row.label} · {row.reason}</span>
          <small>{row.updated_at || '时间未知'} · {row.task_id}</small>
        </button>)}
      </div> : <div className="ops-queue-empty"><ClipboardCheck size={22}/><p>当前条件下没有运行记录，请调整检索或同步业务状态。</p></div>}
    </section>}
    <p className="ops-footnote">Synthetic Operations Sandbox · 实际状态来自独立 Ops SQLite · 本地演示权限不等于生产身份认证</p>
  </div>
}
