"""
TopSDRs Tools — Flask app
Webflow webhook → Postgres → Gemini embed + rubric score → outreach queue → Resend
"""
import hashlib
import hmac
import json
import os
import threading

import psycopg2
import psycopg2.extras
import resend
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import knn_scorer

app = Flask(__name__)

DATABASE_URL           = os.environ.get("DATABASE_URL")
WEBFLOW_WEBHOOK_SECRET = os.environ.get("WEBFLOW_WEBHOOK_SECRET", "")
RESEND_API_KEY         = os.environ.get("RESEND_API_KEY", "")
OUTREACH_FROM_EMAIL    = os.environ.get("OUTREACH_FROM_EMAIL", "hello@example.com")
CALENDLY_LINK          = os.environ.get("CALENDLY_LINK", "")
AIRTABLE_API_KEY       = os.environ.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID       = os.environ.get("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_NAME    = os.environ.get("AIRTABLE_TABLE_NAME", "Candidates")
OUTREACH_SENDER_NAME   = os.environ.get("OUTREACH_SENDER_NAME", "Andrew")

resend.api_key = RESEND_API_KEY


def _outreach_template(candidate: dict) -> tuple[str, str]:
    """Standard outreach email — recruiter edits before sending."""
    name      = (candidate.get("full_name") or "").split()[0] or "there"
    calendly  = CALENDLY_LINK or "[CALENDLY LINK]"
    subject   = f"Quick intro — TopSDRs"
    body      = (
        f"Hi {name},\n\n"
        f"Your background caught our eye — we work exclusively with top SDR candidates "
        f"and think you could be a strong fit for some of the roles we place.\n\n"
        f"Would love to connect for a quick 15-minute intro call. "
        f"Here's a link to find a time: {calendly}\n\n"
        f"Best,\n{OUTREACH_SENDER_NAME}\nTopSDRs"
    )
    return subject, body


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def _migrate():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(open("schema.sql").read())
    # Additive column migrations for existing databases
    for ddl in [
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'webflow'",
    ]:
        cur.execute(ddl)
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Background scoring
# ---------------------------------------------------------------------------

def _rescore_all_unlabeled(skip_id=None):
    """Re-score every unreviewed candidate via kNN. Runs in a background thread."""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.id, c.full_name, c.embedding
              FROM candidates c
             WHERE c.embedding IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM candidate_feedback cf WHERE cf.candidate_id = c.id
               )
               AND (%s IS NULL OR c.id <> %s)
        """, (skip_id, skip_id))
        unlabeled = cur.fetchall()

        cur.execute("""
            SELECT c.id, c.full_name, c.embedding, cf.label
              FROM candidates c
              JOIN candidate_feedback cf ON cf.candidate_id = c.id
             WHERE c.embedding IS NOT NULL
        """)
        labeled = [
            {"id": r["id"], "full_name": r["full_name"],
             "embedding": list(r["embedding"]), "label": r["label"]}
            for r in cur.fetchall()
        ]

        updates = []
        for row in unlabeled:
            score, rationale = knn_scorer.knn_score_from_fixtures(
                list(row["embedding"]), labeled
            )
            if score is not None:
                updates.append((score, rationale, row["id"]))

        if updates:
            psycopg2.extras.execute_values(
                cur,
                "UPDATE candidates SET tier = data.tier, tier_rationale = data.rationale "
                "FROM (VALUES %s) AS data(tier, rationale, id) WHERE candidates.id = data.id",
                updates,
                template="(%s, %s, %s)"
            )
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[rescore] error: {e}")


# ---------------------------------------------------------------------------
# Webflow webhook
# ---------------------------------------------------------------------------

@app.route("/webhook/webflow", methods=["POST"])
def webflow_webhook():
    # Verify signature if secret is set
    if WEBFLOW_WEBHOOK_SECRET:
        sig = request.headers.get("X-Webflow-Signature", "")
        expected = hmac.new(
            WEBFLOW_WEBHOOK_SECRET.encode(), request.data, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            abort(401)

    payload = request.get_json(force=True) or {}
    # Webflow wraps form data under "data" key
    form_data = payload.get("data", payload)

    def _get(keys):
        for k in keys:
            v = form_data.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    full_name       = _get(["Name", "name", "full_name", "Full Name"])
    email           = _get(["Email", "email"])
    linkedin_url    = _get(["LinkedIn", "linkedin", "linkedin_url", "LinkedIn URL"])
    college         = _get(["College", "college", "School", "school"])
    grad_year_raw   = _get(["Graduation year", "graduation_year", "Grad Year", "grad_year"])
    graduation_year = int(grad_year_raw) if grad_year_raw and grad_year_raw.isdigit() else None
    current_company = _get(["Current Company", "current_company", "Company"])
    current_title   = _get(["Current title", "current_title", "Title"])
    how_found       = _get(["How you found us", "how_found", "How did you find us"])
    message         = _get(["Optional Message", "message", "Message", "Why TopSDRs"])
    candidate_type  = _get(["Are you a candidate looking or company hiring",
                             "candidate_type", "Type"])

    # NYC checkbox — Webflow sends "true"/"false" strings or booleans
    nyc_raw = form_data.get("are you in or open to relocating to NYC",
                form_data.get("nyc_open",
                form_data.get("NYC", None)))
    if nyc_raw is None:
        nyc_open = None
    elif isinstance(nyc_raw, bool):
        nyc_open = nyc_raw
    else:
        nyc_open = str(nyc_raw).lower() in ("true", "yes", "1")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO candidates (
            full_name, email, linkedin_url, college, graduation_year,
            current_company, current_title, nyc_open, how_found, message,
            candidate_type, form_data
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (email) DO UPDATE
            SET full_name       = EXCLUDED.full_name,
                linkedin_url    = EXCLUDED.linkedin_url,
                college         = EXCLUDED.college,
                graduation_year = EXCLUDED.graduation_year,
                current_company = EXCLUDED.current_company,
                current_title   = EXCLUDED.current_title,
                nyc_open        = EXCLUDED.nyc_open,
                how_found       = EXCLUDED.how_found,
                message         = EXCLUDED.message,
                candidate_type  = EXCLUDED.candidate_type,
                form_data       = EXCLUDED.form_data,
                updated_at      = NOW()
        RETURNING id
    """, (full_name, email, linkedin_url, college, graduation_year,
          current_company, current_title, nyc_open, how_found, message,
          candidate_type, json.dumps(form_data)))
    candidate_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    # Enrich + embed in background
    threading.Thread(target=_enrich_and_embed, args=(candidate_id,), daemon=True).start()

    return jsonify({"ok": True, "id": candidate_id})


def _enrich_and_embed(candidate_id: int):
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
        c = dict(cur.fetchone())

        # Hard pre-filter before any LLM calls
        passes, filter_reason = knn_scorer.pre_filter(c)
        if not passes:
            cur2 = conn.cursor()
            cur2.execute("""
                UPDATE candidates
                   SET tier = 'polite_decline', tier_rationale = %s, updated_at = NOW()
                 WHERE id = %s
            """, (filter_reason, candidate_id))
            conn.commit()
            cur2.close()
            cur.close()
            conn.close()
            return

        # Enrich → embed → kNN neighbors
        enriched = knn_scorer.gemini_enrich(c)
        c["enriched_text"] = enriched
        text = knn_scorer.candidate_to_text(c)
        embedding = knn_scorer.gemini_embed(text)
        neighbors = knn_scorer.knn_neighbors_from_db(embedding, conn) if embedding else ""

        # Apply rubric via LLM
        tier, rationale, message_class = knn_scorer.score_candidate_rubric(c)

        cur2 = conn.cursor()
        cur2.execute("""
            UPDATE candidates
               SET enriched_text  = %s,
                   message_class  = %s,
                   embedding      = %s,
                   embedded_at    = NOW(),
                   tier           = %s,
                   tier_rationale = %s,
                   knn_neighbors  = %s,
                   updated_at     = NOW()
             WHERE id = %s
        """, (enriched, message_class, embedding, tier, rationale, neighbors, candidate_id))
        conn.commit()
        cur.close()
        cur2.close()
        conn.close()
    except Exception as e:
        print(f"[enrich_embed] candidate {candidate_id}: {e}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status = 'new') AS new_count,
               COUNT(cf.candidate_id) AS labeled_count
          FROM candidates c
     LEFT JOIN candidate_feedback cf ON cf.candidate_id = c.id
    """)
    stats = dict(cur.fetchone())
    cur.execute("""
        SELECT c.*, cf.label
          FROM candidates c
     LEFT JOIN candidate_feedback cf ON cf.candidate_id = c.id
         ORDER BY c.created_at DESC
         LIMIT 10
    """)
    recent = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return render_template("home.html", stats=stats, recent=recent)


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/scorer")
def scorer_explained():
    return render_template("scorer.html")


@app.route("/import", methods=["GET", "POST"])
def import_historical():
    """CSV import for historical candidates — scored for KNN training, never emailed."""
    import csv, io
    if request.method == "GET":
        return render_template("import.html")

    f = request.files.get("csv_file")
    if not f:
        return render_template("import.html", error="No file uploaded.")

    text    = f.read().decode("utf-8-sig")
    reader  = csv.DictReader(io.StringIO(text))
    conn    = get_db()
    cur     = conn.cursor()
    imported, skipped = 0, 0

    for row in reader:
        def g(keys):
            for k in keys:
                v = row.get(k, "").strip()
                if v:
                    return v
            return None

        full_name       = g(["Name", "Full Name", "name"])
        email           = g(["Email", "email"])
        linkedin_url    = g(["LinkedIn", "linkedin_url", "LinkedIn URL"])
        college         = g(["College", "School", "college"])
        grad_raw        = g(["Graduation Year", "Grad Year", "graduation_year"])
        graduation_year = int(grad_raw) if grad_raw and grad_raw.isdigit() else None
        current_company = g(["Current Company", "Company", "current_company"])
        current_title   = g(["Current Title", "Title", "current_title"])
        nyc_raw         = g(["NYC Open", "nyc_open", "NYC"])
        nyc_open        = nyc_raw.lower() in ("yes", "true", "1") if nyc_raw else None
        message         = g(["Message", "Optional Message", "message"])
        how_found       = g(["Source", "How Found", "how_found"])

        if not full_name and not email:
            skipped += 1
            continue

        try:
            cur.execute("""
                INSERT INTO candidates (
                    full_name, email, linkedin_url, college, graduation_year,
                    current_company, current_title, nyc_open, message, how_found,
                    source
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'historical')
                ON CONFLICT (email) DO NOTHING
            """, (full_name, email, linkedin_url, college, graduation_year,
                  current_company, current_title, nyc_open, message, how_found))
            if cur.rowcount:
                imported += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[import] row skipped: {e}")
            skipped += 1

    conn.commit()
    cur.close()

    # Score all unscored historical candidates in background
    cur2 = conn.cursor()
    cur2.execute("""
        SELECT id FROM candidates
         WHERE source = 'historical' AND tier IS NULL
    """)
    ids = [r[0] for r in cur2.fetchall()]
    cur2.close()
    conn.close()

    for cid in ids:
        threading.Thread(target=_enrich_and_embed, args=(cid,), daemon=True).start()

    return render_template("import.html", imported=imported, skipped=skipped, total=imported+skipped)


@app.route("/candidates")
def candidates():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.*, cf.label
          FROM candidates c
     LEFT JOIN candidate_feedback cf ON cf.candidate_id = c.id
         ORDER BY c.created_at DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return render_template("candidates.html", candidates=rows)


@app.route("/label")
def label():
    """Card-stack labeling UI — show next unlabeled candidate."""
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.*
          FROM candidates c
         WHERE NOT EXISTS (
             SELECT 1 FROM candidate_feedback cf WHERE cf.candidate_id = c.id
         )
           AND c.embedding IS NOT NULL
         ORDER BY c.created_at DESC
         LIMIT 1
    """)
    row = cur.fetchone()
    candidate = dict(row) if row else None
    cur.execute("SELECT COUNT(*) AS n FROM candidate_feedback")
    label_count = cur.fetchone()["n"]
    cur.close()
    conn.close()
    return render_template("label.html", candidate=candidate, label_count=label_count)


@app.route("/label/submit", methods=["POST"])
def label_submit():
    candidate_id = int(request.form["candidate_id"])
    label        = int(request.form["label"])
    notes        = request.form.get("notes", "").strip() or None

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO candidate_feedback (candidate_id, label, notes)
        VALUES (%s, %s, %s)
        ON CONFLICT (candidate_id) DO UPDATE
            SET label = EXCLUDED.label,
                notes = EXCLUDED.notes
    """, (candidate_id, label, notes))
    # Mark the candidate as reviewed
    cur.execute("UPDATE candidates SET status = 'reviewed' WHERE id = %s", (candidate_id,))
    conn.commit()
    cur.close()
    conn.close()

    threading.Thread(target=_rescore_all_unlabeled, daemon=True).start()
    return redirect(url_for("label"))


@app.route("/candidates/<int:candidate_id>")
def candidate_detail(candidate_id):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.*, cf.label, cf.notes AS label_notes
          FROM candidates c
     LEFT JOIN candidate_feedback cf ON cf.candidate_id = c.id
         WHERE c.id = %s
    """, (candidate_id,))
    row = cur.fetchone()
    if not row:
        abort(404)
    cur.close()
    conn.close()
    return render_template("candidate_detail.html", candidate=dict(row))


# ---------------------------------------------------------------------------
# Outreach
# ---------------------------------------------------------------------------

# Tiers that surface in the outreach queue
OUTREACH_TIERS = {"strong_intro", "weak_intro"}


def _write_to_airtable(candidate: dict):
    """Write candidate + outreach record to Airtable CRM. Silently skips if not configured."""
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        return
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    fields = {
        "Name":             candidate.get("full_name") or "",
        "Email":            candidate.get("email") or "",
        "LinkedIn":         candidate.get("linkedin_url") or "",
        "College":          candidate.get("college") or "",
        "Graduation Year":  str(candidate["graduation_year"]) if candidate.get("graduation_year") else "",
        "Current Company":  candidate.get("current_company") or "",
        "Current Title":    candidate.get("current_title") or "",
        "NYC Open":         "Yes" if candidate.get("nyc_open") else "No",
        "Tier":             knn_scorer.TIER_LABELS.get(candidate.get("tier", ""), candidate.get("tier", "")),
        "Tier Rationale":   candidate.get("tier_rationale") or "",
        "Outreach Subject": candidate.get("outreach_subject") or "",
        "Outreach Sent":    candidate.get("outreach_sent_at").isoformat() if candidate.get("outreach_sent_at") else "",
        "Source":           candidate.get("how_found") or "",
    }
    # Remove empty strings to keep Airtable clean
    fields = {k: v for k, v in fields.items() if v}
    try:
        requests.post(
            url,
            headers={"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10,
        )
    except Exception as e:
        print(f"[airtable] write failed: {e}")



@app.route("/outreach")
def outreach_queue():
    """strong_intro and weak_intro candidates that haven't been contacted yet."""
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.*, cf.label
          FROM candidates c
     LEFT JOIN candidate_feedback cf ON cf.candidate_id = c.id
         WHERE c.tier IN ('strong_intro', 'weak_intro')
           AND c.outreach_sent_at IS NULL
           AND c.status != 'rejected'
           AND c.source != 'historical'
         ORDER BY CASE c.tier WHEN 'strong_intro' THEN 0 ELSE 1 END,
                  c.created_at DESC
    """)
    queue = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT COUNT(*) AS n FROM candidates WHERE outreach_sent_at IS NOT NULL")
    sent_count = cur.fetchone()["n"]
    cur.close()
    conn.close()
    return render_template("outreach.html", queue=queue, sent_count=sent_count)


@app.route("/outreach/<int:candidate_id>/compose")
def outreach_compose(candidate_id):
    """Pre-fill the standard email template for a candidate."""
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
    row = cur.fetchone()
    if not row:
        abort(404)
    candidate = dict(row)
    cur.close()
    conn.close()

    # Pre-fill template if not already drafted
    if not candidate.get("outreach_subject"):
        subject, body = _outreach_template(candidate)
        candidate["outreach_subject"] = subject
        candidate["outreach_body"]    = body

    return render_template("outreach_review.html", candidate=candidate)


@app.route("/outreach/<int:candidate_id>/review")
def outreach_review(candidate_id):
    """Show the drafted email for review/edit before sending."""
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
    row = cur.fetchone()
    if not row:
        abort(404)
    cur.close()
    conn.close()
    return render_template("outreach_review.html", candidate=dict(row))


@app.route("/outreach/<int:candidate_id>/send", methods=["POST"])
def outreach_send(candidate_id):
    """Send the (possibly edited) email via Resend."""
    subject = request.form.get("subject", "").strip()
    body    = request.form.get("body", "").strip()

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
    row = cur.fetchone()
    if not row:
        abort(404)
    candidate = dict(row)

    if not candidate.get("email"):
        abort(400, "Candidate has no email address")

    resend.Emails.send({
        "from": OUTREACH_FROM_EMAIL,
        "to": candidate["email"],
        "subject": subject,
        "text": body,
    })

    cur2 = conn.cursor()
    cur2.execute("""
        UPDATE candidates
           SET outreach_subject  = %s,
               outreach_body     = %s,
               outreach_sent_at  = NOW(),
               status            = 'outreach_sent',
               updated_at        = NOW()
         WHERE id = %s
    """, (subject, body, candidate_id))
    conn.commit()
    cur2.close()

    # Refresh candidate dict with sent_at for Airtable
    cur3 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur3.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
    sent_candidate = dict(cur3.fetchone())
    cur3.close()
    cur.close()
    conn.close()

    threading.Thread(target=_write_to_airtable, args=(sent_candidate,), daemon=True).start()
    return redirect(url_for("outreach_queue"))


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

_migrate()  # runs on gunicorn import and local dev alike

if __name__ == "__main__":
    app.run(debug=True, port=5001)
