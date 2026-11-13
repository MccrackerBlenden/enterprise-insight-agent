import test from 'node:test'
import assert from 'node:assert/strict'
import { taskForIncident, initialIncidentContext } from '../src/operations/contextSelection.ts'

const incidents = [{incident_id:'INC-1001'}, {incident_id:'INC-1003'}]
const queue = [
  {incident_id:'INC-1001', task_id:'TASK-A', bucket:'approval'},
  {incident_id:'INC-1003', task_id:null, bucket:'new'},
  {incident_id:'INC-1003', task_id:'TASK-C', bucket:'recovery'},
]
const tasks = [{incident_id:'INC-1001', task_id:'TASK-A'}, {incident_id:'INC-1003', task_id:'TASK-C'}]

test('URL-linked incident never inherits a different incident task', () => {
  assert.deepEqual(initialIncidentContext(incidents, queue, tasks, 'INC-1003'), {incidentId:'INC-1003', taskId:'TASK-C'})
  assert.equal(taskForIncident('INC-1003', queue, tasks, 'TASK-A'), 'TASK-C')
})
test('choosing an incident row finds its matching task and preserves an explicitly selected matching task', () => {
  assert.equal(taskForIncident('INC-1003', queue, tasks), 'TASK-C')
  assert.equal(taskForIncident('INC-1001', queue, tasks, 'TASK-A'), 'TASK-A')
})
test('bad/deleted URL falls back without leaking another task', () => {
  assert.deepEqual(initialIncidentContext(incidents, queue, tasks, 'INC-404'), {incidentId:'INC-1001', taskId:'TASK-A'})
  assert.deepEqual(initialIncidentContext([], [], [], 'INC-1001'), {incidentId:'', taskId:''})
})

test('archived tasks are not silently promoted into current approvals', () => {
  const rows = [
    {incident_id:'INC-2001', task_id:null, bucket:'new'},
    {incident_id:'INC-2001', task_id:'OLD-REJECTED', bucket:'archive'},
  ]
  const records = [{incident_id:'INC-2001', task_id:'OLD-REJECTED'}]
  assert.equal(taskForIncident('INC-2001', rows, records), '')
  assert.equal(taskForIncident('INC-2001', rows, records, 'OLD-REJECTED'), 'OLD-REJECTED')
})
