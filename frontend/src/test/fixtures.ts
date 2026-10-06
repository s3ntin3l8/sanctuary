import type { Schemas } from '../api/client'

export const caseCard: Schemas['CaseCard'] = {
  id: 'ADV-024-A',
  title: 'Weber ./. Weber',
  status: 'pre_trial',
  status_label: 'Pre-Trial',
  is_draft: false,
  pending_close: false,
  client_name: 'A. Weber',
  opposing_party: 'M. Weber',
  proceeding_name: 'AG Hamburg',
  matter_type: 'Sorgerecht',
  next_action: {
    title: 'File counter-statement',
    due_date: '2026-06-18T00:00:00Z',
    action_type: 'deadline',
  },
  exposure_eur: 18450,
  doc_count: 12,
  open_action_count: 1,
  new_docs: 2,
  days_since_activity: 3,
  is_dormant: false,
  max_significance: 'critical',
  last_activity_at: '2026-06-14T09:00:00Z',
}

export const shellView: Schemas['ShellView'] = {
  user: { id: 1, email: 'k.vogt@ra-vogt.de', display_name: 'Katharina Vogt', role: 'admin' },
  triage_count: 7,
}

export const emptyQueue: Schemas['QueueView'] = {
  counts: { executing: 0, queued: 0, failed: 0, ai_inflight: 0 },
  executing: [],
  queued: [],
  failed: [],
}

export const homeView: Schemas['HomeView'] = {
  greeting: 'Good morning',
  user_name: 'Katharina',
  now: '2026-06-17T06:40:00Z',
  today_items: [
    {
      id: 1,
      case_id: 'ADV-024-A',
      case_title: 'Weber ./. Weber',
      title: 'File counter-statement',
      description: null,
      due_date: '2026-06-18T00:00:00Z',
      action_type: 'deadline',
    },
  ],
  triage_bundles: [
    {
      id: 42,
      status: 'pending',
      received_at: '2026-06-21T09:12:00Z',
      sender_email: 'kanzlei-vogt@ra-vogt.de',
      title: 'Klageerwiderung',
      doc_count: 3,
      case_id: null,
      suggested_case_id: 'ADV-024-A',
      pipeline: { total: 3, running: 1, pending: 0, failed: 0, completed: 2 },
    },
  ],
  last_home_visit: '2026-06-16T08:00:00Z',
  delta_cases: [
    {
      case_id: 'ADV-019-C',
      case_title: 'Brandt GmbH ./. Keller',
      new_doc_count: 2,
      new_actions: 1,
      max_significance: 'significant',
      doc_titles: ['Lieferschein', 'Mahnung'],
    },
  ],
  signals: [
    {
      id: 'dormancy-1',
      kind: 'dormancy',
      severity: 'warn',
      title: 'Nachlass Hoffmann is dormant',
      detail: 'No document in 94 days.',
      action: 'check',
      link: '/cases/ADV-031-A',
    },
  ],
  draft_cases: [],
  active_cases: [caseCard],
  caught_up: false,
}
