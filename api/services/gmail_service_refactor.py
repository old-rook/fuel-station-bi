"""
gmail_service.py
----------------
Gmail Inbox Ingestion & Extraction for Fuel Station Sales Reports.

Features:
- Headless / non-interactive OAuth2 & Service Account authentication with secure token storage (0600).
- Atomic SQLite transactions with WAL mode, foreign keys, and idempotency guarantees.
- Safe MIME traversal with base64 padding normalization and HTML tag stripping.
- Resilient line-by-line heuristic parser and CSV reader supporting negative values.
- Explicit validation: records lacking dates or station identifiers are routed to an exception state
  rather than silently populated with fallback defaults.
- Gmail API pagination handling with configurable search queries.
"""

import base64
import csv
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple

from api.utlis.html_helper import strip_html
from api.models.gmail_datamodels import FuelSaleRecord, SyncSummary

# Google API Client Libraries
try:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import Resource, build
    from googleapiclient.errors import HttpError
except ImportError:
    Credentials = None  # type: ignore
    service_account = None  # type: ignore
    InstalledAppFlow = None  # type: ignore
    Resource = None  # type: ignore
    build = None  # type: ignore
    HttpError = Exception  # type: ignore

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration Constants (Environment-Overridable)
# -----------------------------------------------------------------------------

SCOPES: Sequence[str] = ["https://www.googleapis.com/auth/gmail.readonly"]

DEFAULT_TOKEN_FILE = os.getenv("GMAIL_TOKEN_PATH", os.path.abspath("token.json"))
DEFAULT_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_PATH", os.path.abspath("credentials.json"))
DEFAULT_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
DEFAULT_DB_FILE = os.getenv("APP_DB_PATH", os.path.abspath("fuel_sales.db"))
DEFAULT_GMAIL_QUERY = os.getenv("GMAIL_SEARCH_QUERY", "in:inbox")

# -----------------------------------------------------------------------------
# Storage & Deduplication Engine (SQLite)
# -----------------------------------------------------------------------------

class IngestionStore:
    """Thread-safe SQLite storage enforcing atomic transactions and foreign keys."""

    def __init__(self, db_path: str = DEFAULT_DB_FILE):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    subject TEXT,
                    sender TEXT,
                    is_sales_report INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PROCESSED', 'IGNORED', 'QUARANTINED', 'FAILED'))
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fuel_sales (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    sale_date TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    fuel_product TEXT NOT NULL,
                    volume_sold REAL NOT NULL,
                    revenue_amount REAL NOT NULL,
                    currency TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (message_id) REFERENCES processed_emails(message_id) ON DELETE CASCADE,
                    CONSTRAINT uq_sale_item UNIQUE (message_id, sale_date, branch_name, fuel_product)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fuel_sales_lookup ON fuel_sales(sale_date, branch_name);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fuel_sales_message ON fuel_sales(message_id);")
            conn.commit()

    def is_message_processed(self, message_id: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,))
            return cursor.fetchone() is not None

    def persist_email_and_records(
        self,
        message_id: str,
        subject: str,
        sender: str,
        status: str,
        is_sales_report: bool,
        records: List[FuelSaleRecord]
    ) -> None:
        """Atomic insert: commits email status and parsed records in a single transaction."""
        now = datetime.now(timezone.utc).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO processed_emails 
                    (message_id, processed_at, subject, sender, is_sales_report, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (message_id, now, subject, sender, int(is_sales_report), status))

                if records:
                    cursor.executemany("""
                        INSERT INTO fuel_sales 
                        (message_id, sale_date, branch_name, fuel_product, volume_sold, revenue_amount, currency, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(message_id, sale_date, branch_name, fuel_product) DO UPDATE SET
                            volume_sold = excluded.volume_sold,
                            revenue_amount = excluded.revenue_amount,
                            currency = excluded.currency
                    """, [
                        (r.raw_message_id, r.sale_date, r.branch_name, r.fuel_product, r.volume_sold, r.revenue_amount, r.currency, now)
                        for r in records
                    ])
                conn.commit()
            except Exception:
                conn.rollback()
                raise


# -----------------------------------------------------------------------------
# Email Filtering & Extraction
# -----------------------------------------------------------------------------

class SalesEmailClassifier:
    """Identifies legitimate sales reports and ignores spam/notifications."""

    SALES_KEYWORDS = {
        "sales", "fuel", "daily report", "end of day", "eod", "meter reading",
        "volume", "liters", "litres", "shift report", "pump", "reconciliation", "dip reading"
    }

    IGNORE_KEYWORDS = {
        "newsletter", "unsubscribe", "promotion", "marketing", "webinar",
        "discount", "order confirmation", "otp", "verify your account", "password reset"
    }

    FUEL_PRODUCTS = {"unleaded", "regular", "diesel", "gasoil", "premium", "v-power", "e10", "autogas", "lpg"}

    @classmethod
    def is_sales_email(cls, subject: str, clean_text: str) -> bool:
        content = f"{subject} {clean_text}".lower()
        if any(ign in content for ign in cls.IGNORE_KEYWORDS):
            return False

        has_sales_kw = any(kw in content for kw in cls.SALES_KEYWORDS)
        has_fuel_kw = any(fuel in content for fuel in cls.FUEL_PRODUCTS)
        return has_sales_kw and has_fuel_kw


class SalesDataExtractor:
    """Robust extraction engine for tabular and semi-structured email data."""

    PRODUCT_MAPPING = {
        "unleaded": "Unleaded",
        "regular": "Unleaded",
        "diesel": "Diesel",
        "gasoil": "Diesel",
        "premium": "Premium",
        "v-power": "Premium",
        "super": "Premium",
        "e10": "E10",
        "lpg": "LPG"
    }

    CURRENCY_SYMBOLS = {
        "₱": "PHP",
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "¥": "JPY",
        "A$": "AUD",
        "C$": "CAD"
    }

    @staticmethod
    def parse_float(val: Optional[str]) -> Optional[float]:
        """Parses numeric values supporting negative signs and European/US decimal formats."""
        if not val:
            return None
        cleaned = val.strip()
        negative = cleaned.startswith("-") or (cleaned.startswith("(") and cleaned.endswith(")"))
        cleaned = re.sub(r"[^\d.,]", "", cleaned)

        if not cleaned:
            return None

        # Determine if European decimal notation (1.234,56) or standard (1,234.56)
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned and "." not in cleaned:
            # If comma is followed by 1 or 2 digits, treat as decimal point
            parts = cleaned.split(",")
            if len(parts[-1]) in (1, 2):
                cleaned = "".join(parts[:-1]) + "." + parts[-1]
            else:
                cleaned = cleaned.replace(",", "")

        try:
            num = float(cleaned)
            return -num if negative else num
        except ValueError:
            return None

    @classmethod
    def extract_date(cls, text: str) -> Optional[str]:
        """Extracts and validates an ISO 8601 formatted date. Does not guess ambiguously."""
        iso_match = re.search(r"\b(20\d{2})[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])\b", text)
        if iso_match:
            y, m, d = iso_match.group(1), iso_match.group(2), iso_match.group(3)
            return f"{y}-{m}-{d}"

        # Textual month representation (e.g. October 24, 2024)
        text_month_match = re.search(
            r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)\s+(\d{1,2}),?\s+(20\d{2})\b",
            text,
            re.IGNORECASE
        )
        if text_month_match:
            raw = f"{text_month_match.group(1)} {text_month_match.group(2)} {text_month_match.group(3)}"
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    pass
        return None

    @classmethod
    def extract_branch(cls, text: str) -> Optional[str]:
        """Extracts branch or station identifier."""
        match = re.search(r"(?:Branch|Station|Location|Site)\s*[:#-]\s*([A-Za-z0-9\s\-]+?)(?:\r?\n|,|$)", text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            if 3 <= len(candidate) <= 60:
                return candidate
        return None

    @classmethod
    def detect_currency(cls, text: str, default: str = DEFAULT_CURRENCY) -> str:
        for symbol, code in cls.CURRENCY_SYMBOLS.items():
            if symbol in text:
                return code
        code_match = re.search(r"\b(PHP|USD|EUR|GBP|CAD|AUD|JPY)\b", text, re.IGNORECASE)
        if code_match:
            return code_match.group(1).upper()
        return default

    @classmethod
    def extract_records(cls, plain_text: str, attachments_csv: List[str], message_id: str) -> List[FuelSaleRecord]:
        combined_text = plain_text + "\n" + "\n".join(attachments_csv)
        sale_date = cls.extract_date(combined_text)
        branch_name = cls.extract_branch(combined_text)
        currency = cls.detect_currency(combined_text)

        # Fail closed: Do not hallucinate date or station assignment
        if not sale_date or not branch_name:
            logger.warning(
                f"Missing critical metadata for msg_id={message_id} "
                f"(Date: {sale_date}, Branch: {branch_name}). Quarantining email."
            )
            return []

        records: List[FuelSaleRecord] = []

        # 1. Parse CSV attachments first
        for csv_content in attachments_csv:
            csv_records = cls._parse_csv_content(csv_content, sale_date, branch_name, currency, message_id)
            records.extend(csv_records)

        if records:
            return records

        # 2. Line-by-line regex parsing for unstructured body text
        for line in plain_text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue

            for raw_keyword, normalized_product in cls.PRODUCT_MAPPING.items():
                if re.search(rf"\b{raw_keyword}\b", line_str, re.IGNORECASE):
                    # Capture volume and revenue pairs on the same line
                    # Matches: "Diesel: 1,500.00 L - ₱75,000.00" or "Unleaded | 500 | 25000"
                    tokens = re.findall(r"[-+]?\d[\d,]*\.?\d*", line_str)
                    if len(tokens) >= 2:
                        vol = cls.parse_float(tokens[0])
                        rev = cls.parse_float(tokens[1])
                        if vol is not None and rev is not None and (vol != 0 or rev != 0):
                            records.append(FuelSaleRecord(
                                sale_date=sale_date,
                                branch_name=branch_name,
                                fuel_product=normalized_product,
                                volume_sold=vol,
                                revenue_amount=rev,
                                raw_message_id=message_id,
                                currency=currency
                            ))
                            break

        return records

    @classmethod
    def _parse_csv_content(
        cls,
        csv_text: str,
        default_date: str,
        default_branch: str,
        default_curr: str,
        message_id: str
    ) -> List[FuelSaleRecord]:
        results: List[FuelSaleRecord] = []
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            if not reader.fieldnames:
                return results

            headers = {h.strip().lower(): h for h in reader.fieldnames if h}
            prod_col = next((headers[h] for h in headers if any(k in h for k in ("product", "fuel", "item"))), None)
            vol_col = next((headers[h] for h in headers if any(k in h for k in ("volume", "liter", "qty", "quantity"))), None)
            rev_col = next((headers[h] for h in headers if any(k in h for k in ("revenue", "amount", "total", "sales"))), None)
            date_col = next((headers[h] for h in headers if "date" in h), None)
            branch_col = next((headers[h] for h in headers if any(k in h for k in ("branch", "station", "site"))), None)

            if not prod_col or (not vol_col and not rev_col):
                return results

            for row in reader:
                raw_prod = (row.get(prod_col) or "").strip().lower()
                norm_prod = next((target for key, target in cls.PRODUCT_MAPPING.items() if key in raw_prod), None)
                if not norm_prod:
                    continue

                vol = cls.parse_float(row.get(vol_col)) if vol_col else 0.0
                rev = cls.parse_float(row.get(rev_col)) if rev_col else 0.0
                row_date = (row.get(date_col) or "").strip() if date_col else default_date
                clean_date = cls.extract_date(row_date) or default_date
                row_branch = (row.get(branch_col) or "").strip() if branch_col else default_branch

                results.append(FuelSaleRecord(
                    sale_date=clean_date,
                    branch_name=row_branch or default_branch,
                    fuel_product=norm_prod,
                    volume_sold=vol or 0.0,
                    revenue_amount=rev or 0.0,
                    raw_message_id=message_id,
                    currency=default_curr
                ))
        except Exception as e:
            logger.error(f"Failed parsing CSV for msg_id={message_id}: {e}")
        return results


# -----------------------------------------------------------------------------
# Gmail Client & Sync Runner
# -----------------------------------------------------------------------------

class GmailService:
    """Robust OAuth2 and Service Account Gmail Ingestion Service."""

    def __init__(
        self,
        credentials_file: str = DEFAULT_CREDENTIALS_FILE,
        token_file: str = DEFAULT_TOKEN_FILE,
        service_account_file: str = DEFAULT_SERVICE_ACCOUNT_FILE,
        db_path: str = DEFAULT_DB_FILE
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service_account_file = service_account_file
        self.store = IngestionStore(db_path=db_path)
        self.service: Optional[Resource] = None

    def authenticate(self, allow_browser_login: bool = False) -> Resource:
        """
        Authenticates securely.
        1. Checks for Service Account credentials (ideal for cron/backend).
        2. Checks for pre-authorized user token and refreshes non-interactively.
        3. Fails securely if running headless without authorization rather than hanging.
        """
        if build is None:
            raise RuntimeError("Google API client libraries are not installed.")

        creds = None

        # 1. Service Account Flow
        if self.service_account_file and os.path.exists(self.service_account_file):
            logger.info("Authenticating via Google Service Account.")
            creds = service_account.Credentials.from_service_account_file(
                self.service_account_file, scopes=SCOPES
            )
            self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            return self.service

        # 2. User OAuth2 Token Flow
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired OAuth token non-interactively...")
                creds.refresh(Request())
                self._save_token_securely(creds)
            else:
                if not allow_browser_login:
                    raise PermissionError(
                        "No valid OAuth token found and interactive login is disabled in headless mode. "
                        "Pre-generate token.json or configure service account credentials."
                    )
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(f"Missing OAuth client secret at '{self.credentials_file}'")

                logger.info("Launching local OAuth flow (interactive)...")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
                self._save_token_securely(creds)

        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self.service

    def _save_token_securely(self, creds: Credentials) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = 0o600  # Read/Write only for owner
        token_dir = os.path.dirname(self.token_file)
        if token_dir:
            os.makedirs(token_dir, exist_ok=True)
        fd = os.open(self.token_file, flags, mode)
        with open(fd, "w") as f:
            f.write(creds.to_json())

    @staticmethod
    def _safe_b64decode(raw_data: str) -> str:
        padded = raw_data + "=" * (-len(raw_data) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded.encode("ASCII"))
        return decoded_bytes.decode("utf-8", errors="replace")

    def _extract_body_and_attachments(self, payload: Dict[str, Any], message_id: str) -> Tuple[str, List[str]]:
        plain_parts: List[str] = []
        html_parts: List[str] = []
        csv_attachments: List[str] = []

        def recurse(part: Dict[str, Any]) -> None:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            filename = part.get("filename", "")
            att_id = part.get("body", {}).get("attachmentId")

            if mime == "text/plain" and data:
                plain_parts.append(self._safe_b64decode(data))
            elif mime == "text/html" and data:
                html_parts.append(self._safe_b64decode(data))

            if filename.lower().endswith(".csv"):
                if att_id and self.service:
                    try:
                        res = self.service.users().messages().attachments().get(
                            userId="me", messageId=message_id, id=att_id
                        ).execute()
                        att_data = res.get("data", "")
                        if att_data:
                            csv_attachments.append(self._safe_b64decode(att_data))
                    except HttpError as err:
                        logger.error(f"Failed to fetch attachment {att_id} in msg {message_id}: {err}")
                elif data:
                    csv_attachments.append(self._safe_b64decode(data))

            for subpart in part.get("parts", []):
                recurse(subpart)

        recurse(payload)

        # Prioritize text/plain; fall back to stripped HTML
        body_text = "\n".join(plain_parts) if plain_parts else strip_html("\n".join(html_parts))
        return body_text, csv_attachments

    def sync_inbox(self, max_total_messages: int = 100, query: str = DEFAULT_GMAIL_QUERY) -> SyncSummary:
        """
        Executes ingestion with pagination and deduplication.
        """
        if not self.service:
            self.authenticate()

        summary = SyncSummary(
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_messages_found=0,
            processed_count=0,
            ignored_count=0,
            already_synced_count=0,
            extracted_records_count=0,
            quarantined_count=0,
            records=[],
            errors=[]
        )

        try:
            # 1. Fetch message references with pagination
            message_ids: List[str] = []
            page_token = None

            while len(message_ids) < max_total_messages:
                batch_size = min(50, max_total_messages - len(message_ids))
                res = self.service.users().messages().list(
                    userId="me",
                    maxResults=batch_size,
                    q=query,
                    pageToken=page_token
                ).execute()

                messages = res.get("messages", [])
                if not messages:
                    break

                for m in messages:
                    message_ids.append(m["id"])

                page_token = res.get("nextPageToken")
                if not page_token:
                    break

            summary.total_messages_found = len(message_ids)

            # 2. Process messages
            for msg_id in message_ids:
                if self.store.is_message_processed(msg_id):
                    summary.already_synced_count += 1
                    continue

                try:
                    msg = self.service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                    payload = msg.get("payload", {})
                    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
                    subject = headers.get("subject", "")
                    sender = headers.get("from", "")

                    body_text, csv_attachments = self._extract_body_and_attachments(payload, msg_id)

                    # Filter non-sales emails
                    if not SalesEmailClassifier.is_sales_email(subject, body_text):
                        self.store.persist_email_and_records(
                            message_id=msg_id,
                            subject=subject,
                            sender=sender,
                            status="IGNORED",
                            is_sales_report=False,
                            records=[]
                        )
                        summary.ignored_count += 1
                        continue

                    # Extract line items
                    records = SalesDataExtractor.extract_records(body_text, csv_attachments, msg_id)

                    if not records:
                        # Email matched sales criteria but failed date/branch/product validation
                        self.store.persist_email_and_records(
                            message_id=msg_id,
                            subject=subject,
                            sender=sender,
                            status="QUARANTINED",
                            is_sales_report=True,
                            records=[]
                        )
                        summary.quarantined_count += 1
                        continue

                    # Persist atomically
                    self.store.persist_email_and_records(
                        message_id=msg_id,
                        subject=subject,
                        sender=sender,
                        status="PROCESSED",
                        is_sales_report=True,
                        records=records
                    )
                    summary.processed_count += 1
                    summary.records.extend(records)
                    summary.extracted_records_count += len(records)

                except sqlite3.DatabaseError as db_err:
                    err_msg = f"Database error on msg_id={msg_id}: {db_err}"
                    logger.exception(err_msg)
                    summary.errors.append(err_msg)
                except Exception as msg_err:
                    err_msg = f"Unexpected processing error on msg_id={msg_id}: {msg_err}"
                    logger.exception(err_msg)
                    summary.errors.append(err_msg)

        except HttpError as api_err:
            err_msg = f"Gmail API error: {api_err}"
            logger.error(err_msg)
            summary.errors.append(err_msg)

        return summary


# -----------------------------------------------------------------------------
# Trigger Endpoints
# -----------------------------------------------------------------------------

def run_scheduled_cron() -> SyncSummary:
    """Cron entry point (strictly headless)."""
    service = GmailService()
    service.authenticate(allow_browser_login=False)
    return service.sync_inbox()


def sync_inbox_now() -> Dict[str, Any]:
    """Interactive / UI trigger endpoint."""
    service = GmailService()
    summary = service.sync_inbox()
    return {
        "timestamp": summary.timestamp,
        "total_messages_found": summary.total_messages_found,
        "processed_count": summary.processed_count,
        "ignored_count": summary.ignored_count,
        "quarantined_count": summary.quarantined_count,
        "already_synced_count": summary.already_synced_count,
        "extracted_records_count": summary.extracted_records_count,
        "errors": summary.errors,
        "records": [asdict(r) for r in summary.records]
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    res = sync_inbox_now()
    print(json.dumps(res, indent=2))