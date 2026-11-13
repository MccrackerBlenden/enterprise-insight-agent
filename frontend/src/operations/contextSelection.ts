/** Choose a task that belongs to the selected incident; never leak another event's approval context. */
export type ContextQueueRow = { incident_id: string; task_id: string | null; kind?: string; bucket?: string }
export type ContextTaskRow = { incident_id: string; task_id: string }
export type ContextIncidentRow = { incident_id: string }

export function taskForIncident(
  incidentId: string, queue: readonly ContextQueueRow[], tasks: readonly ContextTaskRow[], preferred?: string | null,
): string {
  const matchingQueue = queue.filter(item => item.incident_id === incidentId && item.task_id)
  if (preferred && (matchingQueue.some(row => row.task_id === preferred) ||
    tasks.some(task => task.incident_id === incidentId && task.task_id === preferred))) return preferred
  // An archived task is not an active approval just because it belongs to this incident.
  const current = matchingQueue.find(row => row.bucket !== 'archive')
  if (current?.task_id) return current.task_id
  // When triage has an incident entry but only archived tasks, the incident is task-free.
  if (queue.some(row => row.incident_id === incidentId)) return ''
  return tasks.find(task => task.incident_id === incidentId)?.task_id || ''
}

export function initialIncidentContext(
  incidents: readonly ContextIncidentRow[], queue: readonly ContextQueueRow[],
  tasks: readonly ContextTaskRow[], requestedId?: string | null,
): { incidentId: string; taskId: string } {
  const validRequested = requestedId && incidents.some(item => item.incident_id === requestedId) ? requestedId : ''
  const incidentId = validRequested || queue.find(item => item.bucket !== 'archive')?.incident_id ||
    queue[0]?.incident_id || incidents[0]?.incident_id || ''
  return { incidentId, taskId: incidentId ? taskForIncident(incidentId, queue, tasks) : '' }
}
