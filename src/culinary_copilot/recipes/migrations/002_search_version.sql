-- Workstream 1A: track which search-document renderer built search_text.
-- Additive only; never edit 001. Existing rows default to '1'.
-- NOT APPLIED to the current application database in this change (see docs).
ALTER TABLE recipes
    ADD COLUMN IF NOT EXISTS search_document_version text NOT NULL DEFAULT '1';
