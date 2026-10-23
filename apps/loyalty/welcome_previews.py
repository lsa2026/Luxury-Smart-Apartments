"""One synthetic welcome to the pinned, verified owner; no recipient override."""

from base64 import b64encode
from uuid import uuid4

from django.contrib import admin, messages
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.access import operations_access_required, require_operations_owner

from .message_previews import owner_preview_binding, send_catalogue
from .models import GuestEvent, Member
from .phone_gifts import delivery_ready
from .welcome import image
from .whatsapp_notices import lifecycle_caption

SALT = "owner-loyalty-welcome-preview-v1"


@operations_access_required
@require_http_methods(["GET", "POST"])
def preview(request):
    require_operations_owner(request.user)
    language = request.GET.get("language", "ar")
    if language not in {"ar", "en", "fr"}:
        language = "ar"
    reason, masked_phone = "", ""
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
            result = send_catalogue(batch=proof["batch"], language=language, kind="welcome")
        except (signing.BadSignature, ValidationError, KeyError, ObjectDoesNotExist):
            messages.error(request, "تعذر إرسال النموذج؛ حدّث الصفحة وتحقق من رقم المالك المعتمد.")
        else:
            messages.info(
                request,
                {
                    "accepted": (
                        "قبل مزود واتساب نموذج الترحيب إلى رقمك المعتمد؛ "
                        "الوصول والقراءة غير مؤكدين."
                    ),
                    "already_attempted": "سُجل طلب هذا النموذج مسبقًا؛ لم يُرسل مرة أخرى.",
                    "uncertain": "نتيجة إرسال النموذج غير مؤكدة؛ لم تُعد المحاولة تلقائيًا.",
                }[result[0][1]],
            )
        return redirect(request.path + "?language=" + language)
    event = GuestEvent(kind=GuestEvent.Kind.JOIN, occurred_at=timezone.now())
    event.member = Member(number="PREVIEW-MEMBER")
    return render(
        request,
        "admin/loyalty/welcome_preview.html",
        {
            **admin.site.each_context(request),
            "title": "نموذج ترحيب برنامج الولاء",
            "language_code": language,
            "preview_text": lifecycle_caption(event, language),
            "preview_image": "data:image/png;base64," + b64encode(image(language)).decode("ascii"),
            "masked_phone": masked_phone,
            "reason": reason,
            "confirmation": signing.dumps(
                {"operator": request.user.pk, "language": language, "batch": str(uuid4())},
                salt=SALT,
            ),
        },
    )
