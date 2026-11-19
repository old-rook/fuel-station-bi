"""
gmail_service.py
----------------
Implements FR-1: Gmail Inbox Ingestion & Extraction for Fuel Station Sales Reports.

Features:
- FR-1.1: OAuth 2.0 authentication with Gmail readonly scope (and optional send scope).
- FR-1.2: On-demand sync execution ('Sync Inbox Now') and cron-compatible trigger.
- FR-1.3: Deduplication by unique Gmail message_id using persistent storage (SQLite by default).
- FR-1.4: Extraction of sale_date, branch_name, fuel_product, volume_sold, revenue_amount, currency.
- FR-1.5: Filtering non-sales emails (promos, receipts, newsletters) without cluttering review queues.
"""

import base64
import csv
import io
import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

# Google API Client Libraries
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import Resource, build
    from googleapiclient.errors import HttpError

except ImportError:
    # Allows module inspection or mock-testing even if google libs are not installed yet
    Credentials = None  # type: ignore
    InstalledAppFlow = None  # type: ignore
    Resource = None  # type: ignore
    build = None  # type: ignore
    HttpError = Exception  # type: ignore

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Scopes: Read-only for ingestion; send is optional for notification workflows
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

DEFAULT_TOKEN_FILE = os.getenv("GMAIL_TOKEN_PATH", "token.json")
DEFAULT_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
DEFAULT_DB_FILE = os.getenv("APP_DB_PATH", "fuel_sales.db")


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------

@dataclass
class FuelSaleRecord:
    sale_date: str          # Format: YYYY-MM-DD
    branch_name: str        # e.g., "North Highway Station", "Downtown Branch", can use branch code if available.
    fuel_product: str       # e.g., "Unleaded", "Diesel", "Premium", "E10"
    volume_sold: float      # in Liters
    revenue_amount: float   # total amount sold
    raw_message_id: str     # Gmail message ID for traceability
    currency: str = "PHP"   #  "PHP"


@dataclass
class SyncSummary:
    timestamp: str
    total_messages_found: int
    processed_count: int
    ignored_count: int
    already_synced_count: int
    extracted_records_count: int
    records: List[FuelSaleRecord]
    errors: List[str]


# -----------------------------------------------------------------------------
# Storage / Deduplication Helper (SQLite-backed)
# -----------------------------------------------------------------------------

class IngestionStore:
    """Manages processed Gmail message IDs and extracted records for deduplication."""

    def __init__(self, db_path: str = DEFAULT_DB_FILE):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Table to guarantee FR-1.3 deduplication
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    subject TEXT,
                    sender TEXT,
                    is_sales_report INTEGER NOT NULL
                )
            """)
            # Table for extracted fuel sales data
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
                    FOREIGN KEY (message_id) REFERENCES processed_emails(message_id)
                )
            """)
            conn.commit()

    def is_message_processed(self, message_id: str) -> bool:
        """FR-1.3: Check if email has already been ingested."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,))
            return cursor.fetchone() is not None

    def record_processed_email(self, message_id: str, subject: str, sender: str, is_sales_report: bool) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO processed_emails (message_id, processed_at, subject, sender, is_sales_report)
                VALUES (?, ?, ?, ?, ?)
            """, (message_id, datetime.now(timezone.utc).isoformat(), subject, sender, int(is_sales_report)))
            conn.commit()

    def save_sales_records(self, records: List[FuelSaleRecord]) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now(timezone.utc).isoformat()
            cursor.executemany("""
                INSERT INTO fuel_sales (message_id, sale_date, branch_name, fuel_product, volume_sold, revenue_amount, currency, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (r.raw_message_id, r.sale_date, r.branch_name, r.fuel_product, r.volume_sold, r.revenue_amount, r.currency, now)
                for r in records
            ])
            conn.commit()


# -----------------------------------------------------------------------------
# FR-1.5: Non-Sales Filter & Classifier
# -----------------------------------------------------------------------------

class SalesEmailClassifier:
    """Detects whether an incoming email is a valid station sales report."""

    SALES_KEYWORDS = {
        "sales", "fuel", "daily report", "end of day", "eod", "meter reading",
        "volume", "liters", "litres", "gallons", "shift report", "pump", "reconciliation"
    }

    IGNORE_KEYWORDS = {
        "newsletter", "unsubscribe", "promotion", "marketing", "webinar",
        "discount", "order confirmation", "otp", "verify your account", "password reset"
    }

    FUEL_PRODUCTS = ["unleaded", "regular", "diesel", "premium", "v-power", "e10", "autogas", "lpg"]

    @classmethod
    def is_sales_email(cls, subject: str, body: str, sender: str) -> bool:
        combined = f"{subject} {body}".lower()

        # Check explicit ignore keywords first
        if any(ign in combined for ign in cls.IGNORE_KEYWORDS):
            return False

        # Look for sales indicators
        has_sales_kw = any(kw in combined for kw in cls.SALES_KEYWORDS)
        has_fuel_kw = any(fuel in combined for fuel in cls.FUEL_PRODUCTS)

        return has_sales_kw and has_fuel_kw


# -----------------------------------------------------------------------------
# FR-1.4: Extraction Engine
# -----------------------------------------------------------------------------

class SalesDataExtractor:
    """Extracts sale_date, branch_name, fuel_product, volume, revenue, currency."""

    # Recognized fuel products normalizer
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
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
        "₱": "PHP",
        "¥": "JPY",
        "A$": "AUD",
        "C$": "CAD"
    }

    @staticmethod
    def extract_date(text: str) -> str:
        # Match YYYY-MM-DD, DD/MM/YYYY, MM/DD/YYYY, or Month DD, YYYY
        patterns = [
            r"\b(20\d{2}[-/](?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12]\d|3[01]))\b",
            r"\b((?:0[1-9]|[12]\d|3[01])[-/.](?:0[1-9]|1[0-2])[-/.](?:20\d{2}))\b",
            r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2})\b"
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                raw_date = match.group(1).replace("/", "-").replace(".", "-")
                try:
                    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
                        try:
                            return datetime.strptime(raw_date.replace(",", ""), fmt).strftime("%Y-%m-%d")
                        except ValueError:
                            continue
                except Exception:
                    pass
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def extract_branch(text: str, default: str = "Main Branch") -> str:
        # Match "Branch: North Station" or "Station: Downtown #101"
        branch_match = re.search(r"(?:Branch|Station|Location|Site)\s*[:#-]?\s*([A-Za-z0-9\s\-]+?)(?:\r?\n|,|$)", text, re.IGNORECASE)
        if branch_match:
            candidate = branch_match.group(1).strip()
            if len(candidate) > 2 and len(candidate) < 60:
                return candidate
        return default

    @classmethod
    def extract_records(cls, text: str, attachments_data: List[str], message_id: str) -> List[FuelSaleRecord]:
        """
        Extracts sales line items from text and attached CSVs.
        Handles tabular/key-value representations.
        """
        records: List[FuelSaleRecord] = []
        full_text = text + "\n" + "\n".join(attachments_data)

        sale_date = cls.extract_date(full_text)
        branch_name = cls.extract_branch(full_text)

        # Detect currency
        currency = "USD"
        for symbol, code in cls.CURRENCY_SYMBOLS.items():
            if symbol in full_text:
                currency = code
                break
        code_match = re.search(r"\b(USD|EUR|GBP|PHP|CAD|AUD|NZD)\b", full_text, re.IGNORECASE)
        if code_match:
            currency = code_match.group(1).upper()

        # 1. Attempt parsing CSV attachments first if present
        for attach in attachments_data:
            if "," in attach:
                csv_records = cls._parse_csv_content(attach, sale_date, branch_name, currency, message_id)
                records.extend(csv_records)

        if records:
            return records

        # 2. Heuristic line-by-line / regex parsing for unstructured body text
        # Pattern examples:
        # Unleaded: 1,500.50 L - $4,501.50
        # Diesel Sold: 2000 Liters | Total: $6000.00
        for raw_name, std_name in cls.PRODUCT_MAPPING.items():
            pattern = rf"(?i)\b{raw_name}\b.*?(\d[\d,]*\.?\d*)\s*(?:l|liters|litres|gal|gallons)?.*?[\$€£₱]?\s*(\d[\d,]*\.?\d*)"
            match = re.search(pattern, full_text)
            if match:
                try:
                    vol = float(match.group(1).replace(",", ""))
                    rev = float(match.group(2).replace(",", ""))
                    if vol > 0 or rev > 0:
                        records.append(FuelSaleRecord(
                            sale_date=sale_date,
                            branch_name=branch_name,
                            fuel_product=std_name,
                            volume_sold=vol,
                            revenue_amount=rev,
                            currency=currency,
                            raw_message_id=message_id
                        ))
                except (ValueError, IndexError):
                    continue

        return records

    @classmethod
    def _parse_csv_content(cls, csv_text: str, default_date: str, default_branch: str, default_curr: str, message_id: str) -> List[FuelSaleRecord]:
        results: List[FuelSaleRecord] = []
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            if not reader.fieldnames:
                return results

            # Normalize headers
            headers = {h.strip().lower(): h for h in reader.fieldnames if h}
            product_col = next((headers[h] for h in headers if "product" in h or "fuel" in h), None)
            volume_col = next((headers[h] for h in headers if "vol" in h or "liter" in h or "quantity" in h), None)
            revenue_col = next((headers[h] for h in headers if "rev" in h or "amount" in h or "total" in h or "sales" in h), None)
            date_col = next((headers[h] for h in headers if "date" in h), None)
            branch_col = next((headers[h] for h in headers if "branch" in h or "station" in h), None)

            if product_col and (volume_col or revenue_col):
                for row in reader:
                    prod_val = row.get(product_col, "").strip()
                    norm_prod = None
                    for key, mapped in cls.PRODUCT_MAPPING.items():
                        if key in prod_val.lower():
                            norm_prod = mapped
                            break
                    if not norm_prod:
                        continue

                    def clean_float(val: Optional[str]) -> float:
                        if not val:
                            return 0.0
                        cleaned = re.sub(r"[^\d.]", "", val)
                        try:
                            return float(cleaned)
                        except ValueError:
                            return 0.0

                    vol = clean_float(row.get(volume_col)) if volume_col else 0.0
                    rev = clean_float(row.get(revenue_col)) if revenue_col else 0.0
                    s_date = row.get(date_col).strip() if date_col and row.get(date_col) else default_date
                    s_branch = row.get(branch_col).strip() if branch_col and row.get(branch_col) else default_branch

                    results.append(FuelSaleRecord(
                        sale_date=s_date,
                        branch_name=s_branch,
                        fuel_product=norm_prod,
                        volume_sold=vol,
                        revenue_amount=rev,
                        currency=default_curr,
                        raw_message_id=message_id
                    ))
        except Exception as err:
            logger.warning(f"Error parsing CSV attachment: {err}")
        return results


# -----------------------------------------------------------------------------
# FR-1.1 & FR-1.2: Gmail Service Client & Ingestion Runner
# -----------------------------------------------------------------------------

class GmailService:
    """Manages OAuth 2.0 connection, inbox fetching, deduplication, and extraction."""

    def __init__(
        self,
        credentials_file: str = DEFAULT_CREDENTIALS_FILE,
        token_file: str = DEFAULT_TOKEN_FILE,
        db_path: str = DEFAULT_DB_FILE
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.store = IngestionStore(db_path=db_path)
        self.service: Optional[Resource] = None

    def authenticate(self) -> Resource:
        """FR-1.1: Connect securely using OAuth 2.0 with gmail.readonly scope."""
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Gmail OAuth token...")
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"OAuth client secrets file '{self.credentials_file}' not found. "
                        "Download it from Google Cloud Console."
                    )
                logger.info("Initiating OAuth 2.0 browser authorization flow...")
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)

            # Persist token for subsequent runs
            with open(self.token_file, "w") as token:
                token.write(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Successfully authenticated with Gmail API.")
        return self.service

    def _extract_body_and_attachments(self, payload: Dict[str, Any], message_id: str) -> Tuple[str, List[str]]:
        """Traverses MIME message payload to retrieve body text and CSV attachments."""
        body_text = ""
        csv_attachments: List[str] = []

        def recurse_parts(part: Dict[str, Any]):
            nonlocal body_text
            mime_type = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            filename = part.get("filename", "")

            # If part has inline base64 text
            if data and ("text/plain" in mime_type or "text/html" in mime_type):
                decoded = base64.urlsafe_b64decode(data.encode("ASCII")).decode("utf-8", errors="replace")
                body_text += "\n" + decoded

            # Check for CSV attachment
            if filename.lower().endswith(".csv"):
                attachment_id = part.get("body", {}).get("attachmentId")
                if attachment_id and self.service:
                    try:
                        att_res = self.service.users().messages().attachments().get(
                            userId="me", messageId=message_id, id=attachment_id
                        ).execute()
                        att_data = att_res.get("data", "")
                        if att_data:
                            decoded_csv = base64.urlsafe_b64decode(att_data.encode("ASCII")).decode("utf-8", errors="replace")
                            csv_attachments.append(decoded_csv)
                    except Exception as err:
                        logger.error(f"Failed to fetch attachment {attachment_id}: {err}")
                elif data:
                    decoded_csv = base64.urlsafe_b64decode(data.encode("ASCII")).decode("utf-8", errors="replace")
                    csv_attachments.append(decoded_csv)

            for subpart in part.get("parts", []):
                recurse_parts(subpart)

        recurse_parts(payload)
        return body_text, csv_attachments

    def sync_inbox(self, max_results: int = 50) -> SyncSummary:
        """
        FR-1.2: Core ingestion method.
        Callable from scheduled cron job or on-demand via 'Sync Inbox Now' UI button.
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
            records=[],
            errors=[]
        )

        try:
            # Fetch recent messages from Inbox
            response = self.service.users().messages().list(userId="me", maxResults=max_results, q="in:inbox").execute()
            messages = response.get("messages", [])
            summary.total_messages_found = len(messages)

            for msg_meta in messages:
                msg_id = msg_meta["id"]

                # FR-1.3: Deduplication check
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

                    # FR-1.5: Ignore non-sales emails
                    if not SalesEmailClassifier.is_sales_email(subject, body_text, sender):
                        self.store.record_processed_email(msg_id, subject, sender, is_sales_report=False)
                        summary.ignored_count += 1
                        continue

                    # FR-1.4: Extract sales metrics
                    records = SalesDataExtractor.extract_records(body_text, csv_attachments, msg_id)

                    # Record email as processed
                    self.store.record_processed_email(msg_id, subject, sender, is_sales_report=True)

                    if records:
                        self.store.save_sales_records(records)
                        summary.records.extend(records)
                        summary.extracted_records_count += len(records)

                    summary.processed_count += 1

                except Exception as msg_err:
                    err_msg = f"Error processing message {msg_id}: {str(msg_err)}"
                    logger.error(err_msg)
                    summary.errors.append(err_msg)

        except Exception as e:
            err_msg = f"Failed to list or sync Gmail messages: {str(e)}"
            logger.error(err_msg)
            summary.errors.append(err_msg)

        logger.info(
            f"Sync complete. Found: {summary.total_messages_found}, "
            f"Already Synced: {summary.already_synced_count}, "
            f"Processed: {summary.processed_count}, "
            f"Ignored: {summary.ignored_count}, "
            f"Records Saved: {summary.extracted_records_count}"
        )
        return summary


# -----------------------------------------------------------------------------
# Trigger Endpoints & Cron Runner
# -----------------------------------------------------------------------------

def run_scheduled_cron():
    """FR-1.2: Entry point executed automatically once per day via cron scheduler."""
    logger.info("Executing daily scheduled inbox sync...")
    service = GmailService()
    summary = service.sync_inbox()
    return summary


def sync_inbox_now() -> Dict[str, Any]:
    """FR-1.2: Entry point for the UI 'Sync Inbox Now' button."""
    service = GmailService()
    summary = service.sync_inbox()
    # Return JSON-serializable dictionary for UI/API consumption
    return {
        "timestamp": summary.timestamp,
        "total_messages_found": summary.total_messages_found,
        "processed_count": summary.processed_count,
        "ignored_count": summary.ignored_count,
        "already_synced_count": summary.already_synced_count,
        "extracted_records_count": summary.extracted_records_count,
        "errors": summary.errors,
        "records": [asdict(r) for r in summary.records]
    }


if __name__ == "__main__":
    import sys
    print("Running Gmail Ingestion Service...")
    res = sync_inbox_now()
    print(json.dumps(res, indent=2))