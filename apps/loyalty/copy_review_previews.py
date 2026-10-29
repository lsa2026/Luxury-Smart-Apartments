"""Owner-only copy review; synthetic names and no live guest-template changes."""
# Full owner-reviewed message paragraphs are deliberately kept intact.
# ruff: noqa: E501

import json
from base64 import b64decode, b64encode
from hashlib import sha256
from importlib.resources import files
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.contrib import admin, messages
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.accounts.access import operations_access_required, require_operations_owner

from .invitation_design import invitation_image
from .message_previews import owner_preview_binding, send_catalogue
from .phone_gifts import delivery_ready
from .welcome import image as welcome_image

VERSION = "loyalty-copy-review-20261029-v2"
SALT = "owner-loyalty-copy-review-v2"
INVITATION_REVIEW = "invitation-trilingual-v3"

WELCOME = "*مرحبًا بك في برنامج ولاء Luxury Living 🌿*\n\nأهلًا خالد العتيبي،\n\nسعداء بانضمامك إلينا، ونتطلع إلى أن تكون إقاماتك معنا مليئة بالتجارب الجميلة والمكافآت 💚\n\nاكتشف ملفك الشخصي، وتابع نقاطك ومكافآتك، وأكمل بياناتك إن لم تكن مكتملة.\n\nولأي سؤال أو مساعدة، «سما»، مساعدتك الشخصية الذكية، يسعدها خدمتك والإجابة عن استفساراتك.\n\nرقم عضويتك: LSA-DEMO-001\n\n✨ استكشف ملفك ومكافآتك:\n"
INVITATION = "🎁 *دعوة خاصة إلى مكافآت ضيوفنا*\n\nأهلًا نورة الحربي في Luxury Living 🌿\n\nإقامتك معنا بداية لتجارب أجمل! يسعدنا دعوتك إلى برنامج الولاء لتستمتع بمكافآت إقامتك الحالية وإقاماتك القادمة، وتكسب أيضًا عند دعوة أحبائك:\n\n💚 مكافآت بنسبة ٣٪ عن إقاماتك المؤهلة.\n🎁 مكافأة بنسبة ٥٪ عند إقامة ضيف جديد تدعوه عبر رابطك الشخصي، وفق شروط البرنامج.\n\nانضم مجانًا بخطوات بسيطة:\n١. أنشئ حسابك أو سجّل الدخول.\n٢. استكشف البرنامج وأكمل الانضمام ووثّق جوالك.\n\nولديك «سما»، مساعدتك الشخصية الذكية، للإجابة عن جميع أسئلتك واستفساراتك حول برنامج الولاء.\n\nنسعد بإقامتك، وبمن تدعوهم لمشاركتك التجربة ✨\n\nابدأ رحلتك مع المكافآت:\n"


def packets(language="ar"):
    if language != "ar":
        raise ValidationError("This owner-reviewed copy is Arabic only.")
    base = settings.SITE_BASE_URL.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValidationError("Invalid preview destination.")
    url = base + "/my-bookings/rewards/"
    result = [
        ("review-welcome-v2", WELCOME + url, welcome_image(language)),
        ("review-invitation-v2", INVITATION + url, invitation_image(language)),
    ]
    if any(len(caption.encode("utf-16-le")) // 2 > 1024 for _, caption, _ in result):
        raise ValidationError("Preview caption too long.")
    return result


def invitation_packets(language="all"):
    """Fixed owner-only translations and Figma exports; no arbitrary upload or recipient."""
    if language not in {"all", "ar", "en", "fr"}:
        raise ValidationError("Unsupported invitation review language.")
    data = json.loads(
        files("apps.loyalty")
        .joinpath("invitation_assets", "enrollment-owner-review-v3.json")
        .read_text(encoding="utf-8")
    )
    base = settings.SITE_BASE_URL.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValidationError("Invalid preview destination.")
    result = []
    if [item["language"] for item in data["samples"]] != ["ar", "en", "fr"]:
        raise ValidationError("Invalid review language set.")
    for item in data["samples"]:
        if language != "all" and item["language"] != language:
            continue
        content = b64decode(item["image_base64"], validate=True)
        if sha256(content).hexdigest() != item["image_sha256"]:
            raise ValidationError("Review artwork does not match.")
        caption = item["caption"] + base + "/my-bookings/rewards/"
        if len(caption.encode("utf-16-le")) // 2 > 1024:
            raise ValidationError("Preview caption too long.")
        result.append(("review-invitation-v3-" + item["language"], caption, content))
    return result


@operations_access_required
@require_http_methods(["GET", "POST"])
def preview(request):
    require_operations_owner(request.user)
    mode = request.GET.get("sample", "copy-review-v2")
    if mode not in {"copy-review-v2", INVITATION_REVIEW}:
        raise ValidationError("Unsupported preview sample.")
    trilingual = mode == INVITATION_REVIEW
    sample_packets = invitation_packets() if trilingual else packets()
    expected_count = len(sample_packets)
    reason, masked_phone = "", ""
    try:
        if not delivery_ready():
            raise ValidationError("Provider unavailable.")
        _, binding = owner_preview_binding()
        masked_phone = "••••" + binding.phone[-4:]
    except (ObjectDoesNotExist, ValidationError):
        reason = "تعذر التحقق من رقم المالك المعتمد أو جاهزية الإرسال."
    if request.method == "POST":
        try:
            proof = signing.loads(request.POST.get("confirmation", ""), salt=SALT, max_age=600)
            if proof.get("operator") != request.user.pk or proof.get("version") != VERSION:
                raise ValidationError("Preview does not match the owner or version.")
            if proof.get("sample", "copy-review-v2") != mode:
                raise ValidationError("Preview mode does not match.")
            if reason:
                raise ValidationError(reason)
            result = send_catalogue(
                batch=proof["batch"], language="all" if trilingual else "ar", kind=mode
            )
        except (signing.BadSignature, ValidationError, KeyError, ObjectDoesNotExist):
            messages.error(request, "تعذر إرسال النموذج؛ حدّث الصفحة وتحقق من رقم المالك.")
        else:
            accepted = sum(state == "accepted" for _, state in result)
            if any(state == "uncertain" for _, state in result):
                messages.warning(
                    request,
                    f"قبل المزود {accepted} من {expected_count} نماذج؛ نتيجة رسالة غير مؤكدة، ولا إعادة تلقائية.",
                )
            elif accepted == expected_count:
                messages.success(
                    request,
                    f"قبل مزود واتساب {accepted} نماذج إلى رقم المالك؛ الوصول والقراءة غير مؤكدين.",
                )
            else:
                messages.info(request, "سُجل طلب المعاينة مسبقًا؛ لم تُكرر الرسائل.")
        return redirect(request.get_full_path())
    return render(
        request,
        "admin/loyalty/copy_review_preview.html",
        {
            **admin.site.each_context(request),
            "title": "معاينة الصياغة النهائية لرسائل الولاء",
            "sample_note": (
                "ثلاث دعوات للمعاينة بالعربية والإنجليزية والفرنسية بأسماء تجريبية، وبطاقات فيقما المصححة."
                if trilingual
                else "معاينة للمالك فقط بأسماء تجريبية: خالد العتيبي ونورة الحربي. رقم العضوية تجريبي."
            ),
            "send_label": "إرسال الدعوات الثلاث إلى واتسابي"
            if trilingual
            else "إرسال النموذجين إلى واتسابي",
            "samples": [
                {
                    "key": key,
                    "caption": caption,
                    "image": "data:image/png;base64," + b64encode(content).decode("ascii"),
                }
                for key, caption, content in sample_packets
            ],
            "masked_phone": masked_phone,
            "reason": reason,
            "confirmation": signing.dumps(
                {
                    "operator": request.user.pk,
                    "version": VERSION,
                    "batch": str(uuid4()),
                    "sample": mode,
                },
                salt=SALT,
            ),
        },
    )
