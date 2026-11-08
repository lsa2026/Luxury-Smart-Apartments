"""Read existing manual invitation receipts without rewriting their history."""

from datetime import datetime

from django.utils import timezone

KEYS = (
    ("whatsapp", "owner_join_invitation", "owner_join_invitation_requests"),
    ("email", "owner_join_invitation_email", "owner_join_invitation_email_requests"),
)
LABELS = {
    "queued": "بانتظار الإرسال",
    "sending": "جارٍ الإرسال",
    "accepted": "أُرسلت الدعوة؛ قبلها مزود الإرسال",
    "uncertain": "نتيجة الإرسال غير مؤكدة؛ راجع قبل التكرار",
    "unknown": "نتيجة الإرسال غير مؤكدة؛ راجع قبل التكرار",
    "delivered": "وصلت وفق إيصال المزود",
    "read": "قُرئت وفق إيصال المزود",
    "queue_failed": "تعذر جدولة الإرسال",
    "blocked": "لم تُرسل؛ تغيرت شروط الإرسال",
    "failed": "فشل الإرسال",
}


def timestamp(value):
    try:
        at = datetime.fromisoformat(value)
        return at if timezone.is_aware(at) else None
    except (ValueError, TypeError):
        return None


def receipts(booking):
    result = []
    for channel, legacy, requests in KEYS:
        rows = list(booking.loyalty_state.get(requests, {}).items())
        if booking.loyalty_state.get(legacy):
            rows.append(("legacy", booking.loyalty_state[legacy]))
        for request_id, row in rows:
            result.append(
                {
                    "id": request_id,
                    "channel": channel,
                    "channel_label": "واتساب" if channel == "whatsapp" else "البريد الإلكتروني",
                    "status": row.get("status", "unknown"),
                    "status_label": LABELS.get(row.get("status"), "حالة غير معروفة"),
                    "requested_at": timestamp(row.get("requested_at")),
                    "updated_at": timestamp(row.get("accepted_at") or row.get("updated_at")),
                    "manual_resend": row.get("manual_resend", False),
                }
            )
    return sorted(
        result,
        key=lambda row: row["requested_at"].timestamp() if row["requested_at"] else 0,
        reverse=True,
    )


def fingerprint(booking):
    from .services import digest

    return digest(receipts(booking))


def pending(booking, *, exclude_id=None):
    return any(
        row["id"] != exclude_id
        and row["status"] in {"queued", "sending"}
        and (
            not row["requested_at"] or (timezone.now() - row["requested_at"]).total_seconds() < 900
        )
        for row in receipts(booking)
    )
