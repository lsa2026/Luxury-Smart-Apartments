"""Site-native services. No network clients, tasks, public forms or financial providers."""

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Max, Sum
from django.utils import timezone

from apps.accounts.models import CustomerProfile
from apps.reservations.models import Reservation

from .calculation import amount, cents, reward_plan
from .models import Ledger, Member, Referral

RULES = "loyalty-approved-20261010-v1"
ZERO = Decimal("0.00")


def enabled():
    return settings.LOYALTY_ENABLED


def launch_at():
    try:
        moment = datetime.fromisoformat(settings.LOYALTY_LAUNCH_AT)
    except (ValueError, TypeError) as exc:
        raise ValidationError("تاريخ إطلاق البرنامج غير مثبت.") from exc
    if timezone.is_naive(moment):
        raise ValidationError("يلزم تاريخ إطلاق بمنطقة زمنية.")
    return moment


@contextmanager
def posting_transaction():
    if not enabled():
        raise ValidationError("برنامج الولاء غير مفعّل.")
    launch_at()
    with transaction.atomic():
        # Small portfolio: one consistent financial lock, held until commit.
        # SQLite tests do not prove multi-connection PostgreSQL concurrency.
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(190009, 8)")
        yield


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def member_for(user, *, active=True):
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        raise PermissionDenied("سجل الدخول بحسابك.")
    query = Member.objects.select_related("customer", "referral").filter(customer=user)
    if active:
        query = query.filter(status="active")
    member = query.first()
    if member is None:
        raise ValidationError("عضوية غير متاحة.")
    return member


def balance(member):
    sums = Ledger.objects.filter(member=member).aggregate(c=Sum("credit"), d=Sum("debit"))
    return (sums["c"] or ZERO) - (sums["d"] or ZERO)


def available(member):
    held = sum(
        (
            Decimal(row.get("hold", {}).get("value", "0"))
            for row in Reservation.objects.filter(
                booking_intent__customer=member.customer,
            ).values_list("loyalty_state", flat=True)
        ),
        ZERO,
    )
    return balance(member) - held


def receipt(member, key, payload, work):
    if not isinstance(key, str) or not 3 <= len(key) <= 200:
        raise ValidationError("مرجع طلب ثابت مطلوب.")
    member.refresh_from_db()
    fingerprint = digest(payload)
    old = member.command_receipts.get(key)
    if old:
        if old["hash"] != fingerprint:
            raise ValidationError("استعمل مرجع الطلب ببياناته الأصلية فقط.")
        return old["result"]
    result = work()
    # Work may update corrections/invitation state: do not overwrite them.
    member.refresh_from_db()
    member.command_receipts[key] = {
        "hash": fingerprint,
        "result": result,
        "at": timezone.now().isoformat(),
    }
    member.save(update_fields=["command_receipts", "updated_at"])
    return result


def _post(member, group, branch, *, credit=ZERO, debit=ZERO, **fields):
    credit, debit = amount(cents(credit)), amount(cents(debit))
    if (credit > 0) == (debit > 0) or debit > available(member):
        raise ValidationError("حركة غير متوازنة أو الرصيد المتاح لا يكفي.")
    sequence = (Ledger.objects.aggregate(n=Max("sequence"))["n"] or 0) + 1
    totals = Ledger.objects.aggregate(c=Sum("credit"), d=Sum("debit"))
    program = (totals["c"] or ZERO) - (totals["d"] or ZERO)
    row = Ledger.objects.create(
        sequence=sequence,
        operation_group=group,
        branch=branch,
        operation_number=f"{group}-{branch:02}",
        member=member,
        customer=member.customer,
        membership_number=member.number,
        tree_number=member.referral.tree_number,
        account_name=member.customer.get_full_name() or member.customer.email,
        credit=credit,
        debit=debit,
        member_balance_after=balance(member) + credit - debit,
        program_balance_after=program + credit - debit,
        rules_version=RULES,
        **fields,
    )
    if settings.LOYALTY_MESSAGES_ENABLED:
        from .notifications import queue_movement

        transaction.on_commit(lambda: queue_movement(row.pk), robust=True)
    return row


def _group():
    n = (Ledger.objects.aggregate(n=Max("sequence"))["n"] or 0) + 1
    return f"LSA{n:06}"


def _booking(user, reservation_id):
    booking = Reservation.objects.select_related(
        "booking_intent__customer",
        "booking_intent__quote",
        "property",
    ).get(pk=reservation_id)
    if not booking.booking_intent_id or booking.booking_intent.customer_id != user.pk:
        raise PermissionDenied("الحجز لا يخص هذا الحساب.")
    if booking.source_type != Reservation.SourceType.DIRECT_WEBSITE:
        raise ValidationError("الحجز ليس من موقعنا أو إدارة الموقع.")
    if booking.created_at < launch_at():
        raise ValidationError("الحجوزات السابقة للإطلاق لا تدخل في البرنامج.")
    return booking


def _save_booking(booking):
    booking.save(update_fields=["loyalty_state", "updated_at"])


def enroll(
    user,
    *,
    consent,
    terms_version,
    terms_reference,
    language="ar",
    invitation_token=None,
    confirmed_inviter=None,
):
    with posting_transaction():
        if not getattr(user, "is_authenticated", False) or not user.is_active:
            raise PermissionDenied("يلزم حساب ضيف موثق.")
        if not CustomerProfile.objects.filter(user=user, email_verified_at__isnull=False).exists():
            raise PermissionDenied("تحقق من بريد الحساب أولًا.")
        if (
            consent is not True
            or not terms_version
            or not terms_reference
            or language
            not in {
                "ar",
                "en",
                "fr",
            }
        ):
            raise ValidationError("الموافقة الصريحة ونسخة الشروط ومرجعها مطلوبة.")
        existing = Member.objects.filter(customer=user).first()
        if existing:
            if existing.status != "active":
                raise ValidationError("إعادة عضوية ملغاة تنتظر اعتماد الأحكام.")
            return existing
        parent = None
        if invitation_token:
            parent = (
                Member.objects.select_related("referral")
                .filter(
                    invitation_token=invitation_token,
                    status="active",
                    first_completed_at__isnull=False,
                )
                .first()
            )
            if parent is None or str(parent.pk) != str(confirmed_inviter):
                raise ValidationError("أكد الداعي المطابق لرابط الدعوة.")
        elif confirmed_inviter is not None:
            raise ValidationError("تأكيد الداعي يحتاج رابط دعوة صالحًا.")
        now = timezone.now()
        member = Member.objects.create(
            customer=user,
            number=f"pending-{secrets.token_hex(10)}",
            consent_at=now,
            joined_at=now,
            terms_version=terms_version,
            terms_reference=terms_reference,
            language=language,
            consent_history=[
                {
                    "accepted_at": now.isoformat(),
                    "version": terms_version,
                    "reference": terms_reference,
                    "language": language,
                    "source": "authenticated-account",
                }
            ],
        )
        member.number = f"LSA-M{member.pk:06}"
        member.save(update_fields=["number"])
        branch = (
            Referral.objects.filter(inviter=parent).aggregate(n=Max("branch_order"))["n"] or 0
        ) + 1
        path = f"{parent.referral.tree_number}-{branch:04}" if parent else f"{branch:04}"
        Referral.objects.create(
            member=member,
            inviter=parent,
            root_id=parent.referral.root_id if parent else member.pk,
            tree_number=path,
            depth=parent.referral.depth + 1 if parent else 0,
            branch_order=branch,
            evidence={
                "confirmed_inviter": parent.pk if parent else None,
                "source": "authenticated-account",
            },
        )
        # Joining during a current stay re-evaluates its authoritative facts.
        for booking in Reservation.objects.filter(booking_intent__customer=user):
            reconcile(booking.pk)
        return member


def confirm_arrival(operator, reservation_id, *, arrived_at, reference):
    with posting_transaction():
        if (
            not operator.is_active
            or not operator.is_staff
            or not operator.has_perm(
                "reservations.change_reservation",
            )
        ):
            raise PermissionDenied("تأكيد الوصول للإدارة أو الاستقبال المخول فقط.")
        booking = Reservation.objects.select_related("booking_intent", "property").get(
            pk=reservation_id,
        )
        if not booking.booking_intent_id:
            raise ValidationError("لا يوجد حجز موقع مرتبط.")
        if booking.source_type != Reservation.SourceType.DIRECT_WEBSITE:
            raise ValidationError("هذا الحجز خارج برنامج الموقع.")
        if booking.created_at < launch_at() or booking.normalized_status not in {
            Reservation.Status.CONFIRMED,
            Reservation.Status.MODIFIED,
        }:
            raise ValidationError("الحجز غير مؤهل لتأكيد الوصول.")
        if timezone.is_naive(arrived_at) or arrived_at > timezone.now() or not reference:
            raise ValidationError("يلزم وقت وصول فعلي سابق ومرجع موثق.")
        from .site_adapter import stay_times

        start, end = stay_times(booking)
        if arrived_at < start or arrived_at >= end:
            raise ValidationError("وقت الوصول خارج فترة الإقامة المعتمدة.")
        old = booking.loyalty_state.get("arrival")
        evidence = {
            "at": arrived_at.isoformat(),
            "reference": reference,
            "operator": operator.pk,
            "recorded_at": timezone.now().isoformat(),
        }
        if old:
            if old["at"] != evidence["at"] or old["reference"] != reference:
                raise ValidationError("الوصول موثق بالفعل؛ لا يستبدل بصمت.")
        else:
            booking.loyalty_state["arrival"] = evidence
            _save_booking(booking)
        return reconcile(booking.pk)


def issue_invite(user, reservation_id):
    with posting_transaction():
        member = member_for(user)
        if member.invitation_token:
            return member.invitation_token
        booking = _booking(user, reservation_id)
        from .site_adapter import booking_facts, stay_times

        _, end = stay_times(booking)
        if (
            not booking.loyalty_state.get("arrival")
            or end > timezone.now()
            or booking.normalized_status not in Reservation.ACTIVE_STATUSES
            or member.consent_at > end
        ):
            raise ValidationError("رابط الدعوة بعد أول إقامة مكتملة فقط.")
        # A recorded arrival and elapsed checkout alone do not prove a paid,
        # qualifying stay. Reuse the same authoritative receipt/allocation
        # checks as accrual; full-points stays qualify without a fake receipt.
        _facts, blocker = booking_facts(booking)
        if blocker:
            raise ValidationError("يلزم توثيق سداد الإقامة المؤهلة قبل إصدار الدعوة.")
        member.first_completed_reservation = booking
        member.first_completed_at = end
        member.invitation_token = secrets.token_urlsafe(32)
        member.invitation_issued_at = timezone.now()
        member.save(
            update_fields=[
                "first_completed_reservation",
                "first_completed_at",
                "invitation_token",
                "invitation_issued_at",
                "updated_at",
            ]
        )
        return member.invitation_token


def ancestors(member):
    result, seen = [], {member.pk}
    parent = member.referral.inviter
    while parent:
        if parent.pk in seen:
            raise ValidationError("علاقة الدعوة تحتوي دورة غير صحيحة.")
        seen.add(parent.pk)
        result.append(parent)
        parent = parent.referral.inviter
    return result


def _absorb(member, budget, event, group, branch):
    member.refresh_from_db()
    remaining = []
    for claim in member.deferred_corrections:
        due = Decimal(claim["remaining"])
        take = min(budget, due, available(member))
        if take:
            branch += 1
            _post(
                member,
                group,
                branch,
                debit=take,
                event_type="adjust",
                operation_type=claim["type"],
                component=claim["component"],
                reservation_id=claim["booking"],
                original_id=claim["original"],
                event_reference=f"{event}:offset:{claim['booking']}:{claim['component']}",
                reason="تعويض نقص سابق من مكافأة جديدة فقط",
            )
        budget -= take
        if due > take:
            remaining.append({**claim, "remaining": str(due - take)})
    member.deferred_corrections = remaining
    member.save(update_fields=["deferred_corrections", "updated_at"])
    return branch


def reconcile(reservation_id):
    """Re-read site facts, never accept base/payment/arrival assertions from a guest."""
    if not enabled():
        return {"status": "disabled", "movements": 0}
    with posting_transaction():
        booking = Reservation.objects.select_related(
            "booking_intent__customer",
            "booking_intent__quote",
            "property",
        ).get(pk=reservation_id)
        from .site_adapter import booking_facts

        facts, blocker = booking_facts(booking)
        if blocker:
            booking.loyalty_state["review"] = {"reason": blocker, "at": timezone.now().isoformat()}
            _save_booking(booking)
            return {"status": blocker, "movements": 0}
        owner = Member.objects.select_related("customer", "referral").get(
            customer=booking.booking_intent.customer,
        )
        chain = ancestors(owner)
        key = digest(facts)
        previous = booking.loyalty_state.get("settlement", {})
        if previous.get("hash") == key:
            return {"status": "unchanged", "movements": 0}
        revision = previous.get("revision", 0) + 1
        event = f"settle:{booking.pk}:{revision}"
        group, branch = _group(), 0
        for (
            target_member,
            kind,
            rate,
            target_cents,
            distance,
            numerator,
            denominator,
        ) in reward_plan(
            facts["base"],
            owner,
            chain,
        ):
            member = Member.objects.select_related("customer", "referral").get(pk=target_member.pk)
            target = amount(target_cents) if member.status == "active" else ZERO
            rows = Ledger.objects.filter(
                reservation=booking,
                member=member,
                component=kind,
                event_type__in=["earn", "adjust"],
            )
            totals = rows.aggregate(c=Sum("credit"), d=Sum("debit"))
            current = (totals["c"] or ZERO) - (totals["d"] or ZERO)
            original = rows.filter(event_type="earn").first()
            # Re-compute this booking's claim, preserving claims from other stays.
            claims = [
                c
                for c in member.deferred_corrections
                if not (c["booking"] == str(booking.pk) and c["component"] == kind)
            ]
            member.deferred_corrections = claims
            member.save(update_fields=["deferred_corrections", "updated_at"])
            common = dict(
                reservation=booking,
                property=booking.property,
                booking_total=Decimal(facts["total"]),
                booking_customer=owner.customer,
                booking_guest_name=owner.customer.get_full_name(),
                reservation_reference=booking.public_reference,
                property_name=booking.property.name_ar or booking.property.hostaway_name,
                hostaway_reservation_id=booking.hostaway_reservation_id,
                original_booking_amount=booking.total_price,
                booking_currency=booking.currency,
                check_in_at=datetime.fromisoformat(facts["check_in"]),
                check_out_at=datetime.fromisoformat(facts["check_out"]),
                actual_arrival_at=datetime.fromisoformat(facts["arrival"]["at"]),
                arrival_reference=facts["arrival"]["reference"],
                eligible_lodging_sar=Decimal(facts["base"]),
                verified_cash_sar=Decimal(facts["cash"]),
                refunded_cash_sar=Decimal(facts["refunded"]),
                points_used=Decimal(facts["points"]),
                settlement_revision=revision,
                direct_inviter=chain[0] if chain else None,
                beneficiary_distance=distance,
                older_inviter_count=max(len(chain) - 1, 0),
                allocation_numerator=numerator,
                allocation_denominator=denominator,
                rate_percent=Decimal(rate),
                operation_type=kind,
                component=kind,
                event_reference=event,
                evidence=facts,
            )
            if target > current:
                branch += 1
                _post(
                    member,
                    group,
                    branch,
                    credit=target - current,
                    event_type="adjust" if original else "earn",
                    original=original,
                    reason="استحقاق الإقامة بعد القبض والوصول والموافقة",
                    **common,
                )
                branch = _absorb(member, target - current, event, group, branch)
            elif target < current:
                due = current - target
                take = min(due, available(member))
                if not original:
                    raise ValidationError("التصحيح يحتاج حركة أصلية.")
                if take:
                    branch += 1
                    _post(
                        member,
                        group,
                        branch,
                        debit=take,
                        event_type="adjust",
                        original=original,
                        reason="تصحيح الاستحقاق بعد تعديل أو إلغاء الإقامة",
                        **common,
                    )
                if due > take:
                    member.deferred_corrections = claims + [
                        {
                            "booking": str(booking.pk),
                            "component": kind,
                            "type": kind,
                            "original": original.pk,
                            "remaining": str(due - take),
                        }
                    ]
                    member.save(update_fields=["deferred_corrections", "updated_at"])
        booking.loyalty_state.pop("review", None)
        booking.loyalty_state["settlement"] = {"revision": revision, "hash": key, "facts": facts}
        _save_booking(booking)
        return {"status": "settled", "movements": branch, "base": facts["base"]}


def transfer(user, request_reference, recipient_number, value):
    with posting_transaction():
        sender = member_for(user)

        def work():
            recipient = Member.objects.select_related("customer", "referral").get(
                number=recipient_number,
                status="active",
            )
            n = amount(cents(value))
            if n <= 0 or sender.pk == recipient.pk or n > available(sender):
                raise ValidationError("تحويل غير صالح أو الرصيد لا يكفي.")
            group = _group()
            fields = dict(
                operation_type="transfer",
                event_type="transfer",
                reason="تحويل نقاط",
                event_reference=f"transfer:{sender.pk}:{request_reference}",
                request_reference=request_reference,
                evidence={"request_hash": digest([recipient_number, str(n)])},
            )
            _post(
                sender, group, 1, debit=n, counterpart=recipient, component="transfer-out", **fields
            )
            _post(
                recipient, group, 2, credit=n, counterpart=sender, component="transfer-in", **fields
            )
            return {"group": group, "value": str(n)}

        return receipt(sender, request_reference, ["transfer", recipient_number, str(value)], work)


def hold(user, request_reference, reservation_id, value):
    with posting_transaction():
        member = member_for(user)

        def work():
            booking = _booking(user, reservation_id)
            if (
                booking.normalized_status
                not in {
                    Reservation.Status.AWAITING_PAYMENT,
                    Reservation.Status.READY_FOR_HOSTAWAY,
                }
                or booking.loyalty_state.get("hold")
                or booking.loyalty_state.get("redemption")
            ):
                raise ValidationError("هذا الحجز غير متاح لحجز نقاط جديد.")
            from apps.payments.models import PaymentAttempt

            if (
                PaymentAttempt.objects.filter(booking_intent=booking.booking_intent)
                .exclude(
                    status__in=[
                        PaymentAttempt.Status.FAILED,
                        PaymentAttempt.Status.CANCELLED,
                        PaymentAttempt.Status.EXPIRED,
                    ],
                )
                .exists()
            ):
                raise ValidationError(
                    "اختر النقاط قبل إنشاء عملية الدفع؛ توجد عملية قائمة لهذا الحجز."
                )
            n = amount(cents(value))
            # Until mixed-currency/allocation approval, redemption integration is SAR only.
            if (
                booking.currency != "SAR"
                or n <= 0
                or n > booking.total_price
                or n > available(member)
            ):
                raise ValidationError("قيمة النقاط غير متاحة لهذا الحجز.")
            booking.loyalty_state["hold"] = {
                "value": str(n),
                "member": member.pk,
                "request": request_reference,
            }
            _save_booking(booking)
            return {"value": str(n), "reservation": str(booking.pk)}

        return receipt(member, request_reference, ["hold", str(reservation_id), str(value)], work)


def release(user, request_reference, reservation_id):
    with posting_transaction():
        member = member_for(user)

        def work():
            booking = _booking(user, reservation_id)
            from .checkout import release_allowed

            release_allowed(booking)
            old = booking.loyalty_state.pop("hold", {}).get("value", "0.00")
            booking.loyalty_state.pop("checkout_frozen", None)
            _save_booking(booking)
            return {"value": old}

        return receipt(member, request_reference, ["release", str(reservation_id)], work)


def redeem(user, request_reference, reservation_id):
    with posting_transaction():
        member = member_for(user)

        def work():
            booking = _booking(user, reservation_id)
            state = booking.loyalty_state
            if state.get("redemption"):
                return {"value": state["redemption"]["value"]}
            held = state.get("hold")
            if (
                booking.normalized_status not in Reservation.ACTIVE_STATUSES
                or not held
                or held["member"] != member.pk
            ):
                raise ValidationError("يلزم حجز مؤكد ونقاط محجوزة للحساب نفسه.")
            n = Decimal(held["value"])
            # Removing the hold makes exactly its value available for its single debit.
            state.pop("hold")
            _save_booking(booking)
            row = _post(
                member,
                _group(),
                1,
                debit=n,
                reservation=booking,
                property=booking.property,
                operation_type="transfer",
                event_type="redeem",
                component="redemption",
                event_reference=f"redeem:{booking.pk}",
                request_reference=request_reference,
                reason="استخدام نقاط محجوزة في حجز مؤكد",
            )
            state["redemption"] = {"value": str(n), "original": row.pk, "restored": "0.00"}
            _save_booking(booking)
            return {"value": str(n)}

        return receipt(member, request_reference, ["redeem", str(reservation_id)], work)


def restore_points(reservation_id, cumulative_value, evidence_reference):
    """Trusted refund service only; no guest endpoint accepts cumulative_value."""
    with posting_transaction():
        booking = Reservation.objects.select_related("booking_intent__customer", "property").get(
            pk=reservation_id,
        )
        member = member_for(booking.booking_intent.customer)
        r = booking.loyalty_state.get("redemption")
        if not r or not evidence_reference:
            raise ValidationError("لا توجد حركة صرف موثقة أو مرجع إعادة.")
        target, previous = amount(cents(cumulative_value)), Decimal(r["restored"])
        if target < previous or target > Decimal(r["value"]):
            raise ValidationError("الإعادة تتجاوز الصرف أو تناقض إعادة سابقة.")
        if target == previous:
            return ZERO
        _post(
            member,
            _group(),
            1,
            credit=target - previous,
            reservation=booking,
            property=booking.property,
            operation_type="addition",
            event_type="restore",
            component="restore",
            original_id=r["original"],
            event_reference=f"restore:{booking.pk}:{target}",
            evidence={"reference": evidence_reference},
            reason="إعادة النقاط المستخدمة",
        )
        r["restored"] = str(target)
        _save_booking(booking)
        return target - previous


def statement(user):
    member = member_for(user, active=False)
    with posting_transaction():
        return {
            "membership": member.number,
            "tree_number": member.referral.tree_number,
            "balance": balance(member),
            "available": available(member),
            "rows": Ledger.objects.filter(member=member).order_by("sequence"),
        }


def withdraw(user, request_reference):
    """Voluntary closing only; legal effects on active stays remain unapproved."""
    with posting_transaction():
        member = member_for(user, active=False)

        def work():
            if member.status == "withdrawn":
                return {"status": "withdrawn"}
            from .site_adapter import stay_times

            bookings = Reservation.objects.select_related("property").filter(
                booking_intent__customer=user,
            )
            for booking in bookings:
                if booking.loyalty_state.get("hold") or (
                    booking.normalized_status != Reservation.Status.CANCELLED
                    and stay_times(booking)[1] > timezone.now()
                ):
                    raise ValidationError("إقفال العضوية مع حجز قائم ينتظر اعتماد الأحكام.")
            value = balance(member)
            if value:
                _post(
                    member,
                    _group(),
                    1,
                    debit=value,
                    operation_type="transfer",
                    event_type="withdraw",
                    component="closing",
                    event_reference=f"withdraw:{member.pk}",
                    request_reference=request_reference,
                    reason="إقفال العضوية اختياريًا وإسقاط النقاط وفق القاعدة المعتمدة",
                )
            member.status, member.withdrawn_at = "withdrawn", timezone.now()
            member.save(update_fields=["status", "withdrawn_at", "updated_at"])
            return {"status": "withdrawn", "forfeited": str(value)}

        return receipt(member, request_reference, ["withdraw"], work)
