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

import json
import logging
import os
import random
import time
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from api.utils.html_helper import strip_html
from api.utils.ingestion import IngestionStore
from api.utils.emails import SalesDataExtractor, SalesEmailClassifier
from api.utils.util import safe_b64decode
from api.models.gmail_datamodels import SyncSummary

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
DEFAULT_GMAIL_QUERY = os.getenv("GMAIL_SEARCH_QUERY", "in:inbox")
DEFAULT_DB_FILE = os.getenv("APP_DB_PATH", os.path.abspath("fuel_sales.db"))


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
                plain_parts.append(safe_b64decode(data))
            elif mime == "text/html" and data:
                html_parts.append(safe_b64decode(data))

            if filename.lower().endswith(".csv"):
                if att_id and self.service:
                    try:
                        req = self.service.users().messages().attachments().get(
                            userId="me", messageId=message_id, id=att_id
                        )
                        res = self.execute_with_backoff(req)
                        att_data = res.get("data", "")
                        if att_data:
                            csv_attachments.append(safe_b64decode(att_data))
                    except HttpError as err:
                        logger.error(f"Failed to fetch attachment {att_id} in msg {message_id}: {err}")
                elif data:
                    csv_attachments.append(safe_b64decode(data))

            for subpart in part.get("parts", []):
                recurse(subpart)

        recurse(payload)

        # Prioritize text/plain; fall back to stripped HTML
        body_text = "\n".join(plain_parts) if plain_parts else strip_html("\n".join(html_parts))
        return body_text, csv_attachments

    @staticmethod
    def execute_with_backoff(request, max_retries: int = 5):
        """
        Executes a Google API request with exponential backoff and jitter
        to handle 429 and 403 rate limit / quota errors.
        """
        for attempt in range(max_retries):
            try:
                return request.execute()
            except HttpError as err:
                # 429 = Too Many Requests; 403 with rateLimitExceeded/userRateLimitExceeded
                is_rate_limit = err.resp.status in (429, 500, 503) or (
                    err.resp.status == 403 and any(
                        r.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded")
                        for r in getattr(err, "error_details", [])
                        or [{"reason": "rateLimitExceeded"}]
                    )
                )

                if is_rate_limit and attempt < max_retries - 1:
                    # Exponential backoff formula: (2 ^ attempt) + jitter
                    sleep_seconds = (2 ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning(
                        f"Gmail quota limit hit. Retrying in {sleep_seconds:.2f}s "
                        f"(attempt {attempt + 1}/{max_retries})..."
                    )
                    time.sleep(sleep_seconds)
                else:
                    raise

    def sync_inbox(self, max_total_messages: int = 20, query: str = DEFAULT_GMAIL_QUERY) -> SyncSummary:
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
                req = self.service.users().messages().list(
                    userId="me",
                    maxResults=batch_size,
                    q=query,
                    pageToken=page_token
                )
                res = self.execute_with_backoff(req)

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
                    # Add a 100ms pause to smooth out quota spikes
                    time.sleep(0.1)

                    msg_req = self.service.users().messages().get(
                        userId="me", id=msg_id, format="full"
                    )
                    msg = self.execute_with_backoff(msg_req)
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