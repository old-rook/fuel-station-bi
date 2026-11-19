# Technical Specification (`SPEC.md`): Fuel Station BI (PoC)

**Architecture:** React + Tailwind CSS SPA + Python FastAPI Serverless API on Vercel (Hobby/Lite) + Cloud PostgreSQL + Gmail OAuth 2.0

---

## 1. System Architecture & Vercel Lite PoC Constraints

### Why This Architecture
Vercel Hobby (Lite) is serverless and stateless, good for a PoC:
- **No persistent local disk:** SQLite cannot be used on Vercel; **Neon** or **Supabase** (managed PostgreSQL) provides persistent storage and easy `pg_dump`/CSV exportability.
- **Short serverless timeouts:** Hobby functions have tight execution limits (default 10s, configurable up to 60s via `maxDuration` in `vercel.json` depending on plan/Fluid Compute). For **20–30 emails/day**, we stay well within limits by:
  1. Fetching only recent/unprocessed Gmail message IDs (`after:<last_sync_timestamp>`).
  2. Running a lightweight metadata/keyword pre-filter before calling the LLM.
  3. Processing emails in small batches (e.g., max 10 LLM calls per invocation, with `next_cursor` or concurrent `asyncio` calls using `httpx`).
- **Cron limit on Hobby:** Vercel Hobby allows **1 cron job execution per day** (UTC). A manual **"Sync Now"** button in the UI supplements this for ad-hoc testing.

### High-Level Component Diagram
```text
+-------------------+       HTTPS        +-----------------------------------+
|  React + Tailwind | <----------------> |  Vercel Serverless (Python/FastAPI)|
|  (Recharts,       |                    |  - /api/auth/*                    |
|   pptxgenjs,      |                    |  - /api/cron/sync-emails          |
|   html-to-image)  |                    |  - /api/dashboard/*               |
+-------------------+                    |  - /api/reviews/*                 |
                                         |  - /api/chat/dashboard            |
                                         +---------+---------------+---------+
                                                   |               |
                                  OAuth 2.0        |               | SQL / LLM API
                                                   v               v
                                         +--------------+   +---------------+
                                         |  Gmail API   |   | Cloud Postgres|
                                         |  (Inbox +    |   | (Neon /       |
                                         |   Notify)    |   |  Supabase)    |
                                         +--------------+   +---------------+
```

## 2. Repository Structure

```tree
fuel-station-bi/
├── frontend/                   # React + Vite + Tailwind CSS
│   ├── src/
│   │   ├── components/
│   │   │   ├── charts/         # Recharts wrappers (Bar, Line, Donut)
│   │   │   ├── ExportMenu.tsx  # PNG, PDF, PPTX export logic
│   │   │   ├── ReviewCard.tsx  # Side-by-side email vs extracted form
│   │   │   └── ChatPanel.tsx   # Custom dashboard builder UI
│   │   ├── pages/
│   │   │   ├── Login.tsx
│   │   │   ├── DailyDashboard.tsx
│   │   │   ├── ReviewQueue.tsx
│   │   │   ├── CustomDashboards.tsx
│   │   │   └── Settings.tsx    # Gmail OAuth status, Manual Sync, Data Export
│   │   ├── lib/api.ts
│   │   └── App.tsx
│   ├── package.json
│   └── vite.config.ts
├── api/                        # Python FastAPI mounted on Vercel
│   ├── index.py                # FastAPI entrypoint
│   ├── core/
│   │   ├── config.py           # Env vars & settings
│   │   ├── db.py               # SQLAlchemy / psycopg3 pool
│   │   └── security.py         # JWT auth & CRON_SECRET verification
│   ├── services/
│   │   ├── gmail_service.py    # OAuth2 token refresh, fetch, send alert
│   │   ├── extractor.py        # LLM structured extraction & ambiguity rules
│   │   ├── chat_bi.py          # Intent JSON -> safe parameterized SQL
│   │   └── notify.py           # Ambiguity alert dispatcher
│   ├── routers/
│   │   ├── auth.py
│   │   ├── cron.py
│   │   ├── dashboard.py
│   │   ├── reviews.py
│   │   └── chat.py
│   └── requirements.txt
├── vercel.json
├── PRD.md
└── SPEC.md
```

## 3. Database Schema (PostgreSQL)
```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
```

### `branches`
```sql
CREATE TABLE branches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,           -- e.g., 'Branch X', 'Main Highway'
    aliases TEXT[] DEFAULT '{}',         -- e.g., {'Br. X', 'Station 1'}
    location TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### `fuel_products`
```sql
CREATE TABLE fuel_products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,           -- e.g., 'Unleaded', 'Diesel', 'Premium'
    aliases TEXT[] DEFAULT '{}',
    unit TEXT NOT NULL DEFAULT 'liters', -- 'liters' or 'gallons'
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### `email_logs`
```sql
CREATE TABLE email_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_message_id TEXT NOT NULL UNIQUE,
    gmail_thread_id TEXT,
    sender TEXT NOT NULL,
    subject TEXT,
    raw_body TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'processed',        -- Auto-committed to daily_sales
        'pending_review',   -- Ambiguous; waiting for owner action
        'approved',         -- Manually approved & committed
        'rejected',         -- Manually discarded
        'ignored'           -- Non-sales email
    )),
    confidence_score NUMERIC(3,2),
    ambiguity_reasons JSONB DEFAULT '[]'::jsonb,
    extracted_payload JSONB,             -- Proposed records before/after approval
    processed_at TIMESTAMPTZ DEFAULT NOW()
);
```

### `daily_sales`
```sql
CREATE TABLE daily_sales (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID NOT NULL REFERENCES branches(id),
    product_id UUID NOT NULL REFERENCES fuel_products(id),
    sale_date DATE NOT NULL,
    volume_sold NUMERIC(12,2) NOT NULL CHECK (volume_sold >= 0),
    revenue_amount NUMERIC(14,2) NOT NULL CHECK (revenue_amount >= 0),
    source_email_id UUID REFERENCES email_logs(id) ON DELETE SET NULL,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (branch_id, product_id, sale_date, source_email_id)
);
```

```sql
CREATE INDEX idx_daily_sales_date ON daily_sales(sale_date);
CREATE INDEX idx_daily_sales_branch_date ON daily_sales(branch_id, sale_date);
```

### `custom_dashboards`
```sql
CREATE TABLE custom_dashboards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    spec_json JSONB NOT NULL,            -- Validated DashboardSpec JSON
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```
```sql
CREATE TABLE app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
-- Stores e.g. 'last_gmail_sync_epoch', 'gmail_refresh_token' (encrypted)
```

## 4. Gmail OAuth 2.0 Integration & Sync Pipeline

### 4.1 OAuth 2.0 Setup (PoC One-Time Authorization)
Create a Google Cloud Project, enable Gmail API, and configure OAuth Consent Screen (set to Testing and add your personal Gmail as a Test User—no Google app verification required for personal PoC).

Create an OAuth 2.0 Web Client ID with redirect URI: `https://<your-app>.vercel.app/api/auth/gmail/callback`.

Request scopes:

`https://www.googleapis.com/auth/gmail.readonly` (read inbox)

`https://www.googleapis.com/auth/gmail.send` (send ambiguity notification emails to yourself)
Upon clicking "Connect Gmail" in Settings, exchange the authorization code for a refresh_token. Store the refresh_token encrypted (via Fernet/AES using `APP_SECRET_KEY`) in app_state or as a Vercel Environment Variable (`GMAIL_REFRESH_TOKEN`).


_Note on Google OAuth "Testing" Mode: Refresh tokens in Testing mode expire after 7 days unless you publish the app (which can remain unverified if only you authorize it with the "Advanced -> Continue" warning) or re-auth periodically. Publishing as "In Production" without sensitive scope verification works for a single personal account with the unverified warning screen, keeping the refresh token long-lived._

### 4.2 Daily Sync & Timeout Guard (`POST /api/cron/sync-emails`)
**Auth Check:** Require either Authorization: `Bearer <CRON_SECRET>` (Vercel Cron) or valid owner JWT (manual sync).

**Fetch Candidate IDs:** Query Gmail users.messages.list with `q="after:<last_sync_epoch - 3600>" `and `maxResults=30.`

**Deduplicate:** Filter out any `gmail_message_id` already present in email_logs.

**Lightweight Pre-Filter:** Skip obvious automated/non-sales emails (e.g., `CATEGORY_PROMOTIONS`, no-reply marketing headers) and log as ignored without calling the LLM.

**Concurrent Extraction (asyncio):** Process up to 10 emails per batch concurrently via httpx to stay well under Vercel serverless timeouts. If >10 new emails remain, `return { "remaining": N }` so the frontend manual sync can call again or cron processes incrementally.

### 4.3 LLM Extraction & Ambiguity Rules (extractor.py)
Use `OpenAI (gpt-4o-mini)` or `Anthropic (claude-3-5-haiku)` with Strict JSON Schema Structured Outputs. Pass known branches and fuel_products (with aliases) in the system prompt.

```json
{
  "is_sales_report": true,
  "confidence_score": 0.92,
  "ambiguity_reasons": [],
  "records": [
    {
      "branch_name": "Branch X",
      "sale_date": "2026-04-10",
      "product_name": "Diesel",
      "volume_sold": 4500.5,
      "revenue_amount": 292532.5
    }
  ]
}
```

### 4.4 Deterministic Validation Gate (runs in Python after LLM response):
Flag as pending_review if any of the following hold:

- `confidence_score < 0.85`
- `l`en(ambiguity_reasons) > 0`
- `branch_name` does not match any known branch/alias in PostgreSQL
product_name does not match any known fuel product/alias
- `sale_date` is missing or in the future relative to received_at
- `volume_sold <= 0 ` or `revenue_amount <= 0` (when `is_sales_report == true`)

#### Notification Trigger:
If `pending_review_count > 0` after a sync run, send one summary email to the owner's Gmail via gmail.send with subject:

`[Fuel Station BI] Action Required: {N} ambiguous sales email(s) need review containing a direct link to /reviews.`

## 5. API Endpoints Specification

### Auth & Settings

`POST /api/auth/login` — Validates owner password hash, returns HTTP-only cookie / JWT.

`GET /api/auth/gmail/url` — Returns Google OAuth2 consent URL.

`GET /api/auth/gmail/callback` — Stores Gmail refresh token.

`GET /api/settings/status` — Returns Gmail connection status, last sync time, pending review count.

`GET /api/export/raw?format=csv|json` — Streams full daily_sales joined with branches and fuel_products.

### Sync & Cron

`GET /api/cron/sync` -emails — Called daily by Vercel Cron (CRON_SECRET protected).

`POST /api/sync/manual` — Triggered by owner from UI; returns { processed, pending_review, ignored, remaining }.

### Daily Dashboard
`GET /api/dashboard/daily?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&branch_ids=...&product_ids=...`
- Returns:
  - `kpis`: `{ total_revenue, total_volume, top_branch, revenue_delta_pct, volume_delta_pct }`
  - `time_series`: `[{ date, revenue, volume }]`
  - `by_branch`: `[{ branch_name, revenue, volume }]`
  - `by_product`: `[{ product_name, revenue, volume }]`
  - `rows`: `Detailed table rows.`

### Ambiguous Email Review Queue

`GET /api/reviews/pending` — Lists email_logs where status = 'pending_review'.

`POST /api/reviews/{email_log_id}/approve` — Accepts edited records[] payload, inserts into daily_sales, updates status to approved.

`POST /api/reviews/{email_log_id}/reject` — Marks status as rejected (no sales inserted).

### AI Chat & Custom Dashboards

`POST /api/chat/dashboard` 
- Body: `{ "prompt": "show me sales for Branch X for Q3 2026" }`
- Returns `{ spec, summary_text, data }`

`GET /api/dashboards` — Lists saved custom dashboards.

`POST /api/dashboards` — Saves a generated spec_json + title + prompt_text.

`GET /api/dashboards/{id}/data` — Re-runs the saved spec_json against live Postgres data.

`DELETE /api/dashboards/{id}` — Deletes a saved dashboard.

## 6. Safe Chat-to-Dashboard Engine (`chat_bi.py`)
To eliminate SQL injection and LLM syntax errors, the LLM never writes raw SQL. Instead, it outputs a strict DashboardSpec JSON:
```json
{
  "title": "Branch X Sales — Q3 2026",
  "date_range": {
    "start": "2026-07-01",
    "end": "2026-09-30"
  },
  "branches": ["Branch X"],
  "products": [],
  "group_by": "month",
  "metrics": ["revenue_amount", "volume_sold"],
  "chart_type": "bar",
  "empty_state_hint": "No sales recorded for Branch X in Q3 2026 yet."
}
```

### Execution Flow:
1. Fetch current `branches` and `fuel_products` names from Postgres and inject into LLM prompt.
2. Validate LLM output against a Pydantic `DashboardSpec` model (`group_by enum: day | week | month | branch | product; chart_type enum: bar | line | area | pie | table`).
3. Build a **parameterized SQLAlchemy/psycopg query** using the validated spec.
4. Return aggregated results + KPI totals + natural-language summary.

## 7. Frontend & Multi-Format Export Architecture

### UI Stack
- **React 18 + Vite + TypeScript + Tailwind CSS**
- **Charts**: `recharts` (responsive, clean SVG output for reliable image capture)
- **Icons**: `lucide-react`

### Export Implementations (`ExportMenu.tsx`)

Every dashboard view wraps its printable area in a `React ref={dashboardRef}:`

1. **Image (`.png`):**
- Uses `html-to-image (toPng(dashboardRef.current, { pixelRatio: 2, backgroundColor: '#ffffff' }))` and triggers browser download.

2. **PDF (`.pdf`):**
- Uses `html-to-image (toPng) + jspdf (new jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a4' }))`, adds header title + export timestamp, scales dashboard image to fit page cleanly, and saves `.pdf`.

3. **PowerPoint (`.pptx`):**
- Uses `pptxgenjs` in the browser:
 - **Slide 1 (Title & KPIs)**: Dashboard title, date range, and formatted KPI summary boxes.
 - **Slide 2 (Visual Chart)**: Embeds the high-res PNG capture of the charts or native editable `pptx.addChart(...)` built from the API JSON data.
 - **Slide 3 (Data Table)**: Native editable PowerPoint table `(slide.addTable(...))` with the underlying sales breakdown rows.

 ## 8. Vercel Configuration & Environment Variables

`vercel.json`
```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "buildCommand": "cd frontend && npm install && npm run build",
  "outputDirectory": "frontend/dist",
  "functions": {
    "api/index.py": {
      "maxDuration": 30
    }
  },
  "rewrites": [
    { "source": "/api/(.*)", "destination": "/api/index.py" },
    { "source": "/(.*)", "destination": "/index.html" }
  ],
  "crons": [
    {
      "path": "/api/cron/sync-emails",
      "schedule": "0 23 * * *"
    }
  ]
}
```
_(Adjust schedule in UTC to match your desired local daily run time, e.g., 0 23 * * * = 7:00 AM UTC+8.)_

### Required Environment Variables (Vercel Dashboard)

|**Variable**|**Purpose**|
|---|---|
| `DATABASE_URL` | Neon or Supabase PostgreSQL pooled connection string |
|`OWNER_PASSWORD_HASH` | Bcrypt hash of your app login password|
|`APP_SECRET_KEY` |	JWT signing & token encryption secret|
|`CRON_SECRET`|	Vercel-provided secret to secure `/api/cron/sync-emails`|
|`GMAIL_CLIENT_ID`|	Google Cloud OAuth Client ID|
|`GMAIL_CLIENT_SECRET`|	Google Cloud OAuth Client Secret|
|`GMAIL_REDIRECT_URI`| `https://<your-app>.vercel.app/api/auth/gmail/callback`|
|`OWNER_ALERT_EMAIL`| Your email address for ambiguity notifications|
|`OPENAI_API_KEY` |	LLM API key for extraction & chat BI|

## 9. PoC Testing & Verification Plan

1. **Seed Data Test**: Seed 3 branches (`Branch X, Branch Y, Branch Z`) and 3 fuel products (`Unleaded, Diesel, Premium`) plus 14 days of sample sales to verify Daily Dashboard + Exports immediately.

2. **Gmail Ingestion Test Matrix**:
 - _Case A (Clean)_: `"Branch X Daily Report 2026-04-10: Unleaded 3200L = $4800, Diesel 5100L = $7140"` -> Expect `status='processed'`, dashboard updated.
 - _Case B (Ambiguous)_: `"Station near highway sold around 40k of gas today, will confirm liters tomorrow"` -> Expect `status='pending_review'`, alert email sent, dashboard unchanged until approved.
 - _Case C (Noise)_: Newsletter / promotional email -> Expect `status='ignored'`, no alert.
 - _Case D (Duplicate)_: Re-running manual sync -> Expect 0 duplicate `daily_sales` rows.

3. **Chat BI Test**:
 - Query `"Show me sales for Branch X for Q3 2026"` -> Expect valid empty-state or chart for `2026-07-01` to `2026-09-30` without errors.
 - Save dashboard -> Reload page -> Confirm persisted and re-queried from Postgres.

4. **Export Verification**: Open exported `.png`, `.pdf`, and `.pptx` in Preview/Adobe/PowerPoint/Google Slides to confirm legible typography, non-clipped charts, and editable PPTX tables.