import csv
import io
import logging
import os
import re
from datetime import datetime
from typing import List, Optional

from api.models.gmail_datamodels import FuelSaleRecord
from api.utils.util import parse_float



DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "PHP")
logger = logging.getLogger(__name__)



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