import os
from dataclasses import dataclass
from typing import List


DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "PHP")


@dataclass(frozen=True)
class FuelSaleRecord:
    sale_date: str          # Strict ISO: YYYY-MM-DD
    branch_name: str        # Verified station identifier
    fuel_product: str       # Normalized product name
    volume_sold: float      # Net liters
    revenue_amount: float   # Net monetary amount
    raw_message_id: str     # Originating Gmail message ID
    currency: str = DEFAULT_CURRENCY


@dataclass
class SyncSummary:
    timestamp: str
    total_messages_found: int
    processed_count: int
    ignored_count: int
    already_synced_count: int
    extracted_records_count: int
    quarantined_count: int
    records: List[FuelSaleRecord]
    errors: List[str]