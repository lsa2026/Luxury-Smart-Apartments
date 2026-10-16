"""Private, gated guest/owner interfaces. No payment or messaging endpoints."""

import secrets
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import Q, Sum
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import get_language
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.access import require_operations_owner
from apps.accounts.dashboard_copy import account_copy
from apps.accounts.models import CustomerProfile, VerifiedPhone
from apps.accounts.phone_auth import allowed as phone_allowed
from apps.accounts.phone_auth import ready as phone_ready
from apps.accounts.phone_copy import phone_copy
from apps.reservations.models import Reservation

from .copy import copy, transaction_label
from .forms import GiftInvitationForm, JoinForm, PhoneGiftForm, TransferForm
from .invitation_links import invitation_url
from .launch import (
    participant_allowed,
    pilot_only,
    pilot_references,
    stay_exception_allowed,
    stay_exception_references,
)
from .localization import arabic_admin
from .models import Ledger, Member
from .services import (
    available,
    confirm_arrival,
    enroll,
    invitation_eligible,
    issue_invite,
    posting_transaction,
)

SALT = "loyalty-transfer-confirmation-v1"
GIFT_SALT = "loyalty-gift-invitation-confirmation-v1"
ZERO = Decimal("0.00")


def interface_enabled(user=None):
    return settings.LOYALTY_ENABLED and settings.LOYALTY_UI_ENABLED and participant_allowed(user)


def preview_mode():
    # A preview flag cannot enable draft consent on a deployed production database.
    return settings.LOYALTY_UI_PREVIEW and settings.DEBUG and connection.vendor == "sqlite"


def approved_terms():
    reference = settings.LOYALTY_TERMS_REFERENCE
    parsed = urlparse(reference)
    safe = (
        reference.startswith("/")
        and not reference.startswith("//")
        or (parsed.scheme == "https" and bool(parsed.netloc))
    )
    return settings.LOYALTY_TERMS_APPROVED and bool(settings.LOYALTY_TERMS_VERSION) and safe


def _actions_allowed(request):
    return approved_terms() or (
        preview_mode()
        and request.get_host().split(":")[0] in {"localhost", "127.0.0.1", "testserver"}
    )


def _verified(user):
    return CustomerProfile.objects.filter(user=user, email_verified_at__isnull=False).exists()


def _throttle(user):
    key = f"loyalty-ui:{user.pk}:{int(timezone.now().timestamp()) // 60}"
    if cache.add(key, 1, timeout=65):
        return
    try:
        count = cache.incr(key)
    except ValueError:
        count = 31
    if count > 30:
        raise ValidationError("request limit")


def _rows(query, request):
    # Keyset pagination: never load an unbounded ledger or join other guests' details.
    try:
        before = int(request.GET.get("before", "0"))
    except ValueError:
        before = 0
    if not 0 <= before <= 9223372036854775807:
        before = 0
    if before > 0:
        query = query.filter(sequence__lt=before)
    rows = list(query.order_by("-sequence")[:21])
    cursor = rows[19].sequence if len(rows) > 20 else None
    return rows[:20], cursor


def _decorate(rows):
    for row in rows:
        row.ui_kind = transaction_label(row)
        # Exact allocation ratio, not the one-percent ceiling saved in the model.
        row.ui_rate = (
            Decimal(row.allocation_numerator) / Decimal(row.allocation_denominator)
            if row.allocation_denominator
            else row.rate_percent
        )


@never_cache
def guest_rewards(request):
    if not request.user.is_authenticated or not participant_allowed(request.user):
        raise PermissionDenied
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    labels = copy()
    member = (
        Member.objects.select_related("customer", "referral").filter(customer=request.user).first()
    )
    invitation = request.GET.get("invite", "")[:100]
    inviter = None
    invitation_invalid = False
    if invitation and member is None:
        inviter = Member.objects.filter(invitation_token=invitation, status="active").first()
        if inviter and (
            not participant_allowed(inviter.customer) or not invitation_eligible(inviter)
        ):
            inviter = None
        invitation_invalid = inviter is None
    can_act = _actions_allowed(request)
    join_form = JoinForm(has_inviter=bool(inviter))
    transfer_form = TransferForm()
    phone_gift_form = PhoneGiftForm()
    gift_invitation = None
    invitation_form = None
    error, confirmation = "", None
    if request.method == "POST":
        if not can_act:
            raise PermissionDenied
        if not _verified(request.user):
            raise PermissionDenied
        try:
            _throttle(request.user)
            action = request.POST.get("action")
            if action == "join" and member is None:
                join_form = JoinForm(request.POST, has_inviter=bool(inviter))
                if invitation_invalid or not join_form.is_valid():
                    raise ValidationError("invalid")
                member = enroll(
                    request.user,
                    consent=True,
                    terms_version=settings.LOYALTY_TERMS_VERSION
                    if approved_terms()
                    else "local-preview-not-legal-v1",
                    terms_reference=settings.LOYALTY_TERMS_REFERENCE
                    if approved_terms()
                    else "local-fictional-consent",
                    language=(get_language() or "ar").split("-")[0],
                    invitation_token=invitation or None,
                    confirmed_inviter=inviter.pk if inviter else None,
                )
            elif action == "transfer_review" and member and member.status == "active":
                transfer_form = TransferForm(request.POST)
                if not transfer_form.is_valid():
                    raise ValidationError("invalid")
                recipient = get_object_or_404(
                    Member, number=transfer_form.cleaned_data["recipient"], status="active"
                )
                if not recipient.customer.is_active or not participant_allowed(recipient.customer):
                    raise ValidationError("invalid")
                value = transfer_form.cleaned_data["amount"]
                if recipient.pk == member.pk or value > available(member):
                    raise ValidationError("invalid")
                payload = {
                    "user": request.user.pk,
                    "recipient": recipient.number,
                    "value": str(value),
                    "request": f"ui-{secrets.token_urlsafe(24)}",
                }
                from .phone_gifts import masked_name

                confirmation = {
                    **payload,
                    "name": masked_name(recipient),
                    "token": signing.dumps(payload, salt=SALT),
                }
            elif action == "gift_phone_review" and member and member.status == "active":
                from apps.accounts.phone_auth import _limit

                from .phone_gifts import (
                    masked_name,
                    phone_hash,
                    recipient_for_phone,
                    recipient_language,
                )

                phone_gift_form = PhoneGiftForm(request.POST)
                if not phone_gift_form.is_valid() or not _limit(
                    "gift-lookup-user", request.user.pk, 10, 300
                ):
                    raise ValidationError("invalid")
                phone = phone_gift_form.cleaned_data["phone"]
                value = phone_gift_form.cleaned_data["amount"]
                if value > available(member):
                    raise ValidationError("invalid")
                recipient, binding = recipient_for_phone(phone)
                if recipient:
                    if recipient.pk == member.pk:
                        raise ValidationError("invalid")
                    payload = {
                        "user": request.user.pk,
                        "recipient": recipient.number,
                        "value": str(value),
                        "request": f"ui-{secrets.token_urlsafe(24)}",
                        "phone_binding": {
                            "id": binding.pk,
                            "version": str(binding.version),
                            "phone_hash": phone_hash(phone),
                        },
                    }
                    confirmation = {
                        **payload,
                        "name": masked_name(recipient),
                        "phone": "••••" + phone[-4:],
                        "token": signing.dumps(payload, salt=SALT),
                    }
                else:
                    fixed_language = recipient_language(phone)
                    payload = {
                        "user": request.user.pk,
                        "member": member.pk,
                        "phone": phone,
                        "language": fixed_language,
                        "key": "gift-invite:" + secrets.token_urlsafe(24),
                    }
                    gift_invitation = {
                        "phone": "••••" + phone[-4:],
                        "token": signing.dumps(payload, salt=GIFT_SALT),
                        "eligible": bool(member.invitation_token and invitation_eligible(member)),
                    }
                    invitation_form = GiftInvitationForm(
                        fixed_language=fixed_language,
                        language=(get_language() or "ar").split("-")[0],
                    )
            elif action == "gift_invitation_confirm" and member and member.status == "active":
                from .phone_gifts import queue_invitation

                payload = signing.loads(
                    request.POST.get("confirmation", ""), salt=GIFT_SALT, max_age=600
                )
                if payload["user"] != request.user.pk or payload["member"] != member.pk:
                    raise PermissionDenied
                invitation_form = GiftInvitationForm(
                    request.POST, fixed_language=payload["language"]
                )
                if not invitation_form.is_valid():
                    raise ValidationError("invalid")
                pointer = queue_invitation(
                    request.user,
                    phone=payload["phone"],
                    language=invitation_form.cleaned_data["language"],
                    key=payload["key"],
                    consent=invitation_form.cleaned_data["consent"],
                )
                request.session["loyalty_gift_invitation"] = pointer
                return redirect("accounts:rewards")
            elif action == "transfer_confirm" and member and member.status == "active":
                payload = signing.loads(
                    request.POST.get("confirmation", ""), salt=SALT, max_age=600
                )
                if payload["user"] != request.user.pk:
                    raise PermissionDenied
                from .phone_gifts import confirm_phone_transfer

                confirm_phone_transfer(request.user, payload)
            elif action == "invite" and member and member.status == "active":
                # Offer owned stays only; the service verifies arrival and completion.
                r = get_object_or_404(
                    Reservation,
                    pk=request.POST.get("reservation"),
                    booking_intent__customer=request.user,
                )
                issue_invite(request.user, r.pk)
            else:
                raise ValidationError("invalid")
            if confirmation is None and gift_invitation is None:
                messages.success(request, labels["success"])
                return redirect("accounts:rewards")
        except (
            ValidationError,
            signing.BadSignature,
            Http404,
            Member.DoesNotExist,
            ValueError,
            KeyError,
        ):
            error = labels["invalid"]
    member = (
        Member.objects.select_related("customer", "referral").filter(customer=request.user).first()
    )
    rows, cursor, metrics = [], None, {}
    completed = []
    invite_url = ""
    if member:
        with posting_transaction():
            # Financial writes use this lock. Historical personal balances are immutable.
            query = Ledger.objects.filter(member=member).select_related("original")
            sums = query.aggregate(c=Sum("credit"), d=Sum("debit"))
            posted = (sums["c"] or ZERO) - (sums["d"] or ZERO)
            usable = available(member)
            metrics = {"balance": posted, "available": usable, "held": posted - usable}
            rows, cursor = _rows(query, request)
        _decorate(rows)
        if member.status == "active":
            if member.invitation_token and invitation_eligible(member):
                invite_url = invitation_url(request, member)
            else:
                eligible_dates = Q(
                    created_at__gte=datetime.fromisoformat(settings.LOYALTY_LAUNCH_AT)
                )
                if pilot_only() and settings.LOYALTY_PILOT_ALLOW_HISTORICAL:
                    eligible_dates |= Q(public_reference__in=pilot_references())
                completed_dates = Q(check_out__lte=timezone.localdate())
                if pilot_only():
                    completed_dates |= Q(public_reference__in=stay_exception_references())
                completed = Reservation.objects.filter(
                    eligible_dates,
                    completed_dates,
                    booking_intent__customer=request.user,
                    source_type=Reservation.SourceType.DIRECT_WEBSITE,
                    normalized_status__in=Reservation.ACTIVE_STATUSES,
                ).select_related("property")
                if pilot_only():
                    completed = completed.filter(public_reference__in=pilot_references())
                completed = completed.order_by("-check_out")[:10]
                from .site_adapter import stay_times

                completed = [
                    r
                    for r in completed
                    if (r.loyalty_state.get("arrival") and stay_times(r)[1] <= timezone.now())
                    or stay_exception_allowed(r)
                ]
    a_copy = account_copy()
    from .phone_gifts import invitation_status

    gift_status = invitation_status(request.user, request.session.get("loyalty_gift_invitation"))
    gift_status_text = (
        labels.get("gift_status_" + gift_status["status"], labels["gift_status_uncertain"])
        if gift_status
        else ""
    )
    name = request.user.first_name.strip()
    mobile_available = phone_ready() and phone_allowed(request.user)
    return render(
        request,
        "accounts/dashboard.html",
        {
            "account_section": "rewards",
            "account_copy": a_copy,
            "account_greeting": a_copy["welcome_named"] % {"name": name}
            if name
            else a_copy["welcome"],
            "loyalty_ui": True,
            "loyalty_copy": labels,
            "loyalty_member": member,
            "loyalty_metrics": metrics,
            "loyalty_rows": rows,
            "loyalty_cursor": cursor,
            "loyalty_preview": preview_mode(),
            "loyalty_pilot": pilot_only(),
            "loyalty_stay_exception": pilot_only()
            and Reservation.objects.filter(
                booking_intent__customer=request.user,
                public_reference__in=stay_exception_references(),
            ).exists(),
            "loyalty_can_act": can_act and _verified(request.user),
            "loyalty_verify": not _verified(request.user),
            "loyalty_mobile_verify": mobile_available
            and not VerifiedPhone.objects.filter(user=request.user).exists(),
            "loyalty_mobile_copy": phone_copy(),
            "loyalty_terms_approved": approved_terms(),
            "loyalty_terms_reference": settings.LOYALTY_TERMS_REFERENCE,
            "loyalty_join_form": join_form,
            "loyalty_transfer_form": transfer_form,
            "loyalty_phone_gift_form": phone_gift_form,
            "loyalty_phone_help": phone_copy()["phone_help"],
            "loyalty_gift_invitation": gift_invitation,
            "loyalty_gift_invitation_form": invitation_form,
            "loyalty_gift_status": gift_status,
            "loyalty_gift_status_text": gift_status_text,
            "loyalty_inviter": inviter,
            "loyalty_invitation_invalid": invitation_invalid,
            "loyalty_confirmation": confirmation,
            "loyalty_error": error,
            "loyalty_completed": completed,
            "loyalty_invite_url": invite_url,
        },
    )


def _owner(request):
    require_operations_owner(request.user)
    if not (settings.LOYALTY_ENABLED and settings.LOYALTY_UI_ENABLED):
        raise Http404
    return copy()


@arabic_admin
@never_cache
@staff_member_required
def admin_workspace(request):
    labels = _owner(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    query = request.GET.get("q", "").strip()[:80]
    phone = request.GET.get("phone", "").strip()[:80]
    matches = []
    if phone:
        from apps.reservations.phone_search import bookings_for_phone

        matches = list(
            bookings_for_phone(
                Reservation.objects.filter(booking_intent__isnull=False).select_related(
                    "property", "booking_intent"
                ),
                phone,
            ).order_by("-check_in", "-created_at")[:30]
        )
    rows_query = Ledger.objects.select_related("original")
    if query:
        rows_query = rows_query.filter(
            Q(membership_number=query)
            | Q(operation_number=query)
            | Q(reservation_reference=query)
            | Q(operation_group=query)
        )
    rows, cursor = _rows(rows_query, request)
    _decorate(rows)
    totals = Ledger.objects.aggregate(c=Sum("credit"), d=Sum("debit"))
    return render(
        request,
        "loyalty/admin.html",
        {
            "title": labels["staff_title"],
            "loyalty_copy": labels,
            "loyalty_rows": rows,
            "loyalty_cursor": cursor,
            "loyalty_search": query,
            "booking_phone": phone,
            "booking_matches": matches,
            "loyalty_preview": preview_mode(),
            "program_balance": (totals["c"] or ZERO) - (totals["d"] or ZERO),
        },
    )


@arabic_admin
@never_cache
@staff_member_required
def admin_tree(request):
    labels = _owner(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    from .referrals import graph

    links_by_member = graph()
    focus = None
    query = request.GET.get("q", "").strip()[:80]
    if query:
        focus = next((r for r in links_by_member.values() if r.member.number == query), None)
        if focus is None:
            raise Http404
    else:
        parent = request.GET.get("parent")
        if parent:
            if not parent.isdigit() or len(parent) > 15:
                raise Http404
            focus = links_by_member.get(int(parent))
            if focus is None:
                raise Http404
    links = [
        r for r in links_by_member.values() if r.inviter_id == (focus.member_id if focus else None)
    ]
    try:
        after = max(0, int(request.GET.get("after", "0")))
    except ValueError:
        after = 0
    if after > 9223372036854775807:
        after = 0
    links = sorted((r for r in links if r.branch_order > after), key=lambda r: r.branch_order)[:21]
    for row in links:
        row.child_count = sum(r.inviter_id == row.member_id for r in links_by_member.values())
    cursor = links[19].branch_order if len(links) > 20 else None
    return render(
        request,
        "loyalty/tree.html",
        {
            "title": labels["tree"],
            "loyalty_copy": labels,
            "focus": focus,
            "tree_children": links[:20],
            "tree_cursor": cursor,
            "loyalty_preview": preview_mode(),
        },
    )


@arabic_admin
@never_cache
@staff_member_required
@require_POST
def admin_arrival(request):
    _owner(request)
    if not _actions_allowed(request):
        raise PermissionDenied
    try:
        r = get_object_or_404(
            Reservation,
            public_reference=request.POST.get("booking", ""),
            source_type=Reservation.SourceType.DIRECT_WEBSITE,
        )
        if preview_mode() and not r.is_test:
            raise PermissionDenied
        moment = datetime.fromisoformat(request.POST.get("arrived_at", ""))
        if timezone.is_naive(moment):
            moment = moment.replace(tzinfo=ZoneInfo("Asia/Riyadh"))
        confirm_arrival(
            request.user,
            r.pk,
            arrived_at=moment,
            reference=request.POST.get("reference", "").strip()[:255],
        )
        messages.success(request, copy()["arrival_saved"])
    except (ValueError, ValidationError, Http404):
        messages.error(request, copy()["invalid"])
    return redirect("loyalty:admin")
