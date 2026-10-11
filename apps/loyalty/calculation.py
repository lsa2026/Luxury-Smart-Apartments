"""Exact integer arithmetic, ported from the independently tested stage-seven engine."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


def cents(value):
    if isinstance(value, bool | float):
        raise ValueError("قيمة النقاط يجب أن تكون عشرية دقيقة.")
    try:
        n = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("قيمة النقاط غير صحيحة.") from exc
    if not n.is_finite() or n < 0 or n >= Decimal("100000000000000") or n * 100 != int(n * 100):
        raise ValueError("اعتمد خانتين عشريتين فقط وقيمة غير سالبة.")
    return int(n * 100)


def amount(value):
    return Decimal(value) / 100


def reward_plan(base, owner, ancestors):
    value = cents(base)
    rows = [(owner, "loyalty", 3, value * 3 // 100, 0, 3, 1)]
    if ancestors:
        rows.append((ancestors[0], "direct_referral", 5, value * 5 // 100, 1, 5, 1))
    older = ancestors[1:]
    count = len(older)
    if count:
        cap = value // 100
        pool = min(value * min(count, 7) // 100, cap * count)
        quotient, remainder = divmod(pool, count)
        rows.extend(
            (
                member,
                "indirect_referral",
                1,
                quotient + (index < remainder),
                index + 2,
                min(count, 7),
                count,
            )
            for index, member in enumerate(older)
        )
    return rows


def refund_split(refundable, cash, points):
    r, c, p = cents(refundable), cents(cash), cents(points)
    if r > c + p:
        raise ValueError("الاسترداد يتجاوز الدفع الأصلي.")
    cash_part = r * c // (c + p) if c + p else 0
    return {"cash": amount(cash_part), "points": amount(r - cash_part)}


def departure_refund(nights, vacated_at: datetime, zone: str, *, arrived=True):
    if vacated_at.tzinfo is None:
        raise ValueError("يلزم وقت مغادرة موثق بمنطقته الزمنية.")
    local = vacated_at.astimezone(ZoneInfo(zone))
    dates = set()
    total = 0
    for night in nights:
        date = datetime.strptime(night["date"], "%Y-%m-%d").date()
        if date in dates:
            raise ValueError("قائمة الليالي مكررة.")
        dates.add(date)
        value = cents(night["value"])
        if not arrived or date > local.date() or (date == local.date() and local.hour < 20):
            total += value
    return amount(total)
