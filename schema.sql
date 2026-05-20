-- TopSDRs Tools — Database Schema
-- Safe to re-run on empty DB

CREATE TABLE IF NOT EXISTS candidates (
    id              SERIAL PRIMARY KEY,

    -- Intake form fields (Webflow)
    full_name           TEXT,
    email               TEXT UNIQUE,
    linkedin_url        TEXT,
    college             TEXT,
    graduation_year     INT,
    current_company     TEXT,
    current_title       TEXT,
    nyc_open            BOOLEAN,        -- "in or open to relocating to NYC"
    how_found           TEXT,           -- "how you found us"
    message             TEXT,           -- optional free-text message
    candidate_type      TEXT,           -- "candidate looking" | "company hiring"

    -- Raw form payload (full Webflow submission as JSON)
    form_data       JSONB,

    -- Enrichment + embedding
    enriched_text   TEXT,           -- LLM-rewritten summary shaped for embedding
    message_class   TEXT,           -- specific | ai_slop | neutral  (classifier on message field)
    embedding       REAL[],         -- Gemini embedding vector (3072 dims)
    embedded_at     TIMESTAMPTZ,

    -- Scoring
    tier            TEXT,           -- strong_intro | weak_intro | nurture | senior_redirect | polite_decline | silent_skip
    tier_rationale  TEXT,           -- why this tier was assigned
    knn_neighbors   TEXT,           -- closest labeled candidates (for context)

    -- Status
    status          TEXT NOT NULL DEFAULT 'new',  -- new | reviewed | outreach_sent | hired | rejected | archived

    -- Outreach
    outreach_subject    TEXT,
    outreach_body       TEXT,
    outreach_sent_at    TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Human labels that drive the kNN — one per candidate
CREATE TABLE IF NOT EXISTS candidate_feedback (
    candidate_id    INT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    label           INT NOT NULL,   -- 1–10 (maps to tier: 8–10 strong_intro, 6–7 weak_intro, 4–5 nurture, etc.)
    tier_override   TEXT,           -- explicit tier override if recruiter disagrees with model
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
