"""First-party invitation aliases; no new token, membership, or tracking writes."""

from django.conf import settings
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .launch import participant_allowed
from .models import Member
from .services import invitation_eligible

SHORT_LENGTH = 16  # 96 random bits from the existing URL-safe invitation token.


def invitation_url(request, member):
    token = member.invitation_token
    code = token[:SHORT_LENGTH]
    # Never attribute an ambiguous alias. Existing full-token links still work.
    if Member.objects.filter(invitation_token__startswith=code).exclude(pk=member.pk).exists():
        code = token
    return request.build_absolute_uri(reverse("loyalty_invite_short", kwargs={"code": code}))


@never_cache
@require_safe
def invitation_shortlink(request, code):
    if not (settings.LOYALTY_ENABLED and settings.LOYALTY_UI_ENABLED) or not 16 <= len(code) <= 100:
        raise Http404
    matches = Member.objects.select_related("customer", "referral")
    if len(code) == SHORT_LENGTH:
        matches = matches.filter(invitation_token__startswith=code)
    else:
        matches = matches.filter(invitation_token=code)
    matches = list(matches[:2])
    if len(matches) != 1:
        raise Http404
    member = matches[0]
    if (
        member.status != "active"
        or not member.customer.is_active
        or not participant_allowed(member.customer)
        or not invitation_eligible(member)
    ):
        raise Http404
    target = reverse("accounts:rewards") + "?" + urlencode({"invite": member.invitation_token})
    response = HttpResponseRedirect(target)
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response
