-- 003: fully-rejected rows persist with a status, not just a reason.
-- Extends recipe_quarantine (created in 001) without touching existing rows:
-- routing quarantines keep status 'quarantined'; validator-rejected rows use
-- 'rejected'. problems carries the full validator problem list (codes,
-- classes, details) so rejections stay auditable without re-running.
ALTER TABLE recipe_quarantine
  ADD COLUMN IF NOT EXISTS source_id text,
  ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'quarantined',
  ADD COLUMN IF NOT EXISTS verdict text,
  ADD COLUMN IF NOT EXISTS problems jsonb NOT NULL DEFAULT '[]'::jsonb;
