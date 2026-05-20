"""
kNN fit scoring for SDR candidates.
Embedding via Gemini, cosine similarity against labeled candidates.

Embedding model: gemini-embedding-001 (3072 dims)
"""
import math
import os
import time

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
EMBED_MODEL    = "gemini-embedding-001"
EMBED_URL      = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent"
CHAT_MODEL     = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
CHAT_URL       = f"https://generativelanguage.googleapis.com/v1beta/models/{CHAT_MODEL}:generateContent"

# Shapes candidate text toward the axes that matter for SDR fit before embedding.
# To be refined once the rubric arrives from the recruiter.
ENRICH_SYSTEM_PROMPT = """You are condensing a sales candidate profile into a screening summary that will be embedded and compared (by cosine similarity) against past candidates to find similar ones.

Output 3-4 sentences in one paragraph. Cover, in order:

1. Current or most recent role and company type (startup, enterprise, SMB-focused, etc.)
2. Years of SDR/BDR experience and deal types (outbound, inbound, enterprise, SMB).
3. Industry vertical experience — only what is clearly stated.
4. Performance signals — quota attainment, pipeline generated, specific metrics — only if explicitly stated.
5. Tools and motion (Salesforce, Outreach, cold calling, sequencing, etc.) — only if stated.
6. Progression signals — promotions, AE transition, leadership — only if stated.

Strict rules:
- Stay factual. Never invent experience, metrics, or tools.
- If a field isn't supported by the input, omit it rather than say "not specified".
- One paragraph, no headers, no bullet points, no markdown.
- Optimize for clustering similar candidates together — use precise, repeatable terminology."""


def _candidate_to_structured(c: dict) -> str:
    fields = []
    if c.get("full_name"):    fields.append(f"Name: {c['full_name']}")
    if c.get("form_data"):
        fd = c["form_data"]
        if isinstance(fd, dict):
            for k, v in fd.items():
                if v and k not in ("email", "full_name"):
                    fields.append(f"{k}: {v}")
    return "\n".join(fields)


def gemini_enrich(c: dict) -> str | None:
    """Rewrite a candidate dict into an embedding-friendly summary. Returns None on failure."""
    if not GEMINI_API_KEY:
        return None
    structured = _candidate_to_structured(c)
    if not structured.strip():
        return None
    payload = {
        "systemInstruction": {"parts": [{"text": ENRICH_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": structured}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 300},
    }
    for attempt, delay in enumerate([1, 2, 4]):
        try:
            r = requests.post(f"{CHAT_URL}?key={GEMINI_API_KEY}", json=payload, timeout=60)
            r.raise_for_status()
            body = r.json()
            if "candidates" not in body:
                if attempt < 2:
                    time.sleep(delay)
                    continue
                return None
            text = body["candidates"][0]["content"]["parts"][0]["text"].strip()
            return text or None
        except Exception as e:
            if attempt < 2:
                time.sleep(delay)
            else:
                print(f"[enrich] final failure: {e}")
                return None


def gemini_embed(text: str) -> list[float] | None:
    """Embed text with Gemini. Returns None on failure."""
    if not GEMINI_API_KEY or not text:
        return None
    payload = {"content": {"parts": [{"text": text}]}}
    for attempt, delay in enumerate([1, 2, 4]):
        try:
            r = requests.post(f"{EMBED_URL}?key={GEMINI_API_KEY}", json=payload, timeout=30)
            r.raise_for_status()
            return r.json()["embedding"]["values"]
        except Exception as e:
            if attempt < 2:
                time.sleep(delay)
            else:
                print(f"[embed] final failure: {e}")
                return None


def candidate_to_text(c: dict) -> str:
    """Serialize a candidate for embedding. Prefers enriched_text when present."""
    enriched = (c.get("enriched_text") or "").strip()
    if enriched:
        name = (c.get("full_name") or "").strip()
        return f"{name}. {enriched}" if name else enriched
    parts = [c.get("full_name") or ""]
    fd = c.get("form_data") or {}
    if isinstance(fd, dict):
        vals = [str(v) for v in fd.values() if v]
        if vals:
            parts.append("— " + ", ".join(vals))
    return " ".join(parts)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def knn_score_from_fixtures(target_embedding, labeled_rows, k=5):
    """Core kNN logic. labeled_rows: list of dicts with keys full_name, embedding, label."""
    if not labeled_rows or target_embedding is None:
        return None, None

    scored = [(_cosine(target_embedding, r["embedding"]), r) for r in labeled_rows]
    scored.sort(key=lambda x: x[0], reverse=True)

    seen = set()
    top = []
    for sim, r in scored:
        name = r.get("full_name") or str(r.get("id"))
        if name in seen:
            continue
        seen.add(name)
        top.append((sim, r))
        if len(top) >= k:
            break
    if not top:
        return None, None

    weights = [1.0 / max(1.0 - sim, 0.01) for sim, _ in top]
    score_float = sum(w * r["label"] for w, (_, r) in zip(weights, top)) / sum(weights)
    score = max(1, min(10, round(score_float)))

    top_names = ", ".join(f"{r.get('full_name', 'unknown')} ({r['label']})" for _, r in top[:3])
    best_match = int(round(top[0][0] * 100))
    rationale = f"Similar to {top_names} [match {best_match}%]"
    if len(labeled_rows) < 5:
        rationale = f"[low confidence, only {len(labeled_rows)} labels] {rationale}"

    return score, rationale


def knn_score(target_embedding, db_conn, k=5):
    """Production entry: pulls labeled candidates from DB and runs kNN."""
    if target_embedding is None:
        return None, None
    import psycopg2.extras
    cur = db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.full_name, c.embedding, cf.label
          FROM candidates c
          JOIN candidate_feedback cf ON cf.candidate_id = c.id
         WHERE c.embedding IS NOT NULL
    """)
    rows = [
        {"id": r["id"], "full_name": r["full_name"],
         "embedding": list(r["embedding"]), "label": r["label"]}
        for r in cur.fetchall()
    ]
    cur.close()
    return knn_score_from_fixtures(target_embedding, rows, k=k)
