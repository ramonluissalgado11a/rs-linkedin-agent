-- RS LinkedIn Agent: initial schema. Portable across SQLite and PostgreSQL.
-- Every business row carries created_at and provenance (source) where relevant.

CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- manual_entry | csv_import | public_web | linkedin_api | referral
  name TEXT NOT NULL,
  url TEXT,
  authorized INTEGER NOT NULL DEFAULT 1,
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE companies (
  id TEXT PRIMARY KEY,
  dedup_key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  website TEXT,
  industry TEXT,
  region TEXT,
  source_id TEXT NOT NULL REFERENCES sources(id),
  source_url TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE profiles (
  id TEXT PRIMARY KEY,
  dedup_key TEXT NOT NULL UNIQUE,
  full_name TEXT NOT NULL,
  headline TEXT,
  role_title TEXT,
  company_id TEXT REFERENCES companies(id),
  company_name TEXT,
  industry TEXT,
  region TEXT,
  profile_url TEXT,
  url_key TEXT UNIQUE,
  source_id TEXT NOT NULL REFERENCES sources(id),
  source_url TEXT,
  evidence_date TEXT,
  stage TEXT NOT NULL DEFAULT 'candidate_discovered',
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | qualified | rejected
  score INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE qualifications (
  id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL REFERENCES profiles(id),
  stage TEXT NOT NULL,
  passed INTEGER NOT NULL,
  reason_code TEXT,
  detail TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE actions (
  id TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  target_kind TEXT,                -- profile | company | post | self
  target_id TEXT,
  target_label TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  policy_state TEXT NOT NULL,
  policy_reasons TEXT,
  permission_used TEXT,
  risk TEXT NOT NULL DEFAULT 'low',
  payload TEXT,
  source_id TEXT REFERENCES sources(id),
  reason TEXT,
  status TEXT NOT NULL,            -- proposed | pending_approval | manual_pending | approved | executed | failed | rejected | skipped | blocked
  external_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  executed_at TEXT
);

CREATE TABLE action_queue (
  id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL UNIQUE REFERENCES actions(id),
  state TEXT NOT NULL,             -- MANUAL_APPROVAL_REQUIRED | MANUAL_ACTION_REQUIRED
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | skipped | done
  proposed_content TEXT,
  edited_content TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  queue_id TEXT NOT NULL REFERENCES action_queue(id),
  decision TEXT NOT NULL,          -- approve | reject | edit | skip | mark_done
  actor TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE content (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- post | comment_draft | connection_note
  topic TEXT,
  body TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE,
  shingles TEXT,
  model TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL
);

CREATE TABLE posts (
  id TEXT PRIMARY KEY,
  content_id TEXT NOT NULL REFERENCES content(id),
  action_id TEXT REFERENCES actions(id),
  linkedin_urn TEXT,
  author_urn TEXT,
  status TEXT NOT NULL,            -- queued | published | failed
  published_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE metrics (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  value REAL NOT NULL,
  labels TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE errors (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,          -- temporary | provider | permission | authentication | rate_limit | validation | policy | internal
  component TEXT NOT NULL,
  message TEXT NOT NULL,
  run_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
  id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  event TEXT NOT NULL,
  subject_kind TEXT,
  subject_id TEXT,
  detail TEXT,
  run_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE system_runs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE api_permissions (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  scope TEXT NOT NULL,
  granted INTEGER NOT NULL,
  checked_at TEXT NOT NULL,
  UNIQUE (provider, scope)
);

CREATE TABLE oauth_tokens (
  provider TEXT PRIMARY KEY,
  member_urn TEXT,
  access_token_enc TEXT NOT NULL,
  refresh_token_enc TEXT,
  expires_at TEXT,
  refresh_expires_at TEXT,
  scopes TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE oauth_states (
  state TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload TEXT,
  status TEXT NOT NULL,            -- queued | running | succeeded | failed | dead
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  run_after TEXT NOT NULL,
  last_error TEXT,
  error_category TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE llm_usage (
  id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd REAL NOT NULL,
  subject_kind TEXT,
  subject_id TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_profiles_status ON profiles(status);
CREATE INDEX idx_actions_type_created ON actions(action_type, created_at);
CREATE INDEX idx_actions_target ON actions(target_id);
CREATE INDEX idx_queue_status ON action_queue(status);
CREATE INDEX idx_jobs_status_run_after ON jobs(status, run_after);
CREATE INDEX idx_llm_usage_created ON llm_usage(created_at);
CREATE INDEX idx_audit_created ON audit_log(created_at)
