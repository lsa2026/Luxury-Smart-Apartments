"""Owner-only missing-link repair. Never reassign an existing direct inviter."""

import uuid

from django.core.exceptions import ValidationError
from django.db.models import BigIntegerField, F, Max, OuterRef, Subquery
from django.db.models.functions import Coalesce

from apps.accounts.access import require_operations_owner
from apps.reservations.models import Reservation

from .launch import participant_allowed
from .models import Referral, ReferralRevision

MAX_TREE = 10000
FIELDS = ("inviter_id", "root_id", "tree_number", "depth", "branch_order")


def current_query():
    latest = ReferralRevision.objects.filter(member_id=OuterRef("member_id")).order_by("-pk")
    return Referral.objects.select_related("member__customer").annotate(
        **{
            f"current_{f}": Coalesce(
                Subquery(latest.values(f)[:1]),
                F(f),
                output_field=BigIntegerField()
                if f.endswith("_id")
                else Referral._meta.get_field(f),
            )
            for f in FIELDS
        }
    )


def _current(row):
    for field in FIELDS:
        setattr(row, field, getattr(row, f"current_{field}"))
    return row


def current_link(member_id):
    return _current(current_query().get(member_id=member_id))


def graph():
    # Bounded admin branch operation; normal accrual never loads the entire graph.
    rows = list(current_query().order_by("pk")[: MAX_TREE + 1])
    if len(rows) > MAX_TREE:
        raise ValidationError("يلزم توسيع أداة الشجرة قبل متابعة الربط.")
    result = {}
    for row in rows:
        result[row.member_id] = _current(row)
    return result


def chain_ids(member_id, links):
    result, seen = [], {member_id}
    while True:
        if member_id not in links:
            raise ValidationError("بيانات علاقة العضوية غير مكتملة.")
        parent = links[member_id].inviter_id
        if parent is None:
            return result
        if parent in seen:
            raise ValidationError("لا يمكن تكوين دائرة في علاقات الدعوة.")
        seen.add(parent)
        result.append(parent)
        member_id = parent


def next_branch(inviter_id):
    # Do not reuse an old slot after moving a branch; old ledger paths remain meaningful.
    return (
        max(
            Referral.objects.filter(inviter_id=inviter_id).aggregate(n=Max("branch_order"))["n"]
            or 0,
            ReferralRevision.objects.filter(inviter_id=inviter_id).aggregate(n=Max("branch_order"))[
                "n"
            ]
            or 0,
        )
        + 1
    )


def validate_pair(member_id, inviter_id, links):
    from .services import invitation_eligible

    if member_id == inviter_id:
        raise ValidationError("لا يمكن للعضو دعوة نفسه.")
    if member_id not in links or inviter_id not in links:
        raise ValidationError("يلزم انضمام الطرفين إلى برنامج الولاء أولًا.")
    child, parent = links[member_id], links[inviter_id]
    for row in (child, parent):
        m = row.member
        if m.status != "active" or not m.customer.is_active or not participant_allowed(m.customer):
            raise ValidationError("إحدى العضويتين غير نشطة أو خارج نطاق البرنامج الحالي.")
    if child.inviter_id is not None:
        if child.inviter_id == inviter_id:
            raise ValidationError("العضو مرتبط بهذا الداعي بالفعل؛ لا حاجة لإعادة الربط.")
        raise ValidationError("للعضو داعٍ مباشر مسجل بالفعل؛ لا يمكن إضافة داعٍ ثانٍ أو استبداله هنا.")
    if member_id in chain_ids(inviter_id, links):
        raise ValidationError("هذا الربط يكوّن دائرة دعوات؛ اختر داعيًا من خارج فرع العضو.")
    if not invitation_eligible(parent.member):
        raise ValidationError("الداعي لم يستوفِ شرط أهلية الدعوة بعد أول إقامة مكتملة.")
    return child, parent


def link_members(operator, member_id, inviter_id, *, reason, operation):
    from .services import posting_transaction

    require_operations_owner(operator)
    reason = reason.strip()
    if not 5 <= len(reason) <= 500:
        raise ValidationError("اكتب سبب الربط والتحقق من الطرفين بوضوح.")
    try:
        operation = uuid.UUID(str(operation))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("طلب الربط غير صالح؛ أعد المراجعة.") from exc
    with posting_transaction():
        old = ReferralRevision.objects.filter(operation=operation, member_id=member_id).first()
        if old:
            if (
                old.inviter_id != inviter_id
                or old.reason != reason
                or old.operator_id != operator.pk
            ):
                raise ValidationError("لا يمكن استخدام مرجع الربط لعملية مختلفة.")
            return old
        links = graph()
        child, parent = validate_pair(member_id, inviter_id, links)
        # All descendants move together. An already-linked parent may invite unlimited members.
        affected = {member_id}
        while True:
            added = {key for key, row in links.items() if row.inviter_id in affected} - affected
            if not added:
                break
            affected.update(added)
        # Existing accruals retain their beneficiary chain for later refunds/corrections.
        users = [links[key].member.customer_id for key in affected]
        for booking in Reservation.objects.filter(
            booking_intent__customer_id__in=users
        ).select_related("booking_intent"):
            state = booking.loyalty_state
            if state.get("settlement") and "referral_chain" not in state:
                booking_member = next(
                    key
                    for key in affected
                    if links[key].member.customer_id == booking.booking_intent.customer_id
                )
                state["referral_chain"] = chain_ids(booking_member, links)
                booking.save(update_fields=["loyalty_state", "updated_at"])
        branch = next_branch(inviter_id)
        path = f"{parent.tree_number}-{branch:04}"
        now_root, depth_delta = parent.root_id, parent.depth + 1 - child.depth
        rows = []
        for key in sorted(affected, key=lambda k: links[k].depth):
            before = links[key]
            rows.append(
                ReferralRevision.objects.create(
                    member_id=key,
                    inviter_id=inviter_id if key == member_id else before.inviter_id,
                    root_id=now_root,
                    tree_number=path + before.tree_number[len(child.tree_number) :],
                    depth=before.depth + depth_delta,
                    branch_order=branch if key == member_id else before.branch_order,
                    operation=operation,
                    linked_member_id=member_id,
                    operator=operator,
                    reason=reason,
                )
            )
            from .events import record
            from .models import GuestEvent

            revision = rows[-1]
            record(
                customer=before.member.customer,
                member=before.member,
                actor=operator,
                kind=GuestEvent.Kind.REFERRAL,
                source="admin",
                reference=f"referral-revision:{revision.pk}",
                reason=reason,
                occurred_at=revision.fixed_at,
                before={"inviter": before.inviter_id, "tree_number": before.tree_number},
                after={"inviter": revision.inviter_id, "tree_number": revision.tree_number},
                evidence={"operation": str(operation), "linked_member": member_id},
            )
        return rows[0]
