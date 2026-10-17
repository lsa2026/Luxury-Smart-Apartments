"""Allowlisted measurement metadata; never used to accept or decline a charge."""

import json
import re
from typing import Any


def verified_payment_wallet(document: dict[str, Any]) -> str:
    """Read the owner-approved walletProvider marker from a provider response.

    The actual API position remains provisional: the marker was observed in
    the original transaction's connector log. Accept only the root field or
    provider resultDetails (including serialized connector entries), never
    merchant customParameters, a browser selection, or a network-name guess.
    No raw response/log is retained. Conflicting/unknown values fail closed.
    """
    markers: list[object] = []

    def visit(value: object, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:100]:
                if key == "walletProvider":
                    markers.append(child)
                elif key != "customParameters":
                    visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:20]:
                visit(child, depth + 1)
        elif isinstance(value, str) and len(value) <= 16384:
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError, RecursionError):
                # Some connector entries are key=value text rather than JSON.
                markers.extend(
                    re.findall(
                        r'(?<![\w])walletProvider["\']?\s*[:=]\s*["\']?([A-Z_]+)(?![\w])',
                        value,
                    )
                )
            else:
                if isinstance(parsed, dict | list):
                    visit(parsed, depth + 1)

    if "walletProvider" in document:
        markers.append(document["walletProvider"])
    visit(document.get("resultDetails"))
    return "apple_pay" if markers and all(value == "APPLE_PAY" for value in markers) else ""
