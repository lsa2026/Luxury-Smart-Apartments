"""Three-language copy for the private rewards workspace."""

# ruff: noqa: E501
from django.utils.translation import get_language

# Each row supplies Arabic, English and French; no UI translation fallback.
LABELS = {
    "gift_phone_heading": (
        "إهداء النقاط برقم الجوال",
        "Gift points by mobile",
        "Offrir des points par mobile",
    ),
    "gift_phone_help": (
        "أدخل جوال المستلم الموثق بحسابه. سنراجع العضوية ونعرض اسمًا مختصرًا قبل تأكيد التحويل.",
        "Enter the mobile verified on the recipient’s account. We check membership and show a shortened name before confirmation.",
        "Saisissez le mobile vérifié du destinataire. Nous vérifions l’adhésion et affichons un nom abrégé avant confirmation.",
    ),
    "gift_member_alternative": (
        "أو استخدم رقم العضوية",
        "Or use a membership number",
        "Ou utiliser un numéro d’adhésion",
    ),
    "gift_recipient_name": (
        "اسم المستلم المختصر",
        "Shortened recipient name",
        "Nom abrégé du destinataire",
    ),
    "gift_unavailable": (
        "لا يمكن التحويل إلى هذا الرقم حاليًا. يستطيع صاحبه إنشاء حساب والانضمام إلى برنامج الولاء وتوثيق جواله، ثم تعيد البحث عنه.",
        "This number cannot receive points yet. Its owner can create an account, join rewards and verify their mobile; then search again.",
        "Ce numéro ne peut pas encore recevoir de points. Son titulaire peut créer un compte, adhérer et vérifier son mobile ; recherchez-le ensuite.",
    ),
    "gift_invite_explain": (
        "بموافقتك، نرسل دعوة واحدة عبر رقم واتساب الأعمال الخاص بنا، باسمك وبرابط دعوتك. لن تُخصم أو تُحجز أي نقاط، ولن يحدث تحويل تلقائي بعد الانضمام.",
        "With your approval, we send one invitation from our business WhatsApp, in your name with your invitation link. No points are deducted or reserved, and joining never triggers an automatic transfer.",
        "Avec votre accord, nous envoyons une invitation depuis notre WhatsApp professionnel, en votre nom avec votre lien. Aucun point n’est débité ou réservé et l’adhésion ne déclenche aucun transfert automatique.",
    ),
    "gift_message_language": ("لغة رسالة الدعوة", "Invitation language", "Langue de l’invitation"),
    "gift_invite_consent": (
        "أؤكد أن صاحب الرقم يتوقع هذه الدعوة، وأوافق على إرسالها باسمي عبر واتساب الأعمال.",
        "I confirm the recipient expects this invitation and approve sending it in my name through business WhatsApp.",
        "Je confirme que le destinataire attend cette invitation et j’autorise son envoi en mon nom via WhatsApp professionnel.",
    ),
    "gift_invite_send": (
        "إرسال دعوة للانضمام عبر واتساب",
        "Send a WhatsApp invitation",
        "Envoyer une invitation par WhatsApp",
    ),
    "gift_private_scope": (
        "الدعوة لا تتجاوز نطاق التجربة الخاصة الحالية؛ يبقى الانضمام متاحًا لحسابات التجربة المعتمدة فقط.",
        "Invitations do not bypass the private trial; rewards membership is still limited to approved trial accounts.",
        "L’invitation ne contourne pas l’essai privé : l’adhésion reste réservée aux comptes approuvés.",
    ),
    "gift_status_queued": (
        "طلب الدعوة محفوظ وينتظر الإرسال؛ لم تُخصم أي نقاط. حدّث الحالة للتحقق.",
        "Your invitation is queued; no points were deducted. Refresh to check its status.",
        "Votre invitation est en attente d’envoi ; aucun point débité. Actualisez son état.",
    ),
    "gift_status_sending": (
        "جارٍ إرسال الدعوة؛ لم تُخصم أي نقاط.",
        "The invitation is being sent; no points were deducted.",
        "L’invitation est en cours d’envoi ; aucun point débité.",
    ),
    "gift_status_accepted": (
        "قبلت خدمة واتساب طلب إرسال الدعوة. هذا لا يثبت وصولها أو قراءتها. بعد انضمام المستلم وتوثيق جواله، أعد البحث لتأكيد الهدية.",
        "WhatsApp accepted the invitation for sending; this does not prove delivery or reading. Once the recipient joins and verifies their mobile, search again to confirm the gift.",
        "WhatsApp a accepté l’envoi ; cela ne prouve ni réception ni lecture. Après adhésion et vérification du mobile, recherchez à nouveau pour confirmer le cadeau.",
    ),
    "gift_status_queue_failed": (
        "تعذر تسليم الدعوة لخدمة الإرسال. لم تُخصم أي نقاط؛ يمكنك نسخ رابط دعوتك ومشاركته بنفسك.",
        "The invitation could not be queued. No points were deducted; you can copy and share your invitation link yourself.",
        "L’invitation n’a pas pu être mise en file. Aucun point débité ; vous pouvez copier et partager votre lien.",
    ),
    "gift_status_disabled": (
        "لم تُرسل الدعوة لأن شروط الإرسال لم تعد متحققة. لم تُخصم نقاط.",
        "The invitation was not sent because sending requirements were no longer met. No points were deducted.",
        "L’invitation n’a pas été envoyée car les conditions n’étaient plus remplies. Aucun point débité.",
    ),
    "gift_status_recipient_ready": (
        "أصبح الرقم مؤهلًا لاستقبال النقاط قبل إرسال الدعوة. أعد البحث عنه لمراجعة الهدية؛ لم يحدث تحويل تلقائي.",
        "The number became eligible before the invitation was sent. Search again to review your gift; no automatic transfer occurred.",
        "Le numéro est devenu éligible avant l’envoi. Recherchez-le à nouveau pour vérifier le cadeau ; aucun transfert automatique.",
    ),
    "gift_status_uncertain": (
        "لم نتمكن من تأكيد نتيجة إرسال الدعوة، لذلك لن نعيد إرسالها تلقائيًا. لم تُخصم نقاط؛ يمكنك مشاركة رابط دعوتك بنفسك.",
        "The sending outcome could not be confirmed, so we will not resend automatically. No points were deducted; you can share your invitation link yourself.",
        "Le résultat de l’envoi est incertain ; aucun renvoi automatique. Aucun point débité ; vous pouvez partager votre lien vous-même.",
    ),
    "gift_refresh": (
        "تحديث حالة الدعوة",
        "Refresh invitation status",
        "Actualiser l’état de l’invitation",
    ),
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
    "stay_exception_notice": (
        "لهذا الحساب حجز واحد معتمد للتجربة دون اشتراط إقامة فعلية. لا يسجل ذلك وصولًا أو إقامة مكتملة، ولا يعفي من إثبات الدفع والموافقة، ولا يشمل الحجوزات الأخرى.",
        "One booking on this account has an authorised test-only stay exception. This records neither arrival nor a completed stay, does not waive payment evidence or consent, and does not apply to other bookings.",
        "Une seule réservation de ce compte bénéficie d’une dérogation de séjour pour cet essai. Elle ne constate ni arrivée ni séjour terminé, ne dispense pas de justificatif de paiement ni de consentement et ne concerne aucune autre réservation.",
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
        "عضوية مجانية واختيارية؛ تسجيل الدخول لا يعني الانضمام. بعد تأكيد السداد، تضاف مكافأتك تلقائيًا من الثالثة عصرًا يوم الدخول بتوقيت العقار، أو عند توثيق الإدارة للوصول في أي وقت، أيهما أسبق.",
        "Membership is free and optional. Signing in does not enrol you. After confirmed payment, rewards are added automatically from 3 pm on check-in day in the property’s local time, or when administration records arrival at any time, whichever comes first.",
        "L’adhésion est gratuite et facultative. Se connecter ne vous inscrit pas. Après confirmation du paiement, les points sont ajoutés automatiquement à partir de 15 h le jour d’arrivée, heure locale du logement, ou dès que l’administration enregistre l’arrivée à tout moment, selon la première éventualité.",
    ),
    "accrual_help": (
        "متى تضاف مكافأتك؟ بعد تأكيد السداد، من الثالثة عصرًا يوم الدخول بتوقيت العقار، أو عند توثيق الإدارة للوصول في أي وقت، أيهما أسبق. تُضاف المكافأة مرة واحدة.",
        "When are rewards added? After confirmed payment, from 3 pm on check-in day in the property’s local time, or when administration records arrival at any time, whichever comes first. Each reward is credited once.",
        "Quand les points sont-ils ajoutés ? Après paiement confirmé, à partir de 15 h le jour d’arrivée, heure locale du logement, ou dès que l’administration enregistre l’arrivée à tout moment, selon la première éventualité. Chaque récompense est créditée une seule fois.",
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
    "version": (
        "قواعد الاحتساب المطبقة",
        "Calculation rules applied",
        "Règles de calcul appliquées",
    ),
    "rules_first": (
        "الإصدار الأول — ١٠ أكتوبر ٢٠٢٦",
        "First edition — 10 October 2026",
        "Première édition — 10 octobre 2026",
    ),
    "rules_other": (
        "إصدار محفوظ وقت العملية",
        "Edition recorded at posting",
        "Édition conservée lors de l’opération",
    ),
    "rules_help": (
        "توضح طريقة الاحتساب وقت تسجيل الحركة، وليست نسخة شروط العضوية. لا تتغير العمليات السابقة عند تعديل القواعد لاحقًا.",
        "These identify the calculation rules at posting, not the membership terms. Later rule changes do not rewrite past transactions.",
        "Ces règles identifient le calcul lors de l’opération, et non les conditions d’adhésion. Les changements ultérieurs ne modifient pas les opérations passées.",
    ),
    "rules_reference": (
        "المرجع التقني للقواعد",
        "Technical rules reference",
        "Référence technique des règles",
    ),
    "gift_out": ("إهداء نقاط إلى عضو", "Points gifted to a member", "Points offerts à un membre"),
    "gift_in": ("هدية نقاط من عضو", "Points received as a gift", "Points reçus en cadeau"),
    "gift_to": ("أُهديت إلى", "Gifted to", "Offerts à"),
    "gift_from": ("هدية من", "Gift from", "Cadeau de"),
    "counterpart_number": (
        "رقم عضوية الطرف الآخر",
        "Other member’s number",
        "Numéro de l’autre membre",
    ),
    "paired_operation": (
        "رقم العملية المقابلة",
        "Paired transaction number",
        "Numéro de l’opération correspondante",
    ),
    "member_before": (
        "رصيد العضو قبل العملية",
        "Member balance before this transaction",
        "Solde du membre avant l’opération",
    ),
    "before_balance": (
        "رصيدي قبل العملية",
        "My balance before this transaction",
        "Mon solde avant l’opération",
    ),
    "posted_status": (
        "مكتملة ومسجلة في الحساب",
        "Completed and posted",
        "Terminée et comptabilisée",
    ),
    "transaction_status": ("حالة العملية", "Transaction status", "État de l’opération"),
    "gift_pair_missing": (
        "تفاصيل الطرف الآخر غير متاحة؛ تواصل معنا مع رقم العملية للمراجعة.",
        "Other member details are unavailable. Contact us with this transaction number.",
        "Les coordonnées de l’autre membre ne sont pas disponibles. Contactez-nous avec le numéro d’opération.",
    ),
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
    "copy_invite": ("نسخ رابط الدعوة", "Copy invite link", "Copier le lien d’invitation"),
    "copy_success": ("تم نسخ الرابط", "Link copied", "Lien copié"),
    "copy_failed": (
        "تعذر النسخ التلقائي. اضغط مطولًا على الرابط لنسخه.",
        "Automatic copying was unavailable. Select the link to copy it.",
        "La copie automatique est indisponible. Sélectionnez le lien pour le copier.",
    ),
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
        "توثيق الإدارة للوصول يتيح الاستحقاق في أي وقت، ولو قبل يوم الدخول، مع بقاء السداد المؤكد والانضمام وأهلية الحجز شروطًا لازمة.",
        "Administration may record arrival at any time, including before check-in day. Confirmed payment, membership and booking eligibility remain required.",
        "L’administration peut enregistrer l’arrivée à tout moment, même avant le jour prévu. Paiement confirmé, adhésion et réservation admissible restent obligatoires.",
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
