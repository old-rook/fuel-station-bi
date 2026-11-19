"""
test_gmail_services.py
-----------------------
Unit and coverage tests for gmail_service.py covering FR-1.1 through FR-1.5:
- FR-1.1: OAuth 2.0 flow, token caching, token refreshing, and missing credentials error handling.
- FR-1.2: Cron runner and on-demand UI endpoint ('sync_inbox_now').
- FR-1.3: Message deduplication via message_id.
- FR-1.4: Extraction of fuel product, volume, revenue, currency, branch, and date from body & CSVs.
- FR-1.5: Filtering non-sales emails (promotions, receipts, newsletters).
"""

import base64
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, mock_open, patch

from services.gmail_service import (
    FuelSaleRecord,
    GmailService,
    IngestionStore,
    SalesDataExtractor,
    SalesEmailClassifier,
    SyncSummary,
    run_scheduled_cron,
    sync_inbox_now,
)


class TestIngestionStore(unittest.TestCase):
    """Tests the SQLite deduplication and record storage mechanism (FR-1.3)."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.store = IngestionStore(db_path=self.temp_db.name)

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)

    def test_database_initialization(self):
        with sqlite3.connect(self.temp_db.name) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            self.assertIn("processed_emails", tables)
            self.assertIn("fuel_sales", tables)

    def test_message_deduplication(self):
        msg_id = "msg_abc_123"
        self.assertFalse(self.store.is_message_processed(msg_id))

        self.store.record_processed_email(
            message_id=msg_id,
            subject="Daily Sales Report",
            sender="manager@station.com",
            is_sales_report=True,
        )
        self.assertTrue(self.store.is_message_processed(msg_id))

        # Re-recording with same ID should not raise an error (INSERT OR IGNORE)
        self.store.record_processed_email(
            message_id=msg_id,
            subject="Duplicate",
            sender="manager@station.com",
            is_sales_report=True,
        )
        self.assertTrue(self.store.is_message_processed(msg_id))

    def test_save_sales_records(self):
        record = FuelSaleRecord(
            sale_date="2025-01-15",
            branch_name="Highway West",
            fuel_product="Diesel",
            volume_sold=1200.5,
            revenue_amount=3600.0,
            currency="USD",
            raw_message_id="msg_abc_123",
        )
        self.store.save_sales_records([record])

        with sqlite3.connect(self.temp_db.name) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT branch_name, fuel_product, volume_sold, revenue_amount FROM fuel_sales")
            rows = cursor.fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0], ("Highway West", "Diesel", 1200.5, 3600.0))


class TestSalesEmailClassifier(unittest.TestCase):
    """Tests filtering out non-sales emails and identifying valid reports (FR-1.5)."""

    def test_valid_fuel_sales_emails(self):
        cases = [
            ("Daily Sales Report - Pump reconciliation", "Diesel and Unleaded meter readings for today", "ops@station.com"),
            ("EOD Fuel Volume Report", "End of day fuel report for Highway station. Regular unleaded volume sold.", "branch1@domain.com"),
            ("Shift Report: Sales summary", "Litres sold of Premium and LPG fuels today.", "manager@fuel.com"),
        ]
        for subject, body, sender in cases:
            self.assertTrue(
                SalesEmailClassifier.is_sales_email(subject, body, sender),
                f"Failed for: {subject}",
            )

    def test_ignore_promotions_newsletters_and_spam(self):
        cases = [
            ("Weekly Newsletter #45", "Here is our weekly fuel industry newsletter. Unsubscribe here.", "news@petro.com"),
            ("Big Discount Promotion!", "Get 20% off your next oil change! Exclusive promotion for members.", "promo@carcare.com"),
            ("Your Order Confirmation", "Thank you for purchasing motor oil online. Order confirmation attached.", "store@auto.com"),
            ("Password Reset", "Click here to reset your account password. OTP: 123456", "security@station.com"),
            ("Meeting Agenda", "Team sync on Tuesday at 10 AM.", "boss@station.com"),
        ]
        for subject, body, sender in cases:
            self.assertFalse(
                SalesEmailClassifier.is_sales_email(subject, body, sender),
                f"Should have ignored: {subject}",
            )


class TestSalesDataExtractor(unittest.TestCase):
    """Tests extraction of sale_date, branch_name, fuel_product, volume, revenue, currency (FR-1.4)."""

    def test_extract_date(self):
        self.assertEqual(SalesDataExtractor.extract_date("Sales for 2025-05-18"), "2025-05-18")
        self.assertEqual(SalesDataExtractor.extract_date("Report dated 15/03/2025"), "2025-03-15")
        self.assertEqual(SalesDataExtractor.extract_date("Summary on Jan 20, 2025"), "2025-01-20")

        # Fallback to current date on unparseable string
        fallback = SalesDataExtractor.extract_date("No date mentioned here")
        self.assertRegex(fallback, r"^\d{4}-\d{2}-\d{2}$")

    def test_extract_branch(self):
        self.assertEqual(
            SalesDataExtractor.extract_branch("Branch: Downtown Station #4\nDate: 2025-01-01"),
            "Downtown Station #4",
        )
        self.assertEqual(
            SalesDataExtractor.extract_branch("Station - Coastal Highway\nTotal Sales"),
            "Coastal Highway",
        )
        self.assertEqual(
            SalesDataExtractor.extract_branch("No station name present", default="Main Branch"),
            "Main Branch",
        )

    def test_extract_records_from_unstructured_text(self):
        text = """
        Branch: North Highway
        Date: 2025-04-10
        Daily Fuel Report:
        Unleaded: 1,500.50 L - $4,501.50
        Diesel Sold: 2,000 Liters | Total: $6,000.00
        Premium: 500 L - $2,000.00
        """
        records = SalesDataExtractor.extract_records(text, attachments_data=[], message_id="m1")

        self.assertEqual(len(records), 3)
        products = {r.fuel_product for r in records}
        self.assertEqual(products, {"Unleaded", "Diesel", "Premium"})

        unleaded = next(r for r in records if r.fuel_product == "Unleaded")
        self.assertEqual(unleaded.sale_date, "2025-04-10")
        self.assertEqual(unleaded.branch_name, "North Highway")
        self.assertEqual(unleaded.volume_sold, 1500.50)
        self.assertEqual(unleaded.revenue_amount, 4501.50)
        self.assertEqual(unleaded.currency, "USD")

    def test_extract_records_from_csv_attachment(self):
        csv_content = (
            "Branch,Date,Product,Volume (L),Revenue,Currency\n"
            "East Terminal,2025-06-01,Diesel,5000,15000,EUR\n"
            "East Terminal,2025-06-01,Unleaded 95,3500,11200,EUR\n"
        )
        records = SalesDataExtractor.extract_records(
            text="Please see attached daily sales CSV.",
            attachments_data=[csv_content],
            message_id="m2",
        )

        self.assertEqual(len(records), 2)
        d_rec = next(r for r in records if r.fuel_product == "Diesel")
        self.assertEqual(d_rec.volume_sold, 5000.0)
        self.assertEqual(d_rec.revenue_amount, 15000.0)
        self.assertEqual(d_rec.branch_name, "East Terminal")
        self.assertEqual(d_rec.currency, "EUR")

    def test_malformed_csv_handled_gracefully(self):
        malformed_csv = "Broken,CSV\nNo,Header,Columns\n1,2,3,4"
        # Shouldn't crash, should gracefully return empty list or fallback
        records = SalesDataExtractor.extract_records("Empty report", [malformed_csv], "m3")
        self.assertIsInstance(records, list)


class TestGmailService(unittest.TestCase):
    """Tests GmailService API handling, OAuth flows, and inbox sync orchestration (FR-1.1, FR-1.2, FR-1.3)."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.temp_token = tempfile.NamedTemporaryFile(delete=False)
        self.temp_creds = tempfile.NamedTemporaryFile(delete=False)
        self.service = GmailService(
            credentials_file=self.temp_creds.name,
            token_file=self.temp_token.name,
            db_path=self.temp_db.name,
        )

    def tearDown(self):
        for path in (self.temp_db.name, self.temp_token.name, self.temp_creds.name):
            if os.path.exists(path):
                os.remove(path)

    @patch("gmail_service.build")
    @patch("gmail_service.Credentials")
    def test_authenticate_with_existing_valid_token(self, mock_creds_cls, mock_build):
        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = True
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds_instance
        mock_build.return_value = MagicMock()

        api_res = self.service.authenticate()
        self.assertIsNotNone(api_res)
        mock_creds_cls.from_authorized_user_file.assert_called_once()
        mock_build.assert_called_with("gmail", "v1", credentials=mock_creds_instance)

    @patch("gmail_service.build")
    @patch("gmail_service.Credentials")
    @patch("builtins.open", new_callable=mock_open)
    def test_authenticate_refreshes_expired_token(self, mock_file, mock_creds_cls, mock_build):
        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = False
        mock_creds_instance.expired = True
        mock_creds_instance.refresh_token = "valid_refresh_token"
        mock_creds_instance.to_json.return_value = "{}"
        mock_creds_cls.from_authorized_user_file.return_value = mock_creds_instance
        mock_build.return_value = MagicMock()

        self.service.authenticate()
        mock_creds_instance.refresh.assert_called_once()

    def test_authenticate_missing_credentials_raises_error(self):
        # Point to a credentials file that doesn't exist
        missing_svc = GmailService(
            credentials_file="non_existent_file.json",
            token_file="non_existent_token.json",
            db_path=self.temp_db.name,
        )
        with self.assertRaises(FileNotFoundError):
            missing_svc.authenticate()

    def test_extract_body_and_attachments(self):
        email_body = "Branch: West\nDate: 2025-01-01\nDiesel 1000L $3000"
        b64_body = base64.urlsafe_b64encode(email_body.encode("utf-8")).decode("ASCII")

        csv_content = "Product,Volume,Revenue\nDiesel,100,300"
        b64_csv = base64.urlsafe_b64encode(csv_content.encode("utf-8")).decode("ASCII")

        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": b64_body},
                },
                {
                    "mimeType": "text/csv",
                    "filename": "sales.csv",
                    "body": {"data": b64_csv},
                },
            ],
        }

        body_text, attachments = self.service._extract_body_and_attachments(payload, "test_msg")
        self.assertIn("Diesel 1000L", body_text)
        self.assertEqual(len(attachments), 1)
        self.assertIn("Product,Volume,Revenue", attachments[0])

    def test_sync_inbox_orchestration_and_deduplication(self):
        mock_api = MagicMock()
        self.service.service = mock_api

        # 3 messages in inbox:
        # 1. Valid sales email -> should be processed & saved
        # 2. Promo email -> should be ignored (FR-1.5)
        # 3. Duplicate message -> already processed previously (FR-1.3)
        mock_api.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg_valid"}, {"id": "msg_promo"}, {"id": "msg_dup"}]
        }

        # Pre-mark msg_dup as processed in DB
        self.service.store.record_processed_email(
            "msg_dup", "Old Report", "test@station.com", is_sales_report=True
        )

        valid_body = base64.urlsafe_b64encode(
            b"Branch: Center\nDate: 2025-01-01\nUnleaded: 500L $1500"
        ).decode("ASCII")
        promo_body = base64.urlsafe_b64encode(
            b"Get your huge newsletter discount promo today!"
        ).decode("ASCII")

        def mock_get(userId, id, format):
            cmd = MagicMock()
            if id == "msg_valid":
                cmd.execute.return_value = {
                    "id": "msg_valid",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Daily Fuel Sales Report"},
                            {"name": "From", "value": "ops@station.com"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": valid_body},
                    },
                }
            elif id == "msg_promo":
                cmd.execute.return_value = {
                    "id": "msg_promo",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Weekly Newsletter & Promotion"},
                            {"name": "From", "value": "marketing@news.com"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": promo_body},
                    },
                }
            return cmd

        mock_api.users().messages().get.side_effect = mock_get

        summary = self.service.sync_inbox()

        self.assertEqual(summary.total_messages_found, 3)
        self.assertEqual(summary.already_synced_count, 1)  # msg_dup skipped
        self.assertEqual(summary.ignored_count, 1)         # msg_promo ignored
        self.assertEqual(summary.processed_count, 1)       # msg_valid processed
        self.assertEqual(summary.extracted_records_count, 1)
        self.assertEqual(len(summary.records), 1)
        self.assertEqual(summary.records[0].fuel_product, "Unleaded")

        # Running sync again immediately: everything should be already synced
        second_summary = self.service.sync_inbox()
        self.assertEqual(second_summary.already_synced_count, 3)
        self.assertEqual(second_summary.processed_count, 0)
        self.assertEqual(second_summary.ignored_count, 0)


class TestTriggerEndpoints(unittest.TestCase):
    """Tests cron job and UI invocation functions (FR-1.2)."""

    @patch.object(GmailService, "sync_inbox")
    def test_run_scheduled_cron(self, mock_sync):
        mock_summary = SyncSummary(
            timestamp="2025-01-01T00:00:00Z",
            total_messages_found=5,
            processed_count=2,
            ignored_count=3,
            already_synced_count=0,
            extracted_records_count=2,
            records=[],
            errors=[],
        )
        mock_sync.return_value = mock_summary

        res = run_scheduled_cron()
        self.assertEqual(res.processed_count, 2)
        mock_sync.assert_called_once()

    @patch.object(GmailService, "sync_inbox")
    def test_sync_inbox_now_ui_endpoint(self, mock_sync):
        sample_record = FuelSaleRecord(
            sale_date="2025-02-01",
            branch_name="South Bay",
            fuel_product="Diesel",
            volume_sold=800.0,
            revenue_amount=2400.0,
            currency="USD",
            raw_message_id="msg_ui_1",
        )
        mock_summary = SyncSummary(
            timestamp="2025-02-01T12:00:00Z",
            total_messages_found=1,
            processed_count=1,
            ignored_count=0,
            already_synced_count=0,
            extracted_records_count=1,
            records=[sample_record],
            errors=[],
        )
        mock_sync.return_value = mock_summary

        result = sync_inbox_now()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["total_messages_found"], 1)
        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["fuel_product"], "Diesel")
        self.assertEqual(result["records"][0]["revenue_amount"], 2400.0)


if __name__ == "__main__":
    unittest.main()