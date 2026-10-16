"""Read-only statement decoration. Counterparty facts come from the posted pair."""

from decimal import Decimal

from django.utils.html import strip_tags

from .copy import copy, transaction_label
from .models import Ledger


def decorate(rows):
    labels = copy()
    groups = {r.operation_group for r in rows if r.event_type == "transfer" and r.counterpart_id}
    pairs = (
        {
            (r.operation_group, r.member_id, r.counterpart_id): r
            for r in Ledger.objects.filter(event_type="transfer", operation_group__in=groups).only(
                "operation_group",
                "member_id",
                "counterpart_id",
                "operation_number",
                "membership_number",
                "account_name",
                "credit",
                "debit",
            )
        }
        if groups
        else {}
    )
    for row in rows:
        row.ui_kind = transaction_label(row)
        row.ui_rate = (
            Decimal(row.allocation_numerator) / Decimal(row.allocation_denominator)
            if row.allocation_denominator
            else row.rate_percent
        )
        row.ui_balance_before = row.member_balance_after - row.credit + row.debit
        row.ui_rules = (
            labels["rules_first"]
            if row.rules_version == "loyalty-approved-20261010-v1"
            else labels["rules_other"]
        )
        row.ui_transfer = row.event_type == "transfer"
        row.ui_counterpart = None
        if not row.ui_transfer:
            continue
        pair = pairs.get((row.operation_group, row.counterpart_id, row.member_id))
        # Never guess the other party from the tree, current profile or another group.
        if pair and pair.credit == row.debit and pair.debit == row.credit:
            name = " ".join(strip_tags(pair.account_name).split())
            # Older name snapshots may contain an email fallback. Do not expose it.
            if "@" in name:
                name = ""
            row.ui_counterpart = {
                "name": name or labels["member"],
                "number": pair.membership_number,
                "operation": pair.operation_number,
            }
        row.ui_kind = labels["gift_out"] if row.debit else labels["gift_in"]
