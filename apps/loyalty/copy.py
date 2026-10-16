"""Three-language copy for the private rewards workspace."""

# ruff: noqa: E501
from django.utils.translation import get_language

# Each row supplies Arabic, English and French; no UI translation fallback.
LABELS = {
    "title": ("مكافآتي", "My rewards", "Mes avantages"),
    "preview": (
        "تجربة داخلية — النقاط والعضويات هنا افتراضية، والبرنامج غير منشور.",
        "Internal preview — fictional points and memberships; not launched.",
        "Aperçu interne — points et adhésions fictifs ; programme non lancé.",
    ),
    "pilot_notice": (
        "تجربة خاصة محدودة — النقاط المسجلة هنا فعلية. البرنامج لم يُفتح لجميع الضيوف بعد.",
        "Private limited trial — recorded points are real. The programme is not open to all guests yet.",
        "Essai privé limité — les points enregistrés sont réels. Le programme n’est pas encore ouvert à tous les clients.",
    ),
    "legal_wait": (
        "الانضمام والتحويل ينتظران اعتماد الأحكام وإطلاق البرنامج.",
        "Joining and transfers await approved terms and launch.",
        "L’adhésion et les transferts attendent la validation des conditions et le lancement.",
    ),
    "join_heading": (
        "إقامتك بداية مكافآتك",
        "Your stay is the start of your rewards",
        "Votre séjour marque le début de vos avantages",
    ),
    "join_intro": (
        "عضوية مجانية واختيارية؛ تسجيل الدخول لا يعني الانضمام. تضاف مكافأتك بعد توثيق الوصول واستلام قيمة الإقامة.",
        "Membership is free and optional. Signing in does not enrol you. Rewards follow verified arrival and payment for your stay.",
        "L’adhésion est gratuite et facultative. Se connecter ne vous inscrit pas. Les points suivent l’arrivée et le paiement vérifiés.",
    ),
    "draft": (
        "ملخص تجريبي للقواعد، وليس شروطًا قانونية منشورة",
        "Test rules summary, not published legal terms",
        "Résumé des règles de test, non des conditions juridiques publiées",
    ),
    "rules": (
        "مكافأة الإقامة ثلاثة بالمئة من قيمة الإقامة المدفوعة بغير النقاط. للداعي المباشر خمسة بالمئة، وللداعين الأسبق حصة مجتمعة لا تتجاوز سبعة بالمئة وبحد واحد بالمئة للشخص. نقطة واحدة تعادل ريالًا عند الاستخدام؛ والنقاط لا تنتهي صلاحيتها أثناء العضوية. الدعوات متاحة بعد أول إقامة مكتملة.",
        "Stay rewards are 3% of lodging paid without points. A direct referrer earns 5%; earlier referrers share up to 7%, capped at 1% each. One point is worth one riyal when redeemed; points do not expire during membership. Invitations become available after your first completed stay.",
        "Le séjour rapporte 3 % de l’hébergement payé sans points. Le parrain direct reçoit 5 % ; les parrains précédents se partagent au plus 7 %, avec un plafond de 1 % chacun. Un point vaut un riyal lors de son utilisation ; les points n’expirent pas pendant l’adhésion. Les invitations sont disponibles après le premier séjour terminé.",
    ),
    "terms": ("قراءة الأحكام المعتمدة", "Read the approved terms", "Lire les conditions validées"),
    "consent": (
        "أوافق على الانضمام الاختياري وفق النسخة المعروضة.",
        "I choose to join under the displayed version.",
        "Je choisis d’adhérer selon la version affichée.",
    ),
    "inviter": (
        "أؤكد أن صاحب هذه العضوية هو من دعاني، وستبقى العلاقة ثابتة.",
        "I confirm this member invited me; this relationship will remain fixed.",
        "Je confirme que ce membre m’a invité ; cette relation restera fixe.",
    ),
    "invited_by": ("دعوة من", "Invitation from", "Invitation de"),
    "join": (
        "الانضمام إلى برنامج الولاء",
        "Join the rewards programme",
        "Rejoindre le programme de fidélité",
    ),
    "number": ("رقم العضوية", "Membership number", "Numéro d’adhésion"),
    "tree_number": ("الرقم الشجري", "Referral path", "Chemin de parrainage"),
    "joined": ("تاريخ الانضمام", "Joined", "Date d’adhésion"),
    "balance": ("رصيد النقاط", "Points balance", "Solde de points"),
    "available": ("المتاح للاستخدام", "Available to use", "Disponible"),
    "held": ("محجوز لحجز جارٍ", "Reserved for a booking", "Réservé pour une réservation"),
    "points": ("نقطة", "points", "points"),
    "transfer": ("إهداء نقاط", "Gift points", "Offrir des points"),
    "recipient": (
        "رقم عضوية المستلم",
        "Recipient membership number",
        "Numéro d’adhésion du destinataire",
    ),
    "amount": ("عدد النقاط", "Points amount", "Nombre de points"),
    "review_transfer": ("مراجعة التحويل", "Review transfer", "Vérifier le transfert"),
    "confirm_transfer": ("تأكيد التحويل", "Confirm transfer", "Confirmer le transfert"),
    "transfer_help": (
        "تصل النقاط إلى الحساب الآخر فور اكتمال العملية، دون الحاجة إلى اتصال المستلم.",
        "Points reach the other account when the transfer completes. The recipient need not be online.",
        "Les points arrivent sur le compte dès le transfert terminé. Le destinataire n’a pas besoin d’être connecté.",
    ),
    "confirmation": (
        "راجع رقم المستلم والقيمة قبل التأكيد. لن تتغير علاقة الدعوة بسبب هذا التحويل.",
        "Check the recipient number and amount before confirming. This gift does not change referral relationships.",
        "Vérifiez le numéro et le montant avant de confirmer. Le don ne change pas les liens de parrainage.",
    ),
    "cancel": ("العودة دون تحويل", "Back without transferring", "Retour sans transfert"),
    "success": (
        "اكتملت العملية بنجاح.",
        "The operation completed successfully.",
        "L’opération a été effectuée.",
    ),
    "invalid": (
        "تعذر تنفيذ الطلب؛ راجع البيانات والرصيد وأهلية الحساب.",
        "Unable to process this request. Check the details, balance and account eligibility.",
        "Impossible de traiter la demande. Vérifiez les données, le solde et l’éligibilité du compte.",
    ),
    "verify": (
        "يرجى تأكيد بريد حسابك قبل الانضمام أو التحويل.",
        "Verify your account email before joining or transferring.",
        "Vérifiez votre adresse e-mail avant d’adhérer ou de transférer.",
    ),
    "statement": ("كشف عملياتي", "My points statement", "Mon relevé de points"),
    "empty": (
        "لا توجد عمليات نقاط بعد.",
        "No points transactions yet.",
        "Aucune opération de points pour le moment.",
    ),
    "sequence": ("التسلسل", "Sequence", "Séquence"),
    "operation": ("رقم العملية", "Transaction number", "Numéro d’opération"),
    "date": ("التاريخ والوقت", "Date and time", "Date et heure"),
    "kind": ("نوع العملية", "Transaction type", "Type d’opération"),
    "credit": ("دائن", "Credit", "Crédit"),
    "debit": ("مدين", "Debit", "Débit"),
    "account_balance": (
        "رصيدي بعد العملية",
        "My balance after this transaction",
        "Mon solde après l’opération",
    ),
    "program_balance": (
        "رصيد البرنامج بعد العملية",
        "Programme balance after transaction",
        "Solde du programme après l’opération",
    ),
    "details": ("تفاصيل العملية", "Transaction details", "Détails de l’opération"),
    "booking": ("رقم الحجز", "Booking number", "Numéro de réservation"),
    "property": ("العقار", "Property", "Hébergement"),
    "gross": ("قيمة الحجز", "Booking value", "Montant de la réservation"),
    "base": ("الأساس المؤهل", "Eligible lodging amount", "Montant d’hébergement éligible"),
    "rate": ("النسبة المحفوظة", "Recorded rate", "Taux enregistré"),
    "original": ("العملية الأصلية", "Original transaction", "Opération d’origine"),
    "group": ("مجموعة العملية", "Transaction group", "Groupe d’opérations"),
    "version": ("نسخة القواعد", "Rules version", "Version des règles"),
    "next": ("عمليات أقدم", "Older transactions", "Opérations précédentes"),
    "first": ("أحدث العمليات", "Latest transactions", "Dernières opérations"),
    "invite": ("ادعُ صديقًا", "Invite a friend", "Inviter un ami"),
    "invite_wait": (
        "يُتاح رابط دعوتك بعد إتمام أول إقامة مؤهلة. مكافأة الإقامة نفسها لا تنتظر انتهاء الإقامة.",
        "Your invitation link becomes available after your first eligible completed stay. Stay rewards do not wait until departure.",
        "Votre lien est disponible après votre premier séjour éligible terminé. Les points du séjour n’attendent pas le départ.",
    ),
    "issue_invite": (
        "إظهار رابط دعوتي",
        "Get my invitation link",
        "Afficher mon lien d’invitation",
    ),
    "invite_link": ("رابط دعوتك", "Your invitation link", "Votre lien d’invitation"),
    "redemption_help": (
        "استخدم نقاطك من صفحة مراجعة الحجز المؤهل؛ يظهر عدد النقاط والمبلغ المتبقي قبل التأكيد. هذه الصفحة لعرض حسابك وتحويل النقاط، وليست لتحصيل مبلغ أو تنفيذ استرداد.",
        "Apply points on an eligible booking’s review page, where the points and remaining payment are shown before confirmation. This page displays your account and transfers; it does not collect payments or process refunds.",
        "Utilisez vos points sur la page de vérification d’une réservation éligible : les points et le montant restant s’affichent avant confirmation. Cette page présente votre compte et les transferts ; elle n’encaisse aucun paiement et n’exécute aucun remboursement.",
    ),
    "withdrawn": (
        "هذه العضوية ملغاة؛ يظهر السجل السابق للمراجعة فقط.",
        "This membership is closed; its history remains available for review.",
        "Cette adhésion est clôturée ; son historique reste consultable.",
    ),
    "staff_title": ("إدارة برنامج الولاء", "Rewards administration", "Gestion de la fidélité"),
    "bookings_admin": ("إدارة الحجوزات", "Booking management", "Gestion des réservations"),
    "program_total": (
        "إجمالي رصيد البرنامج",
        "Total programme balance",
        "Solde total du programme",
    ),
    "member_after": (
        "رصيد العضو بعد العملية",
        "Member balance after transaction",
        "Solde du membre après l’opération",
    ),
    "ledger": ("سجل العمليات العام", "Programme ledger", "Registre des opérations"),
    "tree": (
        "شجرة الأعضاء — للإدارة فقط",
        "Member tree — administration only",
        "Arbre des membres — administration uniquement",
    ),
    "search": (
        "بحث برقم العضوية أو العملية أو الحجز",
        "Search membership, transaction or booking reference",
        "Rechercher une adhésion, une opération ou une réservation",
    ),
    "filter": ("بحث", "Search", "Rechercher"),
    "root": ("الجذور", "Roots", "Racines"),
    "children": ("المدعوون المباشرون", "Direct invitees", "Invités directs"),
    "open_branch": ("فتح الفرع", "Open branch", "Ouvrir la branche"),
    "parent": ("الداعي المباشر", "Direct referrer", "Parrain direct"),
    "more": ("عرض المزيد", "Show more", "Afficher la suite"),
    "all": ("كل العمليات", "All transactions", "Toutes les opérations"),
    "member": ("العضو", "Member", "Membre"),
    "arrival": ("توثيق وصول الضيف", "Record guest arrival", "Enregistrer l’arrivée"),
    "arrival_reference": (
        "مرجع تأكيد الوصول",
        "Arrival confirmation reference",
        "Référence de confirmation d’arrivée",
    ),
    "arrival_time": (
        "وقت الوصول الفعلي بتوقيت الرياض",
        "Actual arrival time in Riyadh time",
        "Heure réelle d’arrivée, heure de Riyad",
    ),
    "arrival_help": (
        "تأكيد للوصول الفعلي فقط، وليس توثيقًا للدفع. احتساب النقاط تلقائي بعد تحقق الشروط.",
        "This confirms actual arrival, not payment. Rewards follow automatically when eligible.",
        "Confirme l’arrivée réelle, non le paiement. Les points suivent automatiquement si éligibles.",
    ),
    "arrival_preview": (
        "هذه التجربة تقبل الحجوزات الافتراضية فقط.",
        "This preview accepts fictional bookings only.",
        "Cet aperçu accepte uniquement des réservations fictives.",
    ),
    "arrival_saved": (
        "حُفظ تأكيد الوصول؛ لا يعني ذلك استحقاق النقاط دون تحقق بقية الشروط.",
        "Arrival recorded; rewards still require the other eligibility conditions.",
        "Arrivée enregistrée ; les autres conditions restent nécessaires pour les points.",
    ),
}
TYPES = {
    "loyalty": ("مكافأة إقامة", "Stay reward", "Points de séjour"),
    "addition": ("إضافة نقاط", "Points added", "Ajout de points"),
    "transfer": ("تحويل نقاط", "Points transfer", "Transfert de points"),
    "direct": ("دعوة مباشرة", "Direct referral", "Parrainage direct"),
    "older": ("دعوة غير مباشرة", "Earlier referral", "Parrainage antérieur"),
    "correction": ("تصحيح نقاط", "Points correction", "Correction de points"),
    "redeem": ("استخدام نقاط", "Points redeemed", "Utilisation de points"),
    "restore": ("إعادة نقاط", "Points restored", "Restitution de points"),
    "withdraw": ("إقفال العضوية", "Membership closure", "Clôture de l’adhésion"),
}


def copy(language=""):
    lang = (language or get_language() or "ar").split("-")[0]
    index = {"ar": 0, "en": 1, "fr": 2}.get(lang, 0)
    return {key: value[index] for key, value in LABELS.items()}


def transaction_label(row):
    lang = (get_language() or "ar").split("-")[0]
    key = (
        row.event_type
        if row.event_type in {"adjust", "redeem", "restore", "withdraw"}
        else row.operation_type
    )
    if key == "adjust":
        key = "correction"
    # Model operation types are kept independent of presentation labels.
    key = {"direct_referral": "direct", "indirect_referral": "older"}.get(key, key)
    return TYPES.get(key, TYPES["addition"])[{"ar": 0, "en": 1, "fr": 2}.get(lang, 0)]
