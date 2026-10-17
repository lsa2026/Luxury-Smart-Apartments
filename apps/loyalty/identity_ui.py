"""Arabic owner-only identity maintenance. Never sends a code on the owner's behalf."""

import phonenumbers
from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts.identity import update_identity
from apps.accounts.models import ApprovedPhoneChange, CustomerProfile, VerifiedPhone
from apps.accounts.phone_views import PhoneForm
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number
from apps.notifications.models import AuditLog

from .localization import arabic_admin
from .models import Member
from .views import _owner

SALT = "owner-guest-identity-v1"


class IdentityForm(PhoneForm):
    email = forms.EmailField(
        label="البريد المرتبط بالحساب",
        max_length=254,
        widget=forms.EmailInput(attrs={"dir": "ltr"}),
    )
    reason = forms.CharField(
        label="سبب التعديل وكيف تحققت من هوية الضيف", min_length=5, max_length=500
    )
    verified = forms.BooleanField(label="تحققت من هوية صاحب الحساب وصحة طلب تعديل بياناته.")
    snapshot = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["phone"].required = False
        if not self.is_bound and self.initial.get("phone"):
            try:
                region = phonenumbers.region_code_for_number(
                    phonenumbers.parse(self.initial["phone"], None)
                )
                if region in phonenumbers.SUPPORTED_REGIONS:
                    self.initial["phone_country"] = region
            except phonenumbers.NumberParseException:
                pass

    def clean_phone(self):
        return super().clean_phone() if self.cleaned_data.get("phone") else ""


def snapshot(member):
    profile = CustomerProfile.objects.filter(user=member.customer).first()
    binding = VerifiedPhone.objects.filter(user=member.customer).first()
    pending = ApprovedPhoneChange.objects.filter(user=member.customer).first()
    phone = (
        pending.phone if pending else binding.phone if binding else profile.phone if profile else ""
    )
    return [
        member.customer.email,
        phone,
        str(binding.version) if binding else "",
        str(pending.version) if pending else "",
    ]


@arabic_admin
@never_cache
@staff_member_required
@require_http_methods(["GET", "POST"])
def admin_identity(request):
    _owner(request)
    query = request.GET.get("q", "").strip()[:100]
    members = Member.objects.select_related("customer")
    selected = None
    form = None
    error = ""
    if request.method == "POST" or request.GET.get("member"):
        member_id = request.POST.get("member") or request.GET.get("member", "")
        if not member_id.isdigit() or len(member_id) > 18:
            raise Http404
        selected = get_object_or_404(members, pk=member_id)
        values = snapshot(selected)
        form = IdentityForm(
            request.POST if request.method == "POST" else None,
            initial={
                "email": values[0],
                "phone": values[1],
                "snapshot": signing.dumps({"member": selected.pk, "value": values}, salt=SALT),
            },
        )
        if request.method == "POST" and form.is_valid():
            try:
                proof = signing.loads(form.cleaned_data["snapshot"], salt=SALT, max_age=600)
                if proof.get("member") != selected.pk:
                    raise ValidationError("مراجعة الحساب لا تطابق العضو المحدد.")
                update_identity(
                    request,
                    selected.customer_id,
                    email=form.cleaned_data["email"],
                    phone=form.cleaned_data["phone"],
                    reason=form.cleaned_data["reason"],
                    expected=proof["value"],
                )
                messages.success(
                    request,
                    "حُفظ تعديل الإدارة دون تغيير العضوية أو النقاط. إذا تغير الجوال، "
                    "يدخل الضيف برقمه الجديد ويؤكده برمز واتساب؛ وإذا تغير البريد "
                    "فيلزم توثيقه قبل استخدامه.",
                )
                return redirect(request.path + "?member=" + str(selected.pk))
            except (ValidationError, signing.BadSignature, KeyError, IntegrityError) as exc:
                error = (
                    " ".join(exc.messages)
                    if isinstance(exc, ValidationError)
                    else "انتهت المراجعة أو تغيرت؛ افتح الحساب مجددًا."
                )
    elif query:
        condition = Q(number=query) | Q(customer__email__iexact=query)
        try:
            phone = normalize_phone_number(query, default_region="SA")
            condition |= (
                Q(customer__verifiedphone__phone=phone)
                | Q(customer__customer_profile__phone=phone)
                | Q(customer__approvedphonechange__phone=phone)
            )
        except InvalidPhoneNumber:
            pass
        members = members.filter(condition).distinct()[:20]
    else:
        members = members.none()
    history = (
        AuditLog.objects.filter(
            action="guest.identity_changed", object_reference=str(selected.customer_id)
        )[:10]
        if selected
        else []
    )
    return render(
        request,
        "loyalty/identity.html",
        {
            "title": "إدارة جوال وبريد الأعضاء",
            "query": query,
            "members": members if not selected else [],
            "selected": selected,
            "form": form,
            "error": error,
            "identity_history": history,
            "pending_verification": bool(
                selected and ApprovedPhoneChange.objects.filter(user=selected.customer).exists()
            ),
        },
    )
