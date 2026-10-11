"""Owner-only, audited Google manager access; no invitations or guest deletion."""

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from apps.notifications.services.audit import record_audit

from .access import normalize_email, require_operations_owner
from .models import OperationsManager


class ManagerGrantForm(forms.Form):
    display_name = forms.CharField(label="اسم المدير (اختياري)", max_length=150, required=False)
    email = forms.EmailField(
        label="بريد Gmail",
        max_length=254,
        widget=forms.EmailInput(attrs={"dir": "ltr", "autocomplete": "off", "inputmode": "email"}),
    )
    confirm_access = forms.BooleanField(
        label="أوافق على منح هذا البريد إدارة الحجوزات والفواتير وبيانات الضيوف المرتبطة بها."
    )

    def clean_email(self):
        email = normalize_email(self.cleaned_data["email"])
        if email == settings.OPERATIONS_OWNER_EMAIL:
            raise forms.ValidationError("حساب المالك محفوظ ولا يمكن إضافته أو إلغاء وصوله من هنا.")
        if email.rsplit("@", 1)[-1] != "gmail.com":
            raise forms.ValidationError(
                "أدخل عنوان Gmail ينتهي بـ @gmail.com، كما يظهر في حساب Google."
            )
        return email


def _audit(request, manager, action):
    record_audit(
        request=request,
        action=f"operations_manager.{action}",
        object_type="operations_manager",
        object_reference=str(manager.pk),
        summary="غيّر المالك وصول مدير إلى الحجوزات والفواتير.",
        metadata={"status": action},
    )


@staff_member_required
@never_cache
@require_http_methods(["GET", "POST"])
def manager_list(request):
    require_operations_owner(request.user)
    form = ManagerGrantForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            manager, created = OperationsManager.objects.get_or_create(
                email=form.cleaned_data["email"],
                defaults={
                    "display_name": form.cleaned_data["display_name"],
                    "created_by": request.user,
                },
            )
            manager = OperationsManager.objects.select_for_update().get(pk=manager.pk)
            if created or not manager.is_enabled:
                if not created:
                    manager.is_enabled = True
                    manager.access_version += 1
                    manager.display_name = form.cleaned_data["display_name"]
                    manager.save(
                        update_fields=["is_enabled", "access_version", "display_name", "updated_at"]
                    )
                _audit(request, manager, "granted")
                messages.success(
                    request,
                    "أُضيف المدير. يمكنه اختيار حساب Gmail المحدد عند تسجيل الدخول. "
                    "لم يُرسل بريد دعوة.",
                )
            else:
                messages.info(request, "هذا البريد مضاف ومفعّل بالفعل؛ لم تتغير صلاحياته.")
        return redirect("accounts:operations_users")
    return render(
        request,
        "admin/accounts/operations_users.html",
        {
            "title": "إدارة المستخدمين",
            "form": form,
            "managers": OperationsManager.objects.all(),
            "owner_email": settings.OPERATIONS_OWNER_EMAIL,
        },
    )


@staff_member_required
@never_cache
@require_POST
def manager_revoke(request, manager_id):
    require_operations_owner(request.user)
    with transaction.atomic():
        manager = get_object_or_404(OperationsManager.objects.select_for_update(), pk=manager_id)
        if manager.email == settings.OPERATIONS_OWNER_EMAIL:
            messages.error(request, "لا يمكن إلغاء حساب المالك.")
        elif request.POST.get("confirm_revoke") != "on":
            messages.error(request, "أكد إلغاء وصول المدير أولًا.")
        elif manager.is_enabled:
            manager.is_enabled = False
            manager.access_version += 1
            manager.save(update_fields=["is_enabled", "access_version", "updated_at"])
            _audit(request, manager, "revoked")
            messages.success(
                request,
                "أُلغي الوصول إلى الإدارة من الطلب التالي، بما فيه الجلسات المفتوحة. "
                "لم تُحذف بيانات الحساب أو الحجوزات.",
            )
    return redirect("accounts:operations_users")
