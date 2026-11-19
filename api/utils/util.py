import base64
import re
from typing import Optional


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

def safe_b64decode(raw_data: str) -> str:
        padded = raw_data + "=" * (-len(raw_data) % 4)
        decoded_bytes = base64.urlsafe_b64decode(padded.encode("ASCII"))
        return decoded_bytes.decode("utf-8", errors="replace")