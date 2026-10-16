"""Self-service, CSRF-protected recovery; browser supplies no matching identity."""

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .booking_history import ACTIVE, identity_for, start_import
from .history_copy import history_copy
from .models import BookingHistoryImport


def history_context(user):
    copy = history_copy()
    job = BookingHistoryImport.objects.filter(user=user).first()
    verified = bool(identity_for(user))
    status = job.status if job else "idle"
    if job and status in ACTIVE and job.updated_at < timezone.now() - timedelta(minutes=5):
        status = "partial"
    return {
        "history_copy": copy,
        "history_job": job,
        "history_verified": verified,
        "history_running": verified and status in ACTIVE,
        "history_message": copy.get(status, copy["partial"])
        if verified
        else copy["verification_required"],
    }


@never_cache
@login_required(login_url="accounts:login")
@require_POST
def restore_bookings(request):
    # No posted email, phone, account or Hostaway identifier is ever trusted.
    start_import(request.user)
    response = redirect("accounts:dashboard")
    response["Location"] += "#booking-history-recovery"
    return response


@never_cache
@login_required(login_url="accounts:login")
def restore_status(request):
    if request.method != "GET":
        from django.http import HttpResponseNotAllowed

        return HttpResponseNotAllowed(["GET"])
    context = history_context(request.user)
    job = context["history_job"]
    # No identifiers/contact details or arbitrary job lookup exposed.
    return JsonResponse(
        {
            "running": context["history_running"],
            "message": context["history_message"],
            "added": job.added if job else 0,
        }
    )
