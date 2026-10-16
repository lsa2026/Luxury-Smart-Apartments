"""Two-member selection, explicit attestation and signed owner confirmation."""

import uuid

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Replace
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from apps.reservations.phone_search import phone_variants

from .launch import participant_allowed
from .localization import arabic_admin
from .models import Member, ReferralRevision
from .referrals import graph, link_members, validate_pair
from .services import digest, posting_transaction
from .views import _actions_allowed, _owner

SALT = "loyalty-owner-referral-link-v1"


def search_members(value):
    query = Member.objects.select_related("customer").filter(
        status="active", customer__is_active=True
    )
    variants = phone_variants(value)
    if variants:
        aliases = {}
        for i, field in enumerate(
            ("customer__customer_profile__phone", "customer__verifiedphone__phone")
        ):
            expression = F(field)
            for separator in (" ", "-", "(", ")", ".", "\u00a0"):
                expression = Replace(
                    expression, Value(separator), Value(""), output_field=CharField()
                )
            aliases[f"link_phone_{i}"] = expression
        query = query.alias(**aliases).filter(
            Q(link_phone_0__in=variants) | Q(link_phone_1__in=variants)
        )
    else:
        query = query.filter(number__iexact=value)
    return [m for m in query.order_by("pk")[:21] if participant_allowed(m.customer)]


def snapshot(links):
    return digest(
        [
            [key, r.inviter_id, r.tree_number, r.member.status, r.member.customer.is_active]
            for key, r in sorted(links.items())
        ]
    )


@arabic_admin
@never_cache
@staff_member_required
def admin_referrals(request):
    _owner(request)
    if not _actions_allowed(request):
        raise PermissionDenied
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    queries = {key: request.GET.get(key, "").strip()[:80] for key in ("member_q", "inviter_q")}
    selected, error = None, ""
    reason = request.POST.get("reason", "").strip()
    if request.method == "POST":
        try:
            if request.POST.get("action") == "review":
                links = graph()
                member_id, inviter_id = (
                    int(request.POST.get("member", "")),
                    int(request.POST.get("inviter", "")),
                )
                child, parent = validate_pair(member_id, inviter_id, links)
                if not 5 <= len(reason) <= 500:
                    raise ValidationError("اكتب سبب الربط وكيفية التحقق من الطرفين.")
                payload = {
                    "actor": request.user.pk,
                    "member": member_id,
                    "inviter": inviter_id,
                    "reason": reason,
                    "operation": str(uuid.uuid4()),
                    "snapshot": snapshot(links),
                }
                selected = {
                    "member": child.member,
                    "inviter": parent.member,
                    "reason": reason,
                    "token": signing.dumps(payload, salt=SALT),
                }
            elif request.POST.get("action") == "confirm":
                payload = signing.loads(
                    request.POST.get("confirmation", ""), salt=SALT, max_age=600
                )
                if payload["actor"] != request.user.pk or request.POST.get("verified") != "on":
                    raise ValidationError("أكد التحقق من الطرفين وموافقتهما على الربط.")
                with posting_transaction():
                    if (
                        not ReferralRevision.objects.filter(operation=payload["operation"]).exists()
                        and snapshot(graph()) != payload["snapshot"]
                    ):
                        raise ValidationError(
                            "تغيرت بيانات الشجرة بعد المراجعة؛ أعد اختيار الطرفين للتأكد."
                        )
                    link_members(
                        request.user,
                        payload["member"],
                        payload["inviter"],
                        reason=payload["reason"],
                        operation=payload["operation"],
                    )
                messages.success(
                    request, "تم ربط العضو بداعيه وتحديث الشجرة، دون تغيير النقاط السابقة."
                )
                return redirect("loyalty:referrals")
            else:
                raise ValidationError("اختر إجراء الربط من الصفحة.")
        except (ValueError, KeyError, TypeError, signing.BadSignature, ValidationError) as exc:
            error = (
                "؛ ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "انتهت المراجعة أو الطلب غير صالح؛ أعد اختيار الطرفين."
            )
    return render(
        request,
        "loyalty/referrals.html",
        {
            "title": "ربط الأعضاء بالداعين",
            "queries": queries,
            "member_matches": search_members(queries["member_q"]) if queries["member_q"] else [],
            "inviter_matches": search_members(queries["inviter_q"]) if queries["inviter_q"] else [],
            "confirmation": selected,
            "error": error,
            "reason": reason,
            "history": ReferralRevision.objects.filter(member_id=F("linked_member_id"))
            .select_related("member__customer", "inviter__customer", "operator")
            .order_by("-pk")[:10],
        },
    )
