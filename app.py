"""
TopSDRs Tools — Flask app
Webflow webhook → Postgres → embed → kNN score → outreach approval → Resend
"""
import hashlib
import hmac
import json
import os
import threading

import anthropic
import psycopg2
import psycopg2.extras
import resend
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

import knn_scorer

app = Flask(__name__)

DATABASE_URL           = os.environ.get("DATABASE_URL")
WEBFLOW_WEBHOOK_SECRET = os.environ.get("WEBFLOW_WEBHOOK_SECRET", "")
RESEND_API_KEY         = os.environ.get("RESEND_API_KEY", "")
OUTREACH_FROM_EMAIL    = os.environ.get("OUTREACH_FROM_EMAIL", "hello@example.com")
CALENDLY_LINK          = os.environ.get("CALENDLY_LINK", "")

resend.api_key = RESEND_API_KEY


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
                "UPDATE candidates SET fit_score = data.score, fit_rationale = data.rationale "
                "FROM (VALUES %s) AS data(score, rationale, id) WHERE candidates.id = data.id",
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

    full_name = form_data.get("Name") or form_data.get("full_name") or form_data.get("name")
    email     = form_data.get("Email") or form_data.get("email")

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO candidates (full_name, email, form_data)
        VALUES (%s, %s, %s)
        ON CONFLICT (email) DO UPDATE
            SET full_name  = EXCLUDED.full_name,
                form_data  = EXCLUDED.form_data,
                updated_at = NOW()
        RETURNING id
    """, (full_name, email, json.dumps(form_data)))
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

        enriched = knn_scorer.gemini_enrich(c)
        c["enriched_text"] = enriched
        text = knn_scorer.candidate_to_text(c)
        embedding = knn_scorer.gemini_embed(text)

        score, rationale = None, None
        if embedding:
            score, rationale = knn_scorer.knn_score(embedding, conn)

        cur2 = conn.cursor()
        cur2.execute("""
            UPDATE candidates
               SET enriched_text  = %s,
                   embedding      = %s,
                   embedded_at    = NOW(),
                   fit_score      = %s,
                   fit_rationale  = %s,
                   updated_at     = NOW()
             WHERE id = %s
        """, (enriched, embedding, score, rationale, candidate_id))
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


@app.route("/candidates")
def candidates():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.*, cf.label
          FROM candidates c
     LEFT JOIN candidate_feedback cf ON cf.candidate_id = c.id
         ORDER BY c.fit_score DESC NULLS LAST, c.created_at DESC
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
         ORDER BY c.fit_score DESC NULLS LAST, c.created_at DESC
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

OUTREACH_SCORE_THRESHOLD = 7


def _draft_outreach_email(candidate: dict) -> tuple[str, str]:
    """Ask Claude to draft a personalized outreach email. Returns (subject, body)."""
    client = anthropic.Anthropic()
    profile = candidate.get("enriched_text") or candidate.get("full_name") or "the candidate"
    calendly = CALENDLY_LINK or "[CALENDLY LINK]"
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Write a short, warm outreach email to an SDR candidate from a recruiter.

Candidate profile:
{profile}

Rules:
- 3–4 sentences max, no fluff
- Reference something specific from their profile (role, experience, tools)
- End with a clear CTA: book a 15-min call via this Calendly link: {calendly}
- Do not use the word "excited" or "passionate"
- Plain text only, no markdown, no subject line in the body

Return JSON with exactly two keys: "subject" and "body"."""
        }]
    )
    import re
    text = msg.content[0].text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    return parsed["subject"], parsed["body"]


@app.route("/outreach")
def outreach_queue():
    """Candidates scoring ≥ threshold that haven't been contacted yet."""
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.*, cf.label
          FROM candidates c
     LEFT JOIN candidate_feedback cf ON cf.candidate_id = c.id
         WHERE (cf.label >= %s OR (cf.label IS NULL AND c.fit_score >= %s))
           AND c.outreach_sent_at IS NULL
           AND c.status != 'rejected'
         ORDER BY COALESCE(cf.label, c.fit_score) DESC NULLS LAST
    """, (OUTREACH_SCORE_THRESHOLD, OUTREACH_SCORE_THRESHOLD))
    queue = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT COUNT(*) AS sent_count
          FROM candidates
         WHERE outreach_sent_at IS NOT NULL
    """)
    sent_count = cur.fetchone()["sent_count"]
    cur.close()
    conn.close()
    return render_template("outreach.html", queue=queue, sent_count=sent_count,
                           threshold=OUTREACH_SCORE_THRESHOLD)


@app.route("/outreach/<int:candidate_id>/draft", methods=["POST"])
def outreach_draft(candidate_id):
    """Generate a Claude email draft for a candidate."""
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
    row = cur.fetchone()
    if not row:
        abort(404)
    candidate = dict(row)
    cur.close()

    subject, body = _draft_outreach_email(candidate)

    cur2 = conn.cursor()
    cur2.execute("""
        UPDATE candidates SET outreach_subject = %s, outreach_body = %s WHERE id = %s
    """, (subject, body, candidate_id))
    conn.commit()
    cur2.close()
    conn.close()
    return redirect(url_for("outreach_review", candidate_id=candidate_id))


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
    cur.close()
    conn.close()
    return redirect(url_for("outreach_queue"))


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _migrate()
    app.run(debug=True, port=5001)
