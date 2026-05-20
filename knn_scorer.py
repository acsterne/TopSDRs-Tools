"""
Candidate scoring for TopSDRs Tools.

Two-pass pipeline:
  1. Pre-filter  — hard disqualifiers that short-circuit scoring entirely
  2. Tier scorer — Claude applies the rubric and outputs a tier + rationale
  3. kNN         — cosine similarity against labeled historical candidates
                   used as a calibration/confidence signal alongside the tier

Tiers: strong_intro | weak_intro | nurture | senior_redirect | polite_decline | silent_skip
"""
import json
import math
import os
import re
import time

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
EMBED_MODEL    = "gemini-embedding-001"
EMBED_URL      = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent"
CHAT_MODEL     = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
CHAT_URL       = f"https://generativelanguage.googleapis.com/v1beta/models/{CHAT_MODEL}:generateContent"

TIERS = ["strong_intro", "weak_intro", "nurture", "senior_redirect", "polite_decline", "silent_skip"]
TIER_LABELS = {
    "strong_intro":     "Strong Intro",
    "weak_intro":       "Weak Intro",
    "nurture":          "Nurture",
    "senior_redirect":  "Senior Redirect",
    "polite_decline":   "Polite Decline",
    "silent_skip":      "Silent Skip",
}
# Numeric label mapping for kNN (higher = better fit)
TIER_TO_LABEL = {
    "strong_intro": 10,
    "weak_intro": 7,
    "nurture": 5,
    "senior_redirect": 4,
    "polite_decline": 2,
    "silent_skip": 1,
}
LABEL_TO_TIER = {v: k for k, v in TIER_TO_LABEL.items()}

# Shapes candidate text for embedding — surfaces the axes the rubric cares about
ENRICH_SYSTEM_PROMPT = """You are condensing an SDR candidate profile into a screening summary for embedding and similarity search.

Output 3–4 sentences in one paragraph. Cover in order:
1. Graduation year and school (name the school).
2. Current or most recent employer and title.
3. Location and NYC status — are they in NYC, open to NYC, or not?
4. Career background type — finance/IB/consulting, sales, CS/tech, or other.
5. Tenure signals — time in current role; any job-hopping pattern.
6. Athlete signal — varsity sport or captain/MVP, only if explicitly stated.
7. Message quality — if they wrote a message: was it specific and authentic, generic/motivational filler, or AI-written?

Rules:
- One paragraph, no headers, no bullets, no markdown.
- Only include what is explicitly stated — never infer or fabricate.
- Omit fields that have no signal rather than writing "not stated".
- Use precise repeatable terminology so similar profiles cluster together."""

RUBRIC_SYSTEM_PROMPT = """You are a recruiting assistant for TopSDRs, a firm that places entry-level SDRs at tech startups.

SCORING RUBRIC (apply in this order):

STEP 1 — HARD PRE-FILTERS (any one of these = polite_decline immediately, skip remaining steps):
- Graduation year 2017 or earlier (unless clear "Prospect" signal — i.e., they could be a buyer, not a candidate)
- 10+ years of experience AND non-notable school AND non-brand career
- Living abroad with no stated NYC relocation plan
- <6 months in current role AND "open to work" / actively job-seeking

STEP 2 — TIER SCORING (after pre-filters pass):

strong_intro: Top school + brand-name finance/consulting/IB employer OR already at a known feeder company (PitchBook, Rippling, Ramp, Drata, Brex, Attentive, AlphaSigns, Navan, Wunderkind, ZoomInfo) + class of 2022 or later + in NYC or actively open to NYC. Athlete signal (varsity, captain/MVP) is a positive bump. Specific/authentic message can elevate a borderline profile.

weak_intro: Good school or brand employer but one dimension is missing (e.g., class of 2021, or only "open to NYC" rather than already there, or finance background but less-known firm). Still worth a reach-out.

nurture: Decent background but multiple softer signals — no brand employer, state school, NYC interest is vague. Worth keeping in the pipeline but not a priority intro.

senior_redirect: Real background but too senior (5+ years, clear upward trajectory at a brand firm). Leave the door open for a chat but this isn't the placement track.

polite_decline: Doesn't meet the profile — CS/tech background, wrong geography with no plan to move, pre-2018 grad with no special context. We always reply to real humans.

silent_skip: Obvious spam, content-marketer cross-promo, or clearly automated submissions.

MESSAGE FIELD ASYMMETRY (apply after base tier is set):
- Blank or short message → neutral, no adjustment
- Specific/authentic/idiosyncratic message → can elevate a borderline profile by one tier (e.g., nurture → weak_intro)
- AI-slop/generic motivational filler → deduct one tier (e.g., weak_intro → nurture). The CASH acronym, "leveraging my skills", "excited to add value" patterns are red flags.

OUTPUT: Return JSON with exactly these keys:
{
  "tier": "<one of: strong_intro | weak_intro | nurture | senior_redirect | polite_decline | silent_skip>",
  "rationale": "<2–3 sentences explaining the decision, referencing specific signals>",
  "message_class": "<specific | ai_slop | neutral>"
}"""


# ---------------------------------------------------------------------------
# Pre-filter (fast hard gates before we call any LLM)
# ---------------------------------------------------------------------------

def pre_filter(c: dict) -> tuple[bool, str]:
    """Returns (passes, reason). If passes=False, candidate is auto-declined."""
    grad_year = c.get("graduation_year")
    if grad_year and int(grad_year) <= 2017:
        return False, f"Graduation year {grad_year} is pre-2018 (auto-filter)"

    nyc_open = c.get("nyc_open")
    # False explicitly means they said no to NYC
    if nyc_open is False:
        return False, "Not in NYC and not open to relocating (auto-filter)"

    return True, ""


# ---------------------------------------------------------------------------
# Gemini enrichment + embedding
# ---------------------------------------------------------------------------

def _candidate_to_structured(c: dict) -> str:
    fields = []
    if c.get("full_name"):        fields.append(f"Name: {c['full_name']}")
    if c.get("college"):          fields.append(f"School: {c['college']}")
    if c.get("graduation_year"):  fields.append(f"Graduation year: {c['graduation_year']}")
    if c.get("current_company"):  fields.append(f"Current company: {c['current_company']}")
    if c.get("current_title"):    fields.append(f"Current title: {c['current_title']}")
    if c.get("nyc_open") is not None:
        fields.append(f"NYC: {'Yes' if c['nyc_open'] else 'No'}")
    if c.get("how_found"):        fields.append(f"How they found us: {c['how_found']}")
    if c.get("message"):          fields.append(f"Message: {c['message']}")
    return "\n".join(fields)


def _call_gemini_chat(system_prompt: str, user_text: str) -> str | None:
    """Call Gemini chat with retry. Returns response text or None."""
    if not GEMINI_API_KEY or not user_text.strip():
        return None
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 400},
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
            return body["candidates"][0]["content"]["parts"][0]["text"].strip() or None
        except Exception as e:
            if attempt < 2:
                time.sleep(delay)
            else:
                print(f"[gemini_chat] final failure: {e}")
                return None


def gemini_enrich(c: dict) -> str | None:
    """Rewrite candidate into embedding-friendly summary."""
    return _call_gemini_chat(ENRICH_SYSTEM_PROMPT, _candidate_to_structured(c))


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


def score_candidate_rubric(c: dict) -> tuple[str, str, str]:
    """Apply the rubric via Gemini and return (tier, rationale, message_class)."""
    structured = _candidate_to_structured(c)
    text = _call_gemini_chat(RUBRIC_SYSTEM_PROMPT, structured)
    if not text:
        return "nurture", "Could not score — defaulting to nurture", "neutral"
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        tier         = parsed.get("tier", "nurture")
        rationale    = parsed.get("rationale", "")
        message_class = parsed.get("message_class", "neutral")
        if tier not in TIERS:
            tier = "nurture"
        return tier, rationale, message_class
    except Exception as e:
        print(f"[rubric] parse error: {e} — raw: {text[:200]}")
        return "nurture", f"Parse error: {text[:100]}", "neutral"


# ---------------------------------------------------------------------------
# kNN (similarity against labeled historical candidates)
# ---------------------------------------------------------------------------

def candidate_to_text(c: dict) -> str:
    """Serialize for embedding. Prefers enriched_text."""
    enriched = (c.get("enriched_text") or "").strip()
    if enriched:
        name = (c.get("full_name") or "").strip()
        return f"{name}. {enriched}" if name else enriched
    return _candidate_to_structured(c)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def knn_neighbors(target_embedding, labeled_rows, k=3) -> str:
    """Return a human-readable string of the closest labeled candidates."""
    if not labeled_rows or target_embedding is None:
        return ""
    scored = [(_cosine(target_embedding, r["embedding"]), r) for r in labeled_rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    seen, top = set(), []
    for sim, r in scored:
        name = r.get("full_name") or str(r.get("id"))
        if name in seen:
            continue
        seen.add(name)
        top.append((sim, r))
        if len(top) >= k:
            break
    if not top:
        return ""
    parts = [f"{r.get('full_name','?')} ({TIER_LABELS.get(r.get('tier',''), r.get('label','?'))}, {int(sim*100)}%)"
             for sim, r in top]
    return "Similar to: " + ", ".join(parts)


def knn_neighbors_from_db(target_embedding, db_conn, k=3) -> str:
    """Pull labeled candidates from DB and return neighbor string."""
    if target_embedding is None:
        return ""
    import psycopg2.extras
    cur = db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT c.id, c.full_name, c.embedding, c.tier, cf.label
          FROM candidates c
          JOIN candidate_feedback cf ON cf.candidate_id = c.id
         WHERE c.embedding IS NOT NULL
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    for r in rows:
        r["embedding"] = list(r["embedding"])
    return knn_neighbors(target_embedding, rows, k=k)
