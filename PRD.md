# Product Requirements Document (PRD): Fuel Station BI

**Version:** 1.0 (Proof of Concept)  
**Target Scale:** 1 Business Owner, Multiple Branches, ~20–30 emails/day
**Stack:** React + Tailwind CSS, Python (FastAPI), Cloud PostgreSQL, Gmail OAuth 2.0, Vercel (Hobby/Lite)

---

## 1. Executive Summary
**Fuel Station BI** is a personal business intelligence web application for managing and analyzing a gasoline station business. It automates daily sales reporting by reading incoming Gmail messages, extracting structured sales figures (branch, fuel product, volume, revenue, date), updating an interactive daily dashboard, and holding ambiguous reports for human verification before they affect metrics. It also provides an AI chat interface to generate custom ad-hoc dashboards and supports exporting any dashboard to PNG, PDF, or PowerPoint (PPTX).

---

## 2. Goals & Non-Goals

### Goals (PoC Scope)
1. **Automated Daily Ingestion:** Read up to 30 new emails/day from a connected Gmail inbox via Gmail OAuth 2.0.
2. **Accurate Data Extraction:** Parse sales reports into structured records (branch, fuel type, volume, revenue, date).
3. **Human-in-the-Loop (HITL) Safeguard:** Detect vague, incomplete, or conflicting emails, notify the owner, and require manual approval before adding them to dashboard metrics.
4. **Interactive Daily Dashboard:** Provide a clean, filterable daily overview of station performance.
5. **Conversational Custom Dashboards:** Allow natural-language requests (e.g., *"Show me sales for Branch X for Q3 2026"*) to generate and save custom dashboard views.
6. **Multi-Format Exports:** Export any daily or custom dashboard as an **Image (PNG)**, **PDF**, or **PowerPoint (.pptx)**.
7. **Portability:** Store all business data in standard PostgreSQL so the full dataset can be exported (SQL/CSV/JSON) at any time.

### Non-Goals (Out of Scope for PoC)
- Multi-tenant / multi-organization user management (single-owner app).
- Real-time streaming ingestion (daily batch + manual "Sync Now" trigger is sufficient).
- Automated POS hardware integration or inventory/supply-chain procurement.
- Processing massive attachment batches (>30 emails/day or multi-megabyte scanned PDFs).

---

## 3. Target User & Personas
- **Owner / Operator (Primary & Only User):** Needs a quick daily morning/evening check of station revenue and fuel volumes, alerts only when a manager sends an unclear report, and fast ways to slice data or export slides for partners/accountants.

---

## 4. Functional Requirements

### FR-1: Gmail Inbox Ingestion & Extraction
- **FR-1.1:** Connect securely to the owner's Gmail account using OAuth 2.0 (read-only scope `gmail.readonly` for ingestion; optional `gmail.send` or separate SMTP/Resend for notifications).
- **FR-1.2:** Run automatically once per day via scheduled cron, plus support an on-demand **"Sync Inbox Now"** button in the UI.
- **FR-1.3:** Deduplicate emails using Gmail `message_id` so messages are never processed twice.
- **FR-1.4:** Extract: `sale_date`, `branch_name`, `fuel_product` (e.g., Unleaded, Diesel, Premium), `volume_sold`, `revenue_amount`, and `currency`.
- **FR-1.5:** Ignore non-sales emails (promos, receipts unrelated to station sales, newsletters) without cluttering the review queue.

### FR-2: Ambiguity Detection & Notification Workflow
- **FR-2.1:** Each extracted sales email receives a confidence score and validation check (missing date, unknown branch, mismatched totals, vague wording like *"about 50k today"*).
- **FR-2.2:** If **valid & high confidence**, automatically commit records to the sales database.
- **FR-2.3:** If **ambiguous or incomplete**, save the email in a `pending_review` state and **do not** update the dashboard totals yet.
- **FR-2.4:** Notify the owner immediately when 1+ emails enter `pending_review` (via alert email and an in-app notification badge).
- **FR-2.5:** Provide a **Review Queue** UI showing the original email side-by-side with editable extracted fields and actions: **Approve & Commit**, **Edit & Commit**, or **Discard/Ignore**.

### FR-3: Daily Interactive Dashboard
- **FR-3.1:** Display headline KPIs: Total Revenue, Total Volume Sold, Top Performing Branch, and % Change vs. Previous Period.
- **FR-3.2:** Interactive filters: Date range picker, Branch multi-select, Fuel Product multi-select.
- **FR-3.3:** Core visualizations:
  - Daily Revenue & Volume Trend (Line/Area Chart)
  - Sales by Branch (Bar Chart)
  - Sales Mix by Fuel Product (Donut/Bar Chart)
  - Detailed Daily Breakdown Table (sortable, searchable).

### FR-4: AI Chat for Custom Dashboards
- **FR-4.1:** Chat drawer/view where the user types natural-language analytics queries (e.g., *"Show me sales for Branch X for Q3 2026"*, *"Compare Diesel vs Unleaded across all branches last month"*).
- **FR-4.2:** Generate a dynamic dashboard card/view containing the matching chart(s), summary KPIs, and natural-language takeaway.
- **FR-4.3:** Gracefully handle empty/future date ranges (e.g., Q3 2026 if no data exists yet) with a clear empty-state message rather than an error.
- **FR-4.4:** Allow saving/pinning generated dashboards for future viewing and exporting.

### FR-5: Dashboard Exports
- **FR-5.1:** Both the Daily Dashboard and any Custom Dashboard must have an **Export** menu with three options:
  - **PNG Image:** High-resolution snapshot of the dashboard.
  - **PDF Document:** Formatted landscape report with title, filter context, timestamp, and charts.
  - **PowerPoint (.pptx):** Editable slide deck containing title slide, KPI summary, and embedded charts/data tables.

### FR-6: Security & Data Exportability
- **FR-6.1:** Protect the web app behind owner authentication (login required to view data, approve emails, or use chat).
- **FR-6.2:** Provide a one-click **Export Raw Data (CSV / JSON)** feature in Settings alongside standard PostgreSQL backup support.

---

## 5. Key User Flows

1. **Daily Automated Flow:**
   - Cron triggers at scheduled time -> Fetches new emails from last 24–48h -> Pre-filters & extracts via LLM -> Clean reports update Daily Dashboard -> Ambiguous reports trigger notification email + in-app badge.
2. **Ambiguous Email Resolution Flow:**
   - Owner receives notification -> Clicks link to Review Queue -> Sees raw email ("*Branch 2 did ~45k diesel yesterday, forgot exact liters*") -> Edits/confirms values -> Clicks **Approve** -> Daily Dashboard immediately reflects updated totals.
3. **Custom Dashboard via Chat Flow:**
   - Owner opens Chat -> Types *"Show me sales for Branch A for Q3 2026"* -> System parses intent, queries Postgres, renders custom bar/line chart -> Owner clicks **Save Dashboard** or **Export as PPTX**.

---

## 6. Non-Functional Requirements & Constraints (Vercel Lite PoC)
- **Performance & Serverless Limits:** Daily sync of 20–30 emails must complete within Vercel Hobby serverless execution limits (or gracefully resume via batching if needed).
- **Cost:** Must operate comfortably on free/low-cost tiers (Vercel Hobby, Neon/Supabase Free Postgres, low-cost LLM like `gpt-4o-mini` or `claude-haiku`).
- **Reliability:** Idempotent ingestion (safe to re-run sync without duplicating sales rows).
- **Security:** OAuth tokens, DB credentials, and LLM keys stored strictly in Vercel Environment Variables; never exposed to the browser.

---

## 7. Acceptance Criteria
1. Connecting Gmail via OAuth 2.0 successfully reads unread/recent emails from the inbox.
2. Sending 3 test emails (clean sales report, non-sales newsletter, vague sales email) results in:
   - Clean email auto-added to Daily Dashboard.
   - Newsletter silently ignored in logs.
   - Vague email held in Review Queue and owner notified.
3. Approving the vague email updates dashboard charts in real time.
4. Typing *"Show me sales for Branch X for Q3 2026"* generates a valid custom dashboard view.
5. Exporting both Daily and Custom dashboards produces valid, readable `.png`, `.pdf`, and `.pptx` files.