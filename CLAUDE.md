# TopSDRs Tools

Flask + PostgreSQL app for screening SDR candidates from Webflow form submissions.
Candidates are embedded via Gemini and scored by kNN similarity to past labeled candidates.

## Stack
- **Backend:** Python / Flask
- **Database:** PostgreSQL via psycopg2 (raw SQL, no ORM)
- **Frontend:** Jinja2 templates, vanilla JS, no build step
- **Hosting:** Railway (`railway.toml`, `Procfile`)
- **Embeddings:** Gemini `gemini-embedding-001` (3072 dims)
- **LLM enrichment:** `gemini-2.5-flash`
- **Email drafting:** Claude `claude-sonnet-4-6` (Anthropic SDK)
- **Email delivery:** Resend API

## Running locally
```bash
DATABASE_URL=postgresql://... GEMINI_API_KEY=... ANTHROPIC_API_KEY=... RESEND_API_KEY=... python app.py
```

## Key files
- `app.py` — Flask routes + Webflow webhook handler + outreach queue
- `knn_scorer.py` — Gemini embedding + cosine kNN scoring
- `schema.sql` — DB schema (idempotent, run on boot)
- `templates/outreach.html` — Queue of 7+ scored candidates awaiting outreach
- `templates/outreach_review.html` — Review/edit Claude-drafted email before sending

## Workflow
1. Webflow form submission → POST to `/webhook/webflow`
2. Candidate saved to `candidates` table
3. Background thread: Gemini enriches → embeds → kNN scores
4. Recruiter labels candidates at `/label` (1–10)
5. Each new label triggers a background rescore of all unlabeled candidates
6. Candidates scoring 7+ surface in `/outreach` queue
7. Recruiter clicks Draft → Claude (claude-sonnet-4-6) writes personalized email with Calendly link
8. Recruiter reviews/edits at `/outreach/<id>/review`, then clicks Send → Resend API delivers it
9. Outreach status (`pending` / `drafted` / `sent`) tracked on `candidates` table

## TODO
- Confirm Webflow form field names and update `schema.sql` + webhook parser
- Update `ENRICH_SYSTEM_PROMPT` in `knn_scorer.py` with the actual SDR rubric
- Add backfill script for past Webflow submissions
- Build out templates (home, candidates list, label UI, candidate detail)
