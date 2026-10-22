"""Owner-only rendering and one explicitly requested sample to the pinned owner phone."""

from base64 import b64encode
from uuid import uuid4

from django.conf import settings
from django.contrib import admin, messages
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.access import operations_access_required, require_operations_owner

from .booking_invitations import invitation_caption
from .invitation_design import invitation_image
from .message_previews import owner_preview_binding, send_catalogue
from .phone_gifts import delivery_ready

SALT = "owner-booking-invitation-preview-v1"


@operations_access_required
@require_http_methods(["GET", "POST"])
def preview(request):
    require_operations_owner(request.user)
    language = request.GET.get("language", "ar")
    if language not in {"ar", "en", "fr"}:
        language = "ar"
    reason = ""
    masked_phone = ""
    try:
        if not delivery_ready():
            raise ValidationError("Provider unavailable.")
        _, binding = owner_preview_binding()
        masked_phone = "••••" + binding.phone[-4:]
    except (ObjectDoesNotExist, ValidationError):
        reason = "تعذر التحقق من رقم واتساب المالك المعتمد أو جاهزية الإرسال. لم تُرسل رسالة."
    if request.method == "POST":
        try:
            proof = signing.loads(request.POST.get("confirmation", ""), salt=SALT, max_age=600)
            if proof.get("operator") != request.user.pk or proof.get("language") != language:
                raise ValidationError("Preview does not match the owner or language.")
            if reason:
                raise ValidationError(reason)
            result = send_catalogue(
                batch=proof["batch"], language=language, kind="booking-invitation"
            )
        except (signing.BadSignature, ValidationError, KeyError, ObjectDoesNotExist):
            messages.error(request, "تعذر إرسال النموذج؛ حدّث الصفحة وتحقق من رقم المالك المعتمد.")
        else:
            status = result[0][1]
            messages.info(
                request,
                {
                    "accepted": (
                        "قبل مزود واتساب نموذج الدعوة إلى رقمك المعتمد؛ الوصول والقراءة غير مؤكدين."
                    ),
                    "already_attempted": "سُجل طلب هذا النموذج مسبقًا؛ لم يُرسل مرة أخرى.",
                    "uncertain": "نتيجة إرسال النموذج غير مؤكدة؛ لم تُعد المحاولة تلقائيًا.",
                }[status],
            )
        return redirect(request.path + "?language=" + language)
    return render(
        request,
        "admin/loyalty/booking_invitation_preview.html",
        {
            **admin.site.each_context(request),
            "title": "نموذج دعوة برنامج الولاء",
            "language_code": language,
            "preview_text": invitation_caption(
                language, settings.SITE_BASE_URL.rstrip("/") + "/my-bookings/rewards/"
            ),
            "preview_image": "data:image/png;base64,"
            + b64encode(invitation_image(language)).decode("ascii"),
            "masked_phone": masked_phone,
            "reason": reason,
            "confirmation": signing.dumps(
                {"operator": request.user.pk, "language": language, "batch": str(uuid4())},
                salt=SALT,
            ),
        },
    )
