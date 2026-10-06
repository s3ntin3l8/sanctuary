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

export const aiSettings: Schemas['AiSettingsView'] = {
  instances: [
    {
      id: 'inst_a',
      label: 'Ollama · local',
      base_url: 'http://127.0.0.1:11434',
      has_api_key: false,
      is_external: false,
      summary_model: 'qwen3.5:9b',
      embed_model: 'nomic-embed-text',
      embed_dim: 768,
      ocr_model: '',
    },
    {
      id: 'inst_b',
      label: 'LM Studio · OCR',
      base_url: 'http://127.0.0.1:1234',
      has_api_key: true,
      is_external: false,
      summary_model: '',
      embed_model: '',
      embed_dim: null,
      ocr_model: 'chandra-ocr',
    },
  ],
  roles: [
    {
      role: 'chat',
      label: 'Chat',
      hint: 'Powers briefs.',
      active_id: 'inst_a',
      model: 'qwen3.5:9b',
      embed_dim: null,
    },
    {
      role: 'embed',
      label: 'Embeddings',
      hint: 'Powers search.',
      active_id: 'inst_a',
      model: 'nomic-embed-text',
      embed_dim: 768,
    },
    {
      role: 'ocr',
      label: 'OCR',
      hint: 'Reads scans.',
      active_id: 'inst_b',
      model: 'chandra-ocr',
      embed_dim: null,
    },
  ],
  extraction_engine: 'chandra',
  worker_concurrency: 2,
  ocr_concurrency: 4,
  embed_index: { dim: 768, model: 'nomic-embed-text', index_dim: 768, mismatch: false },
  reindex_job: null,
}

export const adminUsers: Schemas['AdminUsersView'] = {
  users: [
    {
      id: 1,
      email: 'k.vogt@ra-vogt.de',
      display_name: 'Katharina Vogt',
      role: 'admin',
      is_active: true,
      created_at: '2026-03-14T10:00:00Z',
      last_login_at: '2026-06-17T06:40:00Z',
      owned_case_count: 3,
    },
    {
      id: 2,
      email: 'reg@example.com',
      display_name: null,
      role: 'user',
      is_active: true,
      created_at: '2026-05-01T10:00:00Z',
      last_login_at: null,
      owned_case_count: 0,
    },
  ],
  signup_enabled: false,
}
