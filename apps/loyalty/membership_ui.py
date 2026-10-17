"""Owner-only lifecycle audit and explicitly confirmed membership closure."""

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import strip_tags
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts.access import require_operations_owner
from apps.core.phone_numbers import InvalidPhoneNumber, normalize_phone_number

from .localization import arabic_admin
from .models import GuestEvent, Member
from .services import balance, held_points, posting_transaction, withdraw
from .views import _owner

SALT = "owner-membership-closure-v1"
FIELD_LABELS = {
    "email": "البريد الإلكتروني",
    "phone": "رقم الجوال",
    "status": "حالة العضوية",
    "membership_number": "رقم العضوية",
    "tree_number": "الرقم الشجري",
    "inviter": "الداعي",
    "balance": "رصيد النقاط",
    "first_name": "الاسم الأول",
    "last_name": "اسم العائلة",
    "preferred_language": "اللغة المفضلة",
    "residence_address_line1": "العنوان",
    "residence_city": "المدينة",
    "residence_region": "المنطقة",
    "residence_postal_code": "الرمز البريدي",
    "residence_country": "الدولة",
}


class ClosureForm(forms.Form):
    reason = forms.CharField(
        label="سبب الإلغاء ومرجع طلب الضيف",
        min_length=5,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    identity_confirmed = forms.BooleanField(label="تحققت من هوية الضيف وطلبه إلغاء العضوية.")
    forfeiture_confirmed = forms.BooleanField(
        label="أؤكد إلغاء عضوية الولاء والتنازل عن رصيدها الموجب، دون حذف حساب الموقع أو الحجوزات."
    )
    snapshot = forms.CharField(widget=forms.HiddenInput)


def state(member):
    member.refresh_from_db()
    return [
        member.status,
        str(balance(member)),
        str(held_points(member)),
        member.updated_at.isoformat(),
    ]


def close_membership(operator, member_id, *, reason, expected, reference):
    require_operations_owner(operator)
    reason = strip_tags(reason).strip()
    if not 5 <= len(reason) <= 500:
        raise ValidationError("اكتب سبب الإلغاء ومرجع طلب الضيف.")
    with posting_transaction():
        member = Member.objects.select_related("customer").get(pk=member_id)
        if member.status != "active":
            raise ValidationError("هذه العضوية ملغاة بالفعل؛ لم تُنشأ عملية جديدة.")
        if state(member) != expected:
            raise ValidationError("تغير الحساب منذ المراجعة؛ افتحه مجددًا قبل التأكيد.")
        return withdraw(member.customer, reference, operator=operator, reason=reason)


def _display(values):
    return [
        (
            FIELD_LABELS.get(key, "تفصيل محفوظ"),
            {
                "active": "نشطة",
                "withdrawn": "ملغاة",
                "ar": "العربية",
                "en": "الإنجليزية",
                "fr": "الفرنسية",
            }.get(str(value), value),
        )
        for key, value in values.items()
        if key in FIELD_LABELS
    ]


@arabic_admin
@never_cache
@staff_member_required
@require_http_methods(["GET", "POST"])
def admin_memberships(request):
    _owner(request)
    query = request.GET.get("q", "").strip()[:254]
    kind = request.GET.get("kind", "")
    if kind not in GuestEvent.Kind.values:
        kind = ""
    selected = member = form = None
    error = ""
    users = get_user_model().objects.filter(is_staff=False, is_superuser=False)
    selected_id = (
        request.POST.get("customer") if request.method == "POST" else request.GET.get("customer")
    )
    if selected_id:
        if not selected_id.isdigit() or len(selected_id) > 18:
            raise Http404
        selected = get_object_or_404(users, pk=selected_id)
        member = Member.objects.filter(customer=selected).first()
        if member and member.status == "active":
            form = ClosureForm(
                request.POST if request.method == "POST" else None,
                initial={
                    "snapshot": signing.dumps(
                        {"member": member.pk, "state": state(member)}, salt=SALT
                    )
                },
            )
            if request.method == "POST" and form.is_valid():
                try:
                    proof = signing.loads(form.cleaned_data["snapshot"], salt=SALT, max_age=600)
                    if proof["member"] != member.pk:
                        raise ValidationError("التأكيد لا يطابق العضوية المحددة.")
                    close_membership(
                        request.user,
                        member.pk,
                        reason=form.cleaned_data["reason"],
                        expected=proof["state"],
                        reference=f"admin-close:{member.pk}",
                    )
                    messages.success(
                        request, "أُلغيت عضوية الولاء وسُجل الحدث. بقي حساب الموقع والحجوزات محفوظين."
                    )
                    return redirect(request.path + "?customer=" + str(selected.pk))
                except (ValidationError, signing.BadSignature, KeyError, TypeError) as exc:
                    error = (
                        " ".join(exc.messages)
                        if isinstance(exc, ValidationError)
                        else "انتهت صلاحية المراجعة؛ افتح الحساب مجددًا."
                    )
        elif request.method == "POST":
            error = "لا توجد عضوية نشطة لإلغائها؛ لم يتغير الحساب."
    elif request.method == "POST":
        raise Http404
    events = GuestEvent.objects.select_related("customer", "member", "actor", "ledger")
    matches = []
    if selected:
        events = events.filter(customer=selected)
    elif query:
        condition = Q(email__iexact=query) | Q(member__number=query)
        historical = Q(before__email__iexact=query) | Q(after__email__iexact=query)
        try:
            phone = normalize_phone_number(query, default_region="SA")
            condition |= (
                Q(verifiedphone__phone=phone)
                | Q(customer_profile__phone=phone)
                | Q(approvedphonechange__phone=phone)
            )
            historical |= Q(before__phone=phone) | Q(after__phone=phone)
        except InvalidPhoneNumber:
            pass
        historical_users = GuestEvent.objects.filter(historical).values("customer_id")
        users = users.filter(condition | Q(pk__in=historical_users)).distinct()
        matches = list(users[:30])
        events = events.filter(customer__in=users)
    if kind:
        events = events.filter(kind=kind)
    page = Paginator(events, 25).get_page(request.GET.get("page"))
    for event in page:
        event.before_display, event.after_display = _display(event.before), _display(event.after)
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "loyalty/memberships.html",
        {
            "title": "أحداث الضيوف وإلغاء عضوية الولاء",
            "query": query,
            "kind": kind,
            "event_kinds": GuestEvent.Kind.choices,
            "matches": matches,
            "selected": selected,
            "member": member,
            "closure_form": form,
            "error": error,
            "event_page": page,
            "page_query": params.urlencode(),
            "member_balance": balance(member) if member else None,
            "member_held": held_points(member) if member else None,
        },
    )
