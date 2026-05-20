# TopSDRs Tools

Flask + PostgreSQL app for screening SDR candidates from Webflow form submissions.
Candidates are embedded via Gemini and scored by kNN similarity to past labeled candidates.

## Stack
- **Backend:** Python / Flask
- **Database:** PostgreSQL via psycopg2 (raw SQL, no ORM)
- **Frontend:** Jinja2 templates, vanilla JS, no build step
- **Hosting:** Railway (`railway.toml`, `Procfile`)
- **Embeddings:** Gemini `gemini-embedding-001` (3072 dims)
- **LLM enrichment:** `gemini-2.5-flash` (profile enrichment + tier scoring via rubric)
- **Email delivery:** Resend API (standard template, pre-filled and editable by recruiter)
- **CRM sync:** Airtable (written after each email send)

## Running locally
```bash
DATABASE_URL=postgresql://... GEMINI_API_KEY=... RESEND_API_KEY=... \
  CALENDLY_LINK=... AIRTABLE_API_KEY=... AIRTABLE_BASE_ID=... OUTREACH_SENDER_NAME=... python app.py
```

## Key files
- `app.py` — Flask routes + Webflow webhook handler + outreach queue
- `knn_scorer.py` — two-stage pipeline: (1) Gemini enrichment → embedding, (2) Gemini rubric → tier; kNN used as calibration context
- `schema.sql` — DB schema (idempotent, run on boot)
- `templates/outreach.html` — Queue of scored candidates awaiting outreach
- `templates/outreach_review.html` — Review/edit pre-filled standard email before sending
- `DEPLOY.md` — Full Railway/Resend/Airtable/Webflow setup guide
- `templates/how_it_works.html` — Public explainer page at `/how-it-works`
- `templates/import.html` — CSV upload UI at `/import` for loading historical candidates as training data
- `templates/scorer.html` — Internal explainer at `/scorer` covering rubric, kNN calibration, and backtesting
- `docs/workflow.html` — Visual flowchart of the full candidate pipeline

## Workflow
1. Webflow form submission → POST to `/webhook/webflow`
2. Candidate saved to `candidates` table
3. Background thread: hard pre-filter → Gemini enrichment → embedding → rubric tier scoring
4. Tier assigned: `strong_intro | weak_intro | nurture | senior_redirect | polite_decline | silent_skip`
5. Recruiter labels candidates at `/label` (1–10); each label triggers rescore of unlabeled candidates
6. `strong_intro` and `weak_intro` candidates surface in `/outreach` queue
7. Recruiter clicks Compose → standard template pre-filled with candidate details + Calendly link
8. Recruiter reviews/edits at `/outreach/<id>/compose`, then clicks Send → Resend API delivers it
9. Outreach status (`pending` / `drafted` / `sent`) tracked on `candidates` table; Airtable synced in background

## Candidate sources
- `source` column on `candidates`: `webflow` (live form), `historical` (CSV import), `manual`
- Historical candidates are scored as training data only — never appear in outreach queue, never emailed, never written to Airtable
- Import via `/import` (CSV upload); backtesting via `/scorer`
