# Deployment Guide

## 1. Deploy on Railway

- Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
- Select `TopSDRs-Tools`
- Railway detects the `Procfile` and builds automatically

## 2. Add PostgreSQL

In the Railway project → **+ New** → **Database** → **PostgreSQL**

Railway automatically sets `DATABASE_URL` as an environment variable. The app runs `schema.sql` on first boot — no manual setup needed.

## 3. Set Environment Variables

In Railway → your service → **Variables**, add:

| Variable | Where to get it |
|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) → Get API key |
| `RESEND_API_KEY` | [resend.com](https://resend.com) → API Keys |
| `OUTREACH_FROM_EMAIL` | The email address you want to send from (must be verified in Resend) |
| `OUTREACH_SENDER_NAME` | Your first name (appears in email sign-off) |
| `CALENDLY_LINK` | Your Calendly booking URL |
| `AIRTABLE_API_KEY` | [airtable.com/account](https://airtable.com/account) → Personal access tokens → Create token (needs `data.records:write` scope) |
| `AIRTABLE_BASE_ID` | Open your Airtable base → the URL contains `app...` — that's the base ID |
| `AIRTABLE_TABLE_NAME` | The exact name of your candidates table in Airtable |

`DATABASE_URL` is set automatically by Railway when you add PostgreSQL — do not set it manually.

## 4. Set Up Resend

1. Sign up at [resend.com](https://resend.com)
2. **Add a domain** (e.g. `topsdrs.com`) → follow DNS verification steps (~5 min)
3. Once verified, use `hello@topsdrs.com` (or similar) as `OUTREACH_FROM_EMAIL`
4. **No domain yet?** Use Resend's sandbox sender for testing — emails only deliver to your own verified address

## 5. Connect Webflow

1. In Railway, find your public URL (e.g. `topsdrs-tools-production.up.railway.app`)
2. In Webflow: **Site Settings** → **Forms** → **Webhooks** → Add webhook
3. Set the URL to: `https://topsdrs-tools-production.up.railway.app/webhook/webflow`
4. That's it — every form submission fires automatically

> **Note:** The webhook endpoint has no authentication. It accepts any POST from Webflow. If you want to lock it down later, set `WEBFLOW_WEBHOOK_SECRET` in Railway and configure the matching secret in Webflow's webhook settings.

## 6. Set Up Airtable

Your Airtable table needs these columns (exact names matter):

| Column | Type |
|---|---|
| Name | Single line text |
| Email | Email |
| LinkedIn | URL |
| College | Single line text |
| Graduation Year | Single line text |
| Current Company | Single line text |
| Current Title | Single line text |
| NYC Open | Single line text |
| Tier | Single line text |
| Tier Rationale | Long text |
| Outreach Subject | Single line text |
| Outreach Sent | Single line text |
| Source | Single line text |

## 7. Invite a Collaborator

Railway → Project **Settings** → **Members** → **Invite** → enter their email.
They get full dashboard access to view logs, edit variables, and redeploy.

## No Cron Needed

Everything is event-driven:
- Webflow fires the webhook → candidate saved → Gemini scores in the background
- No polling, no scheduled jobs

## Running Locally

```bash
cp .env.example .env
# fill in .env with your keys
DATABASE_URL=postgresql://localhost/topsdrs_local python app.py
```
