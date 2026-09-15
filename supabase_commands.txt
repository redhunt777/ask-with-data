-- Run this in Supabase SQL Editor before starting WF4

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  schema_snapshot JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id SERIAL PRIMARY KEY,
  session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Index for fast history fetch
CREATE INDEX IF NOT EXISTS idx_messages_session_time
  ON messages (session_id, created_at DESC);

-- Helper: auto-create session row if first message for that session_id
CREATE OR REPLACE FUNCTION ensure_session(p_session_id TEXT)
RETURNS VOID AS $$
BEGIN
  INSERT INTO sessions (session_id)
  VALUES (p_session_id)
  ON CONFLICT (session_id) DO NOTHING;
END;
$$ LANGUAGE plpgsql;