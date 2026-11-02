# ruff: noqa: E501
"""Owner-approved copy for past guests only; amounts come from verified stay facts."""

from decimal import Decimal

from django.core.exceptions import ValidationError

from .enrollment_invitation import invitation_image  # noqa: F401

DEMO_REWARD = {"total": "1000.00", "base": "1000.00", "reward": "30.00"}


def invitation_caption(language, url, guest_name="", reward=None):
    if not reward or language not in {"ar", "en", "fr"}:
        raise ValidationError("Past-stay reward facts and supported language required.")
    from .enrollment_invitation import invitation_caption as clean_template

    # Reuse the established guest-name validation and neutral fallback.
    clean = clean_template(language, url, guest_name).split("\n\n")[1]
    name = clean.removesuffix(" في Luxury Living 🌿") + " 🌿" if language == "ar" else clean
    total, base, points = (format(Decimal(reward[k]), ",.2f") for k in ("total", "base", "reward"))
    if language == "ar":
        caption = (
            "🎁 *إقامتك معنا… بداية لمكافآت أجمل*\n\n" + name + "\n\n"
            "سعدنا باستضافتك في Luxury Living، ويسعدنا اليوم أن نشاركك إطلاق برنامج الولاء الجديد "
            "الذي صمّمناه تقديرًا لضيوفنا. ولأنك سبق أن أقمت معنا، حرصنا على أن تبدأ مكافآتك من إقامتك السابقة 💚\n\n"
            "*مكافأة إقامتك السابقة بانتظارك:*\n"
            f"🏡 قيمة حجزك: *{total} ر.س*\n"
            + (f"الأساس المؤهل: *{base} ر.س*\n" if total != base else "")
            + f"🎁 مكافأتك بمجرد الانضمام وتوثيق جوالك: *{points} نقطة، بقيمة {points} ر.س*، "
            "تعادل *٣٪ من قيمة حجزك المؤهل*.\n\n"
            "استفد من نقاطك في حجوزاتك القادمة عبر موقعنا، وفق شروط البرنامج.\n\n"
            "ومع انضمامك، تستمر مكافآتك:\n"
            "💚 *٣٪ عن إقاماتك القادمة المؤهلة.*\n"
            "🎁 *٥٪ عند إقامة ضيف جديد تدعوه عبر رابطك الشخصي، وفق شروط البرنامج.*\n\n"
            "*انضم مجانًا بخطوات بسيطة:*\n"
            "١. أنشئ حسابك أو سجّل الدخول.\n"
            "٢. أكمل الانضمام ووثّق جوالك، ثم استكشف ملفك ومكافآتك.\n\n"
            "ولديك «سما»، *مساعدتك الشخصية الذكية*، للإجابة عن جميع أسئلتك واستفساراتك حول برنامج الولاء.\n\n"
            "يسعدنا انضمامك، ونتطلع لاستضافتك مجددًا ✨\n\n"
            "*ابدأ رحلتك مع المكافآت:*\n" + url
        )
    elif language == "en":
        caption = (
            "🎁 *Your stay… the start of more rewards*\n\n" + name + "\n\n"
            "We enjoyed hosting you at Luxury Living. We created our new loyalty programme to thank our guests, "
            "and invite you to start with rewards from your previous eligible stay 💚\n\n"
            f"🏡 Your booking: *SAR {total}*\n"
            + (f"Eligible accommodation: *SAR {base}*\n" if total != base else "")
            + f"🎁 Join and verify your mobile to receive *{points} points, worth SAR {points}* "
            "— *3% of your eligible booking*. Use them on future website bookings under programme terms.\n\n"
            "💚 *3% on future eligible stays.*\n"
            "🎁 *5% when a new guest you invite through your personal link has an eligible stay, under programme terms.*\n\n"
            "*Join for free:*\n1. Create an account or sign in.\n2. Complete enrolment, verify your mobile and explore your rewards.\n\n"
            "Sama, your *intelligent personal assistant*, can answer your questions about the programme.\n\n"
            "We look forward to welcoming you back ✨\n\n*Start your rewards journey:*\n" + url
        )
    else:
        caption = (
            "🎁 *Votre séjour… le début de nouvelles récompenses*\n\n" + name + "\n\n"
            "Heureux de vous avoir accueilli chez Luxury Living, nous lançons un programme de fidélité "
            "pour remercier nos clients. Vos récompenses commencent avec votre séjour passé éligible 💚\n\n"
            f"🏡 Votre réservation : *{total} SAR*\n"
            + (f"Hébergement éligible : *{base} SAR*\n" if total != base else "")
            + f"🎁 Adhérez et vérifiez votre mobile pour recevoir *{points} points, soit {points} SAR* "
            "— *3 % du montant éligible*, utilisables sur notre site selon les conditions du programme.\n\n"
            "💚 *3 % sur vos futurs séjours éligibles.*\n"
            "🎁 *5 % lorsqu’un nouveau client invité via votre lien effectue un séjour éligible, selon les conditions.*\n\n"
            "*Adhérez gratuitement :*\n1. Créez votre compte ou connectez-vous.\n2. Finalisez l’adhésion et vérifiez votre mobile.\n\n"
            "Sama, votre *assistante personnelle intelligente*, répond à vos questions.\n\n"
            "Au plaisir de vous accueillir à nouveau ✨\n\n*Découvrez vos récompenses :*\n" + url
        )
    if len(caption.encode("utf-16-le")) // 2 > 1024:
        raise ValidationError("Invitation caption exceeds the provider limit; no truncation.")
    return caption
