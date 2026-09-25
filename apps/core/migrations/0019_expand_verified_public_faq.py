from django.db import migrations

EXISTING_FAQ_UPDATES = {
    "Is the displayed price final?": {
        "category": "booking",
        "question_ar": "هل السعر المعروض نهائي؟",
        "question_en": "Is the displayed price final?",
        "question_fr": "Le prix affiché est-il définitif ?",
        "answer_ar": (
            "السعر المعروض عرض مؤقت يعتمد على التواريخ والوحدة المختارة. نعيد التحقق "
            "من السعر والتوافر قبل الدفع، ويصبح السعر المعتمد هو السعر الظاهر في ملخص "
            "الحجز قبل إتمام الدفع."
        ),
        "answer_en": (
            "The displayed price is a temporary quote based on the selected dates and "
            "property. We check price and availability again before payment; the final "
            "price is the amount shown in the booking summary before you complete payment."
        ),
        "answer_fr": (
            "Le prix affiché est un devis temporaire calculé selon les dates et le logement "
            "sélectionnés. Nous vérifions à nouveau le prix et la disponibilité avant le "
            "paiement ; le prix définitif est celui indiqué dans le récapitulatif avant la "
            "validation du paiement."
        ),
        "sort_order": 30,
    },
    "Does submitting guest details confirm a booking?": {
        "category": "booking",
        "question_ar": "متى يصبح حجزي مؤكدًا؟",
        "question_en": "When is my booking confirmed?",
        "question_fr": "Quand ma réservation est-elle confirmée ?",
        "answer_ar": (
            "لا يكفي إدخال بيانات الضيف وحده. يصبح الحجز مؤكدًا بعد التحقق من الدفع "
            "وإصدار تأكيد الحجز مع رقم مرجعي واضح. إذا تم خصم المبلغ ولم يصلك التأكيد، "
            "فلا تدفع مرة أخرى وتواصل معنا."
        ),
        "answer_en": (
            "Entering guest details alone does not confirm a booking. Your booking is "
            "confirmed after payment is verified and a confirmation with a clear reference "
            "number is issued. If you were charged but did not receive confirmation, do not "
            "pay again; contact us."
        ),
        "answer_fr": (
            "La saisie des informations du voyageur ne confirme pas à elle seule la "
            "réservation. Celle-ci est confirmée après vérification du paiement et émission "
            "d’une confirmation avec une référence claire. Si vous avez été débité sans "
            "recevoir de confirmation, ne payez pas une seconde fois et contactez-nous."
        ),
        "sort_order": 40,
    },
    "When is the full address shared?": {
        "category": "stay",
        "question_ar": "متى سأستلم تعليمات الوصول والعنوان الكامل؟",
        "question_en": "When will I receive arrival instructions and the full address?",
        "question_fr": (
            "Quand recevrai-je les instructions d’arrivée et l’adresse complète ?"
        ),
        "answer_ar": (
            "لا نعرض العنوان الخاص كاملًا في الصفحات العامة. تُرسل تعليمات الوصول "
            "والعنوان وتفاصيل الدخول إلى الضيف صاحب الحجز المؤكد بعد استيفاء متطلبات "
            "الحجز والتحقق. وقد يختلف توقيت الإرسال وطريقة الدخول حسب الوحدة."
        ),
        "answer_en": (
            "We do not display the complete private address on public pages. Arrival "
            "instructions, the address and access details are sent to the confirmed guest "
            "after the booking and verification requirements are completed. Timing and "
            "access method may vary by property."
        ),
        "answer_fr": (
            "Nous n’affichons pas l’adresse privée complète sur les pages publiques. Les "
            "instructions d’arrivée, l’adresse et les modalités d’accès sont envoyées au "
            "voyageur dont la réservation est confirmée, après satisfaction des exigences "
            "de réservation et de vérification. Le moment d’envoi et le mode d’accès peuvent "
            "varier selon le logement."
        ),
        "sort_order": 20,
    },
}


NEW_FAQS = [
    {
        "category": "booking",
        "question_ar": "كيف أتحقق من التوافر والأسعار الحالية؟",
        "question_en": "How can I check availability and current prices?",
        "question_fr": "Comment vérifier les disponibilités et les prix actuels ?",
        "answer_ar": (
            "اختر تاريخ الوصول والمغادرة من صفحة البحث. سيعرض الموقع الوحدات المتاحة "
            "والسعر الخاص بالفترة المختارة. لأن التوافر والأسعار يتغيران، أعد البحث عند "
            "تغيير التواريخ أو قبل متابعة الحجز."
        ),
        "answer_en": (
            "Select your check-in and check-out dates on the search page. The website will "
            "show available properties and the price for the selected stay. Availability and "
            "prices can change, so search again after changing dates or before continuing."
        ),
        "answer_fr": (
            "Choisissez vos dates d’arrivée et de départ sur la page de recherche. Le site "
            "affichera les logements disponibles et le prix du séjour sélectionné. Les "
            "disponibilités et les prix pouvant évoluer, relancez la recherche après toute "
            "modification des dates ou avant de poursuivre."
        ),
        "sort_order": 10,
    },
    {
        "category": "booking",
        "question_ar": "كيف أحجز من الموقع؟",
        "question_en": "How do I make a booking on the website?",
        "question_fr": "Comment réserver sur le site ?",
        "answer_ar": (
            "ابحث بالتواريخ المطلوبة، واختر الوحدة المناسبة، ثم راجع السعر والتفاصيل "
            "وأدخل بيانات الضيف وأكمل خطوات الدفع الظاهرة. بعد نجاح العملية سيصلك تأكيد "
            "الحجز ورقمه المرجعي."
        ),
        "answer_en": (
            "Search for your dates, choose a suitable property, review the price and stay "
            "details, enter the guest information and complete the displayed payment steps. "
            "After a successful booking, you will receive a confirmation and reference number."
        ),
        "answer_fr": (
            "Recherchez vos dates, choisissez un logement, vérifiez le prix et les détails du "
            "séjour, saisissez les informations du voyageur puis suivez les étapes de paiement "
            "affichées. Une réservation réussie donne lieu à une confirmation et à un numéro "
            "de référence."
        ),
        "sort_order": 20,
    },
    {
        "category": "booking",
        "question_ar": "ما الذي ينبغي مراجعته قبل تأكيد الحجز؟",
        "question_en": "What should I double-check before confirming a booking?",
        "question_fr": "Que faut-il vérifier avant de confirmer une réservation ?",
        "answer_ar": (
            "راجع المدينة والموقع، والتواريخ، وعدد الضيوف، وترتيب الأسرّة، والمرافق، "
            "ومعلومات المواقف والدخول، والسعر وشروط الإلغاء الخاصة بالحجز. إذا كانت إحدى "
            "هذه النقاط مهمة لرحلتك وغير واضحة، فتواصل معنا قبل الدفع."
        ),
        "answer_en": (
            "Check the destination and location, dates, guest capacity, bed setup, amenities, "
            "parking and access information, price and the cancellation terms for your booking. "
            "If an important detail is unclear, contact us before payment."
        ),
        "answer_fr": (
            "Vérifiez la destination et l’emplacement, les dates, la capacité, la disposition "
            "des lits, les équipements, le stationnement et l’accès, le prix ainsi que les "
            "conditions d’annulation de votre réservation. Si un point essentiel reste flou, "
            "contactez-nous avant le paiement."
        ),
        "sort_order": 50,
    },
    {
        "category": "policy",
        "question_ar": "هل يمكنني تعديل الحجز أو إلغاؤه؟",
        "question_en": "Can I modify or cancel my booking?",
        "question_fr": "Puis-je modifier ou annuler ma réservation ?",
        "answer_ar": (
            "يمكنك تقديم طلب من صفحة إدارة الحجز أو عبر صفحة اتصل بنا باستخدام البريد "
            "ورقم الحجز نفسيهما. تعتمد الموافقة وأي فرق سعر أو استرداد على التوافر ونوع "
            "السعر وشروط الحجز المؤكد. لا يصبح الطلب نافذًا إلا بعد إرسال تأكيد محدث."
        ),
        "answer_en": (
            "You can submit a request through the booking-management page or contact us using "
            "the same email address and booking reference. Approval, any price difference and "
            "any refund depend on availability, the rate type and the confirmed booking terms. "
            "A request takes effect only after an updated confirmation is issued."
        ),
        "answer_fr": (
            "Vous pouvez envoyer une demande depuis la page de gestion de la réservation ou "
            "nous contacter avec la même adresse e-mail et la référence de réservation. "
            "L’acceptation, tout écart de prix et tout remboursement dépendent des disponibilités, "
            "du tarif et des conditions confirmées. La demande ne prend effet qu’après émission "
            "d’une confirmation mise à jour."
        ),
        "sort_order": 10,
    },
    {
        "category": "policy",
        "question_ar": "كيف تتم معالجة الاسترداد المستحق؟",
        "question_en": "How is an eligible refund processed?",
        "question_fr": "Comment un remboursement éligible est-il traité ?",
        "answer_ar": (
            "بعد اعتماد الاسترداد، يُعاد المبلغ المؤهل عادةً إلى وسيلة الدفع الأصلية. "
            "يعتمد وقت ظهوره على مزود الدفع والبنك المصدر وقد يستغرق عدة أيام عمل بعد "
            "التنفيذ. سنرسل إشعارًا عند تسجيل الاسترداد من جانبنا."
        ),
        "answer_en": (
            "Once approved, an eligible refund is normally returned to the original payment "
            "method. The time it takes to appear depends on the payment provider and issuing "
            "bank and may be several business days after processing. We will notify you when "
            "the refund is recorded on our side."
        ),
        "answer_fr": (
            "Une fois approuvé, le remboursement éligible est normalement reversé sur le moyen "
            "de paiement d’origine. Son apparition dépend du prestataire de paiement et de la "
            "banque émettrice et peut prendre plusieurs jours ouvrables après le traitement. "
            "Nous vous informerons lorsque le remboursement sera enregistré de notre côté."
        ),
        "sort_order": 20,
    },
    {
        "category": "stay",
        "question_ar": "هل يتوفر تسجيل دخول ذاتي؟",
        "question_en": "Is self-check-in available?",
        "question_fr": "L’arrivée autonome est-elle disponible ?",
        "answer_ar": (
            "يتوفر الدخول الذاتي أو الذكي في عدد من وحداتنا، لكن طريقة الوصول قد تختلف "
            "حسب الوحدة والمبنى. راجع تفاصيل الوحدة وتأكيد الحجز، واتبع تعليمات الوصول "
            "المرسلة إليك."
        ),
        "answer_en": (
            "Self-check-in or smart access is available at many of our properties, but the "
            "access method can vary by property and building. Check the property details and "
            "booking confirmation, then follow the arrival instructions sent to you."
        ),
        "answer_fr": (
            "L’arrivée autonome ou l’accès intelligent est disponible dans de nombreux "
            "logements, mais la méthode peut varier selon le logement et l’immeuble. Consultez "
            "les détails et la confirmation, puis suivez les instructions d’arrivée reçues."
        ),
        "sort_order": 10,
    },
    {
        "category": "stay",
        "question_ar": "أين أجد وقت الدخول والخروج؟",
        "question_en": "Where can I find the check-in and check-out times?",
        "question_fr": "Où trouver les horaires d’arrivée et de départ ?",
        "answer_ar": (
            "قد تختلف الأوقات حسب الوحدة. يظهر الوقت الخاص بالوحدة في صفحتها أو أثناء "
            "الحجز، ويُثبت في تأكيد الحجز وتعليمات الوصول. اعتمد دائمًا الوقت المذكور في "
            "تأكيد حجزك."
        ),
        "answer_en": (
            "Times may vary by property. The applicable time is shown on the property page or "
            "during booking and is recorded in your confirmation and arrival instructions. "
            "Always follow the time stated in your booking confirmation."
        ),
        "answer_fr": (
            "Les horaires peuvent varier selon le logement. L’horaire applicable est affiché "
            "sur sa page ou pendant la réservation, puis indiqué dans la confirmation et les "
            "instructions d’arrivée. Référez-vous toujours à votre confirmation."
        ),
        "sort_order": 30,
    },
    {
        "category": "stay",
        "question_ar": "هل يمكن طلب دخول مبكر أو خروج متأخر؟",
        "question_en": "Can I request early check-in or late check-out?",
        "question_fr": "Puis-je demander une arrivée anticipée ou un départ tardif ?",
        "answer_ar": (
            "يمكنك إرسال الطلب، لكن الموافقة تعتمد على التوافر وجدول تنظيف الوحدة ولا "
            "يمكن ضمانها مسبقًا. تواصل معنا مبكرًا مع رقم الحجز والوقت المطلوب."
        ),
        "answer_en": (
            "You are welcome to request it, but approval depends on availability and the "
            "property’s preparation schedule and cannot be guaranteed in advance. Contact us "
            "early with your booking reference and requested time."
        ),
        "answer_fr": (
            "Vous pouvez en faire la demande, mais son acceptation dépend des disponibilités "
            "et du planning de préparation du logement et ne peut être garantie à l’avance. "
            "Contactez-nous suffisamment tôt avec votre référence et l’horaire souhaité."
        ),
        "sort_order": 40,
    },
    {
        "category": "stay",
        "question_ar": "كيف أحصل على المساعدة قبل الوصول أو أثناء الإقامة؟",
        "question_en": "How can I get help before arrival or during my stay?",
        "question_fr": "Comment obtenir de l’aide avant l’arrivée ou pendant le séjour ?",
        "answer_ar": (
            "استخدم وسائل التواصل الظاهرة في صفحة اتصل بنا أو في تأكيد الحجز، واذكر رقم "
            "الحجز ووصفًا مختصرًا للمشكلة. أبلغنا مبكرًا عن مشاكل الوصول أو الصيانة، "
            "وأرفق صورة عند الحاجة، ولا ترسل بيانات البطاقة أو كلمات المرور أو رموز الدخول."
        ),
        "answer_en": (
            "Use the contact options shown on our contact page or in your booking confirmation, "
            "and include your booking reference and a short description. Report access or "
            "maintenance issues promptly and add a photo when useful. Never send card details, "
            "passwords or access codes."
        ),
        "answer_fr": (
            "Utilisez les coordonnées indiquées sur notre page de contact ou dans votre "
            "confirmation, en précisant votre référence et une brève description. Signalez "
            "rapidement tout problème d’accès ou d’entretien et joignez une photo si utile. "
            "N’envoyez jamais de données de carte, mots de passe ou codes d’accès."
        ),
        "sort_order": 50,
    },
    {
        "category": "property",
        "question_ar": "هل تقع جميع الوحدات في مدينة واحدة؟",
        "question_en": "Are all properties in the same destination?",
        "question_fr": "Tous les logements se trouvent-ils dans la même destination ?",
        "answer_ar": (
            "لا. نعرض وحدات في الرياض ومراكش، وقد تختلف الأحياء وطبيعة الإقامة من وحدة "
            "إلى أخرى. تحقق من المدينة والموقع في بطاقة الوحدة وصفحتها قبل الحجز."
        ),
        "answer_en": (
            "No. We offer properties in Riyadh and Marrakech, and neighbourhoods and stay "
            "styles differ by property. Check the destination and location on the property "
            "card and detail page before booking."
        ),
        "answer_fr": (
            "Non. Nous proposons des logements à Riyad et à Marrakech ; les quartiers et les "
            "types de séjour varient. Vérifiez la destination et l’emplacement sur la fiche "
            "et la page du logement avant de réserver."
        ),
        "sort_order": 10,
    },
    {
        "category": "property",
        "question_ar": "ما مدى قرب الوحدة من المعالم والمطاعم والخدمات؟",
        "question_en": "How close is a property to attractions, restaurants and services?",
        "question_fr": (
            "À quelle distance le logement se trouve-t-il des attractions, "
            "restaurants et services ?"
        ),
        "answer_ar": (
            "يختلف ذلك حسب الوحدة والمدينة وحالة المرور. راجع وصف الموقع والخريطة في صفحة "
            "الوحدة، وإذا كان القرب من مكان محدد مهمًا لرحلتك فتواصل معنا قبل الحجز."
        ),
        "answer_en": (
            "This varies by property, destination and traffic conditions. Review the location "
            "description and map on the property page. If proximity to a specific place is "
            "important, contact us before booking."
        ),
        "answer_fr": (
            "Cela varie selon le logement, la destination et la circulation. Consultez la "
            "description de l’emplacement et la carte sur la page du logement. Si la proximité "
            "d’un lieu précis est essentielle, contactez-nous avant de réserver."
        ),
        "sort_order": 20,
    },
    {
        "category": "property",
        "question_ar": "هل تتوفر مواقف سيارات؟",
        "question_en": "Is parking available?",
        "question_fr": "Un stationnement est-il disponible ?",
        "answer_ar": (
            "تختلف المواقف حسب الوحدة والمبنى، وقد تكون داخلية أو خارجية أو خاضعة لتوفر "
            "المساحات. راجع معلومات الوحدة، وإذا كان لديك مركبة كبيرة أو كان الموقف شرطًا "
            "أساسيًا فتواصل معنا للتأكد قبل الحجز."
        ),
        "answer_en": (
            "Parking arrangements vary by property and building and may be indoor, outdoor or "
            "subject to space availability. Check the property information. If you have a large "
            "vehicle or parking is essential, contact us to confirm before booking."
        ),
        "answer_fr": (
            "Le stationnement varie selon le logement et l’immeuble ; il peut être intérieur, "
            "extérieur ou soumis aux places disponibles. Consultez les informations du logement. "
            "Si vous avez un grand véhicule ou si le stationnement est indispensable, "
            "contactez-nous pour confirmer avant de réserver."
        ),
        "sort_order": 30,
    },
    {
        "category": "property",
        "question_ar": "ما المرافق المتوفرة في الوحدة؟",
        "question_en": "What amenities are included?",
        "question_fr": "Quels équipements sont inclus ?",
        "answer_ar": (
            "تختلف المرافق والتجهيزات من وحدة إلى أخرى. تعرض صفحة كل وحدة المرافق المتاحة "
            "لها؛ راجعها قبل الحجز ولا تفترض أن جميع الوحدات تحتوي على التجهيزات نفسها."
        ),
        "answer_en": (
            "Amenities and equipment vary by property. Each property page lists the available "
            "features; review them before booking and do not assume that every property has the "
            "same setup."
        ),
        "answer_fr": (
            "Les équipements varient selon le logement. Chaque page indique les prestations "
            "disponibles ; consultez-les avant de réserver et ne supposez pas que tous les "
            "logements sont équipés de la même façon."
        ),
        "sort_order": 40,
    },
    {
        "category": "property",
        "question_ar": "هل يتم تنظيف الوحدة قبل الوصول؟",
        "question_en": "Is the property cleaned before arrival?",
        "question_fr": "Le logement est-il nettoyé avant l’arrivée ?",
        "answer_ar": (
            "نعم، تُجهز الوحدة وتُنظف قبل وصول الضيف. إذا لاحظت أي مشكلة عند الدخول، "
            "فتواصل معنا في أقرب وقت وأرسل صورة عند الحاجة حتى نتمكن من المساعدة."
        ),
        "answer_en": (
            "Yes. The property is prepared and cleaned before guest arrival. If you notice an "
            "issue when you enter, contact us promptly and include a photo when useful so that "
            "we can assist."
        ),
        "answer_fr": (
            "Oui. Le logement est préparé et nettoyé avant l’arrivée du voyageur. Si vous "
            "constatez un problème à votre entrée, contactez-nous rapidement et joignez une "
            "photo si utile afin que nous puissions vous aider."
        ),
        "sort_order": 50,
    },
    {
        "category": "property",
        "question_ar": "هل الوحدات مناسبة للعائلات والأطفال؟",
        "question_en": "Are the properties suitable for families and children?",
        "question_fr": "Les logements conviennent-ils aux familles et aux enfants ?",
        "answer_ar": (
            "تصلح وحدات عديدة للعائلات، لكن السعة والتصميم وترتيب الأسرّة والمرافق تختلف. "
            "راجع صفحة الوحدة وأدخل عدد الضيوف الصحيح، وتواصل معنا قبل الحجز إذا كانت لديك "
            "متطلبات خاصة بالأطفال أو الرضع."
        ),
        "answer_en": (
            "Many properties suit family stays, but capacity, layout, bed setup and amenities "
            "vary. Review the property page, enter the correct guest count and contact us before "
            "booking if you have specific needs for children or infants."
        ),
        "answer_fr": (
            "De nombreux logements conviennent aux familles, mais la capacité, l’agencement, "
            "les lits et les équipements varient. Consultez la page du logement, indiquez le "
            "nombre exact de voyageurs et contactez-nous avant de réserver pour tout besoin "
            "particulier concernant les enfants ou les nourrissons."
        ),
        "sort_order": 60,
    },
    {
        "category": "property",
        "question_ar": "هل جميع الوحدات مفروشة ومجهزة بالطريقة نفسها؟",
        "question_en": "Are all properties furnished and equipped in the same way?",
        "question_fr": "Tous les logements sont-ils meublés et équipés de la même façon ?",
        "answer_ar": (
            "لا. لكل وحدة تصميمها ومساحتها وترتيبها وتجهيزاتها الخاصة. الصور والوصف "
            "والمرافق في صفحة الوحدة هي المرجع المناسب لمقارنة الخيارات قبل الحجز."
        ),
        "answer_en": (
            "No. Each property has its own design, size, layout and equipment. Use the photos, "
            "description and amenities on the property page to compare options before booking."
        ),
        "answer_fr": (
            "Non. Chaque logement possède son propre style, sa superficie, son agencement et "
            "ses équipements. Utilisez les photos, la description et les prestations indiquées "
            "sur sa page pour comparer avant de réserver."
        ),
        "sort_order": 70,
    },
]


def expand_verified_faq(apps, schema_editor):
    del schema_editor
    faq_item = apps.get_model("core", "FAQItem")

    for old_question_en, values in EXISTING_FAQ_UPDATES.items():
        faq_item.objects.filter(
            question_en=old_question_en,
            property__isnull=True,
        ).update(**values, is_active=True)

    for values in NEW_FAQS:
        faq_item.objects.update_or_create(
            question_en=values["question_en"],
            property=None,
            defaults={**values, "is_active": True},
        )


def remove_expanded_faq(apps, schema_editor):
    del schema_editor
    faq_item = apps.get_model("core", "FAQItem")
    faq_item.objects.filter(
        question_en__in=[item["question_en"] for item in NEW_FAQS],
        property__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0018_language_urls_and_confirmed_listing_redirects")]

    operations = [
        migrations.RunPython(expand_verified_faq, remove_expanded_faq),
    ]
