# TopSDRs Tools

Flask + PostgreSQL app that automates the first pass of SDR candidate screening. Webflow form submissions are scored against a two-layer pipeline (Gemini rubric → tier + kNN calibration), high-scoring candidates surface in an outreach queue, and every sent email is logged to Airtable.

## Stack
- **Backend:** Python / Flask
- **Database:** PostgreSQL via psycopg2 (raw SQL, no ORM)
- **Frontend:** Jinja2 templates, Tailwind CDN, vanilla JS, no build step
- **Hosting:** Railway (`railway.toml`, `Procfile`)
- **Scoring:** Gemini `gemini-2.5-flash` (enrichment + rubric tier scoring)
- **Embeddings:** Gemini `gemini-embedding-001` (3072 dims, used for kNN)
- **Email delivery:** Resend API (standard template, pre-filled + editable)
- **CRM sync:** Airtable (written in background thread after each email send)

## Running locally
```bash
DATABASE_URL=postgresql://localhost/topsdrs_local \
  GEMINI_API_KEY=... \
  RESEND_API_KEY=... \
  OUTREACH_FROM_EMAIL=... \
  OUTREACH_SENDER_NAME=Andrew \
  CALENDLY_LINK=... \
  AIRTABLE_API_KEY=... \
  AIRTABLE_BASE_ID=... \
  python app.py
```

## Key files
- `app.py` — Flask routes, Webflow webhook, outreach queue, Airtable sync
- `knn_scorer.py` — two-stage scoring pipeline (see Scoring section below)
- `schema.sql` — idempotent DB schema, run on every boot via `_migrate()`
- `DEPLOY.md` — full Railway/Resend/Airtable/Webflow setup guide

## Scoring pipeline (knn_scorer.py)

Two layers, both using Gemini 2.5 Flash:

**Layer 1 — Rubric scoring (runs day one, no labeled data needed)**
- `RUBRIC_SYSTEM_PROMPT` contains the full SDR screening rubric
- Pre-filters first (hard gates): grad year ≤ 2017, not open to NYC → auto `polite_decline`
- Gemini reads the enriched candidate profile and outputs a tier + rationale
- Message field is classified as `specific | ai_slop | neutral` (asymmetric weighting)
- To update the rubric or feeder company list: edit `RUBRIC_SYSTEM_PROMPT` in `knn_scorer.py`

**Layer 2 — kNN calibration (improves as labels accumulate)**
- Candidate profile is embedded and compared to labeled historical candidates
- Returns the 3 closest neighbors + similarity % as context alongside the tier
- Meaningful after ~20–30 labels; strong after ~50

**Tiers:** `strong_intro | weak_intro | nurture | senior_redirect | polite_decline | silent_skip`

**Feeder companies in rubric** (update as needed): PitchBook, Rippling, Ramp, Drata, Brex, Attentive, AlphaSights, Navan, Wunderkind, ZoomInfo

## Workflow
1. Webflow form → POST `/webhook/webflow` → candidate saved
2. Background: pre-filter → Gemini enrich → embed → rubric score → tier assigned
3. Recruiter labels at `/label` (1–10) → triggers background rescore of all unlabeled
4. `strong_intro` + `weak_intro` surface in `/outreach` (source=webflow only, never historical)
5. Recruiter clicks Compose → standard template pre-filled → edits → Send → Resend delivers
6. After send: Airtable write fires in background thread

## Candidate sources
- `webflow` — live Webflow form submissions (appear in outreach queue)
- `historical` — CSV import for backtesting; scored but **never emailed, never in outreach queue, never written to Airtable**
- `manual` — reserved for future manual entry

## Database schema
Schema in `schema.sql`. Key tables:
- `candidates` — all fields from the intake form + enriched_text, embedding, tier, tier_rationale, knn_neighbors, outreach fields, source
- `candidate_feedback` — one label per candidate (1–10), drives kNN training

`_migrate()` runs `schema.sql` on boot (idempotent) plus explicit `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for additive migrations on existing DBs.

## Routes
| Path | Purpose |
|---|---|
| `GET /` | Home: site overview, stats, setup checklist, recent candidates |
| `GET /candidates` | All candidates sorted by recency, with tier badges |
| `GET /label` | Card-stack labeling UI — one candidate at a time |
| `POST /label/submit` | Save label, trigger background rescore |
| `GET /candidates/<id>` | Candidate detail: profile, tier, rationale, kNN neighbors |
| `GET /outreach` | Queue: strong_intro/weak_intro, webflow source only, unsent |
| `GET /outreach/<id>/compose` | Pre-fill standard email template |
| `POST /outreach/<id>/send` | Send via Resend, write to Airtable |
| `POST /webhook/webflow` | Receive Webflow form submission |
| `GET /import` | Historical CSV import UI |
| `POST /import` | Process CSV upload, score in background |
| `GET /how-it-works` | Data flow visual (step-by-step pipeline) |
| `GET /scorer` | Candidate scorer explainer (training, backtesting, corrections) |

## TODO
- **Airtable import (Option B):** Pull historical candidates directly from Airtable API instead of CSV. Need Andrew to share: (1) historical candidates table name → set as `AIRTABLE_IMPORT_TABLE` env var. Column mapping unknown until he shares field names.
- **Airtable CRM column mapping:** The `_write_to_airtable()` function assumes specific column names. Andrew's actual Airtable columns are unknown — may need to update field names in `_write_to_airtable()` once he shares them.
- **Webflow field name confirmation:** Webhook parser uses fuzzy key matching. Confirm real Webflow field names once he connects the webhook and a test submission comes through.
- **Backtest:** Andrew to import ~70 historical inbounds via `/import`, then label them all at `/label` to bootstrap kNN.
- **LinkedIn enrichment:** Step 0 of the rubric says "always enrich with LinkedIn first — form data alone is unreliable." Not yet implemented. Could add a LinkedIn scrape step (e.g. via Proxycurl API) before embedding.
