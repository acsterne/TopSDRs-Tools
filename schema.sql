-- TopSDRs Tools — Database Schema
-- Safe to re-run on empty DB

CREATE TABLE IF NOT EXISTS candidates (
    id              SERIAL PRIMARY KEY,
    -- Core identity (from Webflow form — fields to be confirmed)
    full_name       TEXT,
    email           TEXT UNIQUE,
    linkedin_url    TEXT,

    -- Raw form payload (full Webflow submission as JSON)
    form_data       JSONB,

    -- Enrichment
    enriched_text   TEXT,           -- LLM-rewritten candidate summary for embedding
    embedding       REAL[],         -- Gemini embedding vector (3072 dims)
    embedded_at     TIMESTAMPTZ,

    -- Scoring
    fit_score       INT,            -- 1–10, kNN predicted fit
    fit_rationale   TEXT,           -- e.g. "Similar to Jane D. (9), John S. (8) [match 84%]"

    -- Status
    status          TEXT NOT NULL DEFAULT 'new',  -- new | reviewed | outreach_sent | hired | rejected | archived

    -- Outreach
    outreach_subject    TEXT,
    outreach_body       TEXT,       -- Claude-drafted email, editable before send
    outreach_sent_at    TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Human labels that drive the kNN — one per candidate
CREATE TABLE IF NOT EXISTS candidate_feedback (
    candidate_id    INT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    label           INT NOT NULL,   -- 1–10
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
