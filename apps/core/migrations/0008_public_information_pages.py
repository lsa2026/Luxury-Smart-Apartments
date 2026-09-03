from datetime import date

from django.db import migrations, models


PAGES = {
    "about": {
        "title_ar": "من نحن",
        "title_en": "About us",
        "title_fr": "À propos",
        "meta_description_ar": (
            "تعرّف على Luxury Smart Apartments وتجربة الإقامة الفاخرة والذكية التي نقدمها "
            "للأعمال والعائلات في الرياض."
        ),
        "meta_description_en": (
            "Discover Luxury Smart Apartments and our premium, technology-enabled stays "
            "for business travellers and families in Riyadh."
        ),
        "meta_description_fr": (
            "Découvrez Luxury Smart Apartments et nos séjours haut de gamme et connectés "
            "pour les voyageurs d’affaires et les familles à Riyad."
        ),
        "body_ar": """
إقامة فاخرة تعززها التقنيات الذكية والهندسة الفندقية.

## قصتنا
تأسست Luxury Smart Apartments في قلب الرياض لتقدم وجهة إقامة للضيوف الذين يبحثون عن الفخامة العصرية من دون التنازل عن الخصوصية أو سهولة الاستخدام. ننقل الإقامة قصيرة المدى من مفهوم السكن التقليدي إلى تجربة ذكية مصممة بعناية لرجال وسيدات الأعمال والعائلات الخليجية والمسافرين.

## ركائز تجربتنا
### سكن ذكي متكامل
توفر وحداتنا منظومة منزلية ذكية تدعم Apple HomeKit والتحكم الصوتي عبر Siri لإدارة الإضاءة والتكييف وأنظمة الترفيه بسهولة.

### رفاهية مصممة بعناية
صُممت وحداتنا، التي تصل مساحات بعضها إلى 140 مترًا مربعًا، بالتعاون مع مختصين في التصميم الداخلي لتحقيق توازن بين الأناقة الراقية والراحة والمتانة التشغيلية.

### وصول مرن وخصوصية كاملة
يمنح الحجز المباشر وتسجيل الدخول الذاتي الذكي على مدار الساعة ضيوفنا حرية الوصول بمرونة وخصوصية، بعيدًا عن إجراءات الاستقبال التقليدية.

## أرقام نفخر بها
- متوسط تقييم تاريخي 9.5 من 10 على Booking.com.
- متوسط تقييم موثق 4.7 من 5 على Airbnb.
- أكثر من 60 مراجعة من ضيوف الأعمال والعائلات الخليجية.
- نحو 10 دقائق تفصل بعض وحداتنا عن مطار الملك خالد الدولي وواجهة روشن.

## وعدنا لضيوفنا
لا نقدم مكانًا للنوم فحسب؛ بل نوفر بيئة هادئة ومتكاملة للعمل ومساحة فاخرة للاسترخاء العائلي. احجز مباشرة عبر محرك الحجز الآمن للاطلاع على السعر والتوافر المحدثين لوحدتك المختارة.
""".strip(),
        "body_en": """
Premium hospitality enhanced by digital intelligence and hotel engineering.

## Our story
Luxury Smart Apartments was established in the heart of Riyadh for guests who want contemporary luxury without compromising privacy or convenience. We transform short-term accommodation from a traditional stay into a considered smart-living experience for corporate executives, premium GCC families and business travellers.

## The pillars of our experience
### Integrated smart living
Our homes offer a connected residential environment powered by Apple HomeKit and Siri voice control, making it easy to manage lighting, climate and entertainment systems.

### Thoughtful luxury
Our apartments, with selected layouts reaching 140 m², are designed with interior-design specialists to balance refined aesthetics, comfort and dependable day-to-day operation.

### Flexible arrival and complete privacy
Direct booking and 24/7 smart self-check-in give guests the freedom to arrive flexibly and privately, without the friction of a traditional reception desk.

## Numbers we are proud of
- A historic average rating of 9.5 out of 10 on Booking.com.
- A verified average rating of 4.7 out of 5 on Airbnb.
- More than 60 reviews from business guests and premium GCC families.
- Selected apartments are around 10 minutes from King Khalid International Airport and Roshn Front.

## Our promise
We provide more than a place to sleep: a quiet, fully equipped setting for work and a luxurious space for family time. Book directly through our secure booking engine to see current availability and pricing for your chosen apartment.
""".strip(),
        "body_fr": """
Une hospitalité haut de gamme enrichie par l’intelligence numérique et l’ingénierie hôtelière.

## Notre histoire
Luxury Smart Apartments a été créée au cœur de Riyad pour les voyageurs qui recherchent un luxe contemporain sans compromis sur la confidentialité ni la simplicité. Nous transformons la location de courte durée en une expérience résidentielle intelligente destinée aux dirigeants, aux familles du Golfe et aux voyageurs d’affaires.

## Les piliers de notre expérience
### Un habitat intelligent intégré
Nos logements proposent un environnement connecté reposant sur Apple HomeKit et la commande vocale Siri pour gérer facilement l’éclairage, la climatisation et les systèmes de divertissement.

### Un luxe pensé dans les détails
Nos appartements, dont certains atteignent 140 m², sont conçus avec des spécialistes de l’aménagement intérieur afin d’équilibrer esthétique, confort et fiabilité au quotidien.

### Une arrivée flexible et une confidentialité totale
La réservation directe et l’arrivée autonome intelligente, disponible 24 h/24, offrent à nos voyageurs davantage de liberté et de discrétion, sans les contraintes d’une réception traditionnelle.

## Quelques chiffres
- Une note moyenne historique de 9,5 sur 10 sur Booking.com.
- Une note moyenne vérifiée de 4,7 sur 5 sur Airbnb.
- Plus de 60 avis de voyageurs d’affaires et de familles du Golfe.
- Certains appartements se trouvent à environ 10 minutes de l’aéroport international King Khalid et de Roshn Front.

## Notre promesse
Nous offrons plus qu’un lieu où dormir : un cadre calme et équipé pour travailler, ainsi qu’un espace luxueux pour les moments en famille. Réservez directement sur notre moteur sécurisé pour consulter les disponibilités et tarifs à jour.
""".strip(),
        "last_reviewed_at": date(2026, 5, 28),
    },
    "contact": {
        "title_ar": "اتصل بنا",
        "title_en": "Contact us",
        "title_fr": "Nous contacter",
        "meta_description_ar": (
            "تواصل مع فريق Luxury Smart Apartments للاستفسار عن الإقامة والحجز في الرياض."
        ),
        "meta_description_en": (
            "Contact Luxury Smart Apartments for help with stays and bookings in Riyadh."
        ),
        "meta_description_fr": (
            "Contactez Luxury Smart Apartments pour toute question sur un séjour ou une réservation à Riyad."
        ),
        "body_ar": (
            "ضيفنا العزيز، نتطلع للتواصل معك في أقرب وقت ممكن.\n\n"
            "نراجع كل رسالة بعناية ونتعامل معها بالاهتمام الذي تستحقه. استخدم النموذج "
            "للاستفسار عن وحدة أو حجز قائم أو أي مساعدة تحتاجها أثناء إقامتك."
        ),
        "body_en": (
            "Dear guest, we look forward to connecting with you as soon as possible.\n\n"
            "Every message is reviewed personally and handled with the care and attention it "
            "deserves. Use the form for questions about an apartment, an existing booking or "
            "help during your stay."
        ),
        "body_fr": (
            "Cher voyageur, nous nous réjouissons de pouvoir échanger avec vous rapidement.\n\n"
            "Chaque message est examiné avec soin et reçoit toute l’attention qu’il mérite. "
            "Utilisez le formulaire pour toute question sur un appartement, une réservation "
            "existante ou votre séjour."
        ),
        "last_reviewed_at": date(2026, 6, 5),
    },
    "terms": {
        "title_ar": "الشروط والأحكام",
        "title_en": "Terms and conditions",
        "title_fr": "Conditions générales",
        "meta_description_ar": "الشروط المنظمة لاستخدام موقع Luxury Smart Apartments وخدمات الحجز والإقامة.",
        "meta_description_en": "Terms governing the use of the Luxury Smart Apartments website, bookings and stays.",
        "meta_description_fr": "Conditions régissant l’utilisation du site Luxury Smart Apartments, les réservations et les séjours.",
        "body_ar": """
تحكم هذه الشروط استخدام موقع Luxury Smart Apartments وطلبات الحجز والإقامات التي تتم من خلاله. باستخدام الموقع أو إكمال الحجز، فإنك تقر بقراءة هذه الشروط والموافقة عليها.

## 1. نطاق الخدمة
نعرض معلومات الوحدات والتوافر والأسعار ونوفر وسائل طلب الحجز وإدارته. قد تقدم بعض الخدمات جهات تشغيل أو دفع أو تقنية متعاقدة معنا. لا يُعد تصفح الوحدة أو إنشاء عرض سعر حجزًا مؤكدًا.

## 2. الحجز والتأكيد
- يجب أن تكون بيانات الضيف والتواصل صحيحة وكاملة.
- يظل عرض السعر والتوافر مؤقتين حتى اكتمال الخطوات المعروضة وإصدار تأكيد حجز يحمل مرجعًا واضحًا.
- يحق لنا رفض أو إلغاء طلب يتضمن معلومات غير صحيحة أو نشاطًا احتياليًا أو مخالفة للأنظمة، مع إعادة أي مبلغ مستحق وفق وسيلة الدفع والسياسة المطبقة.

## 3. الأسعار والدفع
يظهر السعر والعملة والضرائب والرسوم المعروفة قبل التأكيد. يتحمل الضيف أي خدمات إضافية يطلبها أثناء الإقامة. عند تفعيل الدفع الإلكتروني، تتم معالجة بيانات البطاقة لدى مزود دفع معتمد ولا نخزن رقم البطاقة الكامل أو رمز الحماية.

## 4. الوصول والإقامة
تُرسل تعليمات الوصول بعد استيفاء متطلبات الحجز والتحقق. يلتزم الضيف بأوقات الدخول والخروج المحددة، والحد الأقصى للضيوف، وتعليمات السلامة والهدوء، وعدم استخدام الوحدة في نشاط غير نظامي أو تجاري غير مصرح به.

## 5. المحافظة على الوحدة
يتحمل صاحب الحجز مسؤولية سلوك مرافقيه والعناية بالوحدة ومحتوياتها. قد تُحمّل عليه تكلفة التلف أو الفقد أو التنظيف الاستثنائي المثبت بما يتوافق مع الأنظمة وشروط الحجز.

## 6. التعديل والإلغاء
تخضع التعديلات والإلغاءات لسياسة الإلغاء والسعر المحدد في تأكيد الحجز. إرسال الطلب لا يعني قبوله أو استحقاق الاسترداد حتى تتم مراجعته وتأكيده.

## 7. المحتوى والتوافر
نبذل عناية معقولة للحفاظ على دقة الصور والأوصاف، وقد تختلف تفاصيل بسيطة بسبب الصيانة أو تحديث الأثاث. عند تعذر توفير الوحدة المؤكدة لسبب تشغيلي جوهري، سنتواصل مع الضيف لعرض حل مناسب أو معالجة المبلغ المستحق.

## 8. المسؤولية والظروف الخارجة عن السيطرة
لا نكون مسؤولين عن تأخير أو تعذر ناتج عن أحداث خارجة بصورة معقولة عن سيطرتنا. لا تحد هذه الشروط أي حق لا يجوز استبعاده بموجب الأنظمة السارية في المملكة العربية السعودية.

## 9. التواصل والتغييرات
يمكننا تحديث هذه الشروط عند تغير الخدمة أو المتطلبات النظامية. تسري النسخة المعروضة وقت الحجز على ذلك الحجز. للاستفسارات، استخدم صفحة اتصل بنا أو البريد المنشور فيها.
""".strip(),
        "body_en": """
These terms govern use of the Luxury Smart Apartments website, booking requests and stays arranged through it. By using the website or completing a booking, you acknowledge that you have read and accepted these terms.

## 1. Scope of the service
We publish apartment information, availability and prices and provide ways to request and manage a booking. Some operational, payment or technology services may be delivered by contracted providers. Browsing an apartment or generating a quote does not create a confirmed booking.

## 2. Booking and confirmation
- Guest and contact information must be accurate and complete.
- Prices and availability remain provisional until the displayed steps are completed and a booking confirmation with a clear reference is issued.
- We may reject or cancel a request involving inaccurate information, fraud or unlawful activity, with any amount due handled under the applicable policy and payment method.

## 3. Prices and payment
The price, currency, known taxes and fees are shown before confirmation. Guests are responsible for additional services requested during a stay. When online payment is enabled, card information is processed by an approved payment provider; we do not store the full card number or security code.

## 4. Access and conduct
Arrival instructions are shared after booking and verification requirements are met. Guests must follow the stated check-in and check-out times, occupancy limits, safety and quiet rules, and must not use an apartment for unlawful or unauthorised commercial activity.

## 5. Care of the apartment
The booking holder is responsible for accompanying guests and for taking reasonable care of the apartment and its contents. Documented damage, loss or exceptional cleaning may be charged in accordance with applicable law and the confirmed booking terms.

## 6. Changes and cancellations
Changes and cancellations are governed by the cancellation and rate terms shown in the booking confirmation. Sending a request does not mean it has been approved or that a refund is due until it is reviewed and confirmed.

## 7. Content and availability
We take reasonable care to keep photographs and descriptions accurate, although minor details may change through maintenance or furnishing updates. If a confirmed apartment becomes unavailable for a material operational reason, we will contact the guest to offer an appropriate solution or process any amount due.

## 8. Liability and events outside our control
We are not responsible for delay or failure caused by events reasonably outside our control. Nothing in these terms excludes a right that cannot lawfully be excluded under the laws of the Kingdom of Saudi Arabia.

## 9. Contact and changes
We may update these terms when the service or legal requirements change. The version presented when a booking is made governs that booking. Questions can be sent through our contact page or its published email address.
""".strip(),
        "body_fr": """
Les présentes conditions régissent l’utilisation du site Luxury Smart Apartments, les demandes de réservation et les séjours organisés par son intermédiaire. En utilisant le site ou en finalisant une réservation, vous reconnaissez les avoir lues et acceptées.

## 1. Étendue du service
Nous publions les informations, disponibilités et tarifs des appartements et proposons des moyens de demander et de gérer une réservation. Certains services opérationnels, techniques ou de paiement peuvent être fournis par des prestataires contractuels. La consultation d’un logement ou l’obtention d’un devis ne constitue pas une réservation confirmée.

## 2. Réservation et confirmation
- Les informations du voyageur et ses coordonnées doivent être exactes et complètes.
- Le prix et la disponibilité restent provisoires jusqu’à l’achèvement des étapes affichées et l’émission d’une confirmation comportant une référence claire.
- Nous pouvons refuser ou annuler une demande comportant des informations inexactes, une fraude ou une activité illicite, les sommes dues étant traitées conformément à la politique et au moyen de paiement applicables.

## 3. Prix et paiement
Le prix, la devise, les taxes et frais connus sont indiqués avant confirmation. Le voyageur assume les services supplémentaires demandés pendant le séjour. Lorsque le paiement en ligne est activé, les données de carte sont traitées par un prestataire agréé ; nous ne conservons ni le numéro complet ni le cryptogramme.

## 4. Accès et comportement
Les instructions d’arrivée sont communiquées après satisfaction des exigences de réservation et de vérification. Les voyageurs doivent respecter les horaires, la capacité maximale, les règles de sécurité et de tranquillité, et ne pas utiliser le logement à des fins illicites ou commerciales non autorisées.

## 5. Respect du logement
Le titulaire de la réservation répond du comportement des personnes qui l’accompagnent et doit prendre soin du logement et de son contenu. Les dommages, pertes ou nettoyages exceptionnels documentés peuvent être facturés conformément à la loi et aux conditions confirmées.

## 6. Modifications et annulations
Les modifications et annulations sont soumises à la politique et au tarif figurant dans la confirmation. L’envoi d’une demande ne signifie pas qu’elle est acceptée ni qu’un remboursement est dû avant examen et confirmation.

## 7. Contenu et disponibilité
Nous veillons raisonnablement à l’exactitude des photos et descriptions, même si certains détails peuvent évoluer à la suite d’un entretien ou d’un renouvellement du mobilier. Si un logement confirmé devient indisponible pour une raison opérationnelle majeure, nous contacterons le voyageur afin de proposer une solution adaptée ou de traiter les sommes dues.

## 8. Responsabilité et événements hors de notre contrôle
Nous ne sommes pas responsables d’un retard ou d’une impossibilité résultant d’un événement raisonnablement hors de notre contrôle. Les présentes conditions n’excluent aucun droit qui ne peut l’être en vertu des lois du Royaume d’Arabie saoudite.

## 9. Contact et mises à jour
Nous pouvons modifier ces conditions lorsque le service ou la réglementation évolue. La version présentée au moment de la réservation s’applique à celle-ci. Toute question peut être adressée via la page de contact ou l’adresse e-mail qui y figure.
""".strip(),
        "last_reviewed_at": date(2026, 8, 1),
    },
    "privacy": {
        "title_ar": "سياسة الخصوصية",
        "title_en": "Privacy policy",
        "title_fr": "Politique de confidentialité",
        "meta_description_ar": "كيف تجمع Luxury Smart Apartments البيانات الشخصية وتستخدمها وتحميها عند التواصل والحجز.",
        "meta_description_en": "How Luxury Smart Apartments collects, uses and protects personal data during enquiries and bookings.",
        "meta_description_fr": "Comment Luxury Smart Apartments collecte, utilise et protège les données personnelles lors des demandes et réservations.",
        "body_ar": """
توضح هذه السياسة كيفية تعامل Luxury Smart Apartments مع بياناتك الشخصية عند زيارة الموقع أو التواصل معنا أو طلب حجز. نلتزم بمبادئ نظام حماية البيانات الشخصية في المملكة العربية السعودية والأنظمة السارية ذات الصلة.

## 1. البيانات التي نجمعها
- بيانات الهوية والتواصل مثل الاسم والبريد والهاتف.
- بيانات الإقامة والحجز مثل الوحدة والتواريخ وعدد الضيوف والطلبات الخاصة.
- الرسائل وسجل خدمة الضيف والتعديلات أو الإلغاءات.
- بيانات تقنية مثل عنوان الشبكة ونوع المتصفح وسجلات الأمان وملفات الارتباط، وبيانات التحليلات أو التسويق عند الحصول على الموافقة المطلوبة.
- سجلات المعاملات وحالة الدفع. لا نخزن رقم البطاقة الكامل أو رمز الحماية.

## 2. أغراض الاستخدام
نستخدم البيانات للرد على الاستفسارات، والتحقق من التوافر والسعر، وتنفيذ الحجز وإدارته، وإرسال التعليمات والإشعارات، وحماية الموقع ومنع الاحتيال، والامتثال للالتزامات النظامية، وتحسين الخدمة بموافقتك عندما تكون مطلوبة.

## 3. الأساس النظامي
نعالج البيانات لتنفيذ طلبك أو العقد معك، والوفاء بالتزام نظامي، وحماية المصالح المشروعة مثل أمن الخدمة ومنع إساءة الاستخدام، أو بناءً على موافقتك للخيارات غير الأساسية. يمكنك سحب الموافقة المستقبلية من إعدادات ملفات الارتباط متى كانت المعالجة قائمة عليها.

## 4. مشاركة البيانات
قد نشارك الحد الأدنى اللازم مع منصة إدارة الحجوزات، ومزودي الدفع عند تفعيلهم، وخدمات الاستضافة والبريد والدعم والتحليلات المعتمدة، وملاك أو مشغلي الوحدات المعنيين، والجهات الرسمية عند وجود طلب نظامي. لا نبيع بياناتك الشخصية.

## 5. النقل خارج المملكة
قد يعالج بعض مزودي التقنية البيانات خارج المملكة. نستخدم الضمانات والتعاقدات المطلوبة نظامًا ونقيّد النقل بالقدر اللازم لتقديم الخدمة.

## 6. مدة الاحتفاظ
نحتفظ برسائل التواصل عادة لمدة لا تتجاوز 12 شهرًا، وبسجلات الحجز والمعاملات للمدة اللازمة للتشغيل أو المطالبات أو المتطلبات النظامية. نحذف البيانات أو نخفي هويتها عند انتهاء الحاجة، ما لم يلزم الاحتفاظ بها مدة أطول.

## 7. حقوقك
بحسب النظام المطبق، يمكنك طلب العلم ببياناتك والوصول إليها وتصحيحها أو إتلافها، والحصول عليها بصيغة مقروءة، وسحب الموافقة أو الاعتراض حيث ينطبق. قد نطلب التحقق من الهوية قبل تنفيذ الطلب.

## 8. الأمان وملفات الارتباط
نطبق ضوابط تقنية وتنظيمية مناسبة، إلا أنه لا توجد وسيلة نقل أو تخزين تضمن الأمان المطلق. يستخدم الموقع ملفات أساسية للجلسة والأمان واللغة، ولا تعمل أدوات التحليل أو التسويق غير الأساسية إلا وفق إعدادات الموافقة المعروضة لك.

## 9. الأطفال والتحديثات
الخدمة موجهة للبالغين القادرين على إجراء الحجز، ولا نجمع عمدًا بيانات الأطفال مباشرة إلا ما يقدمه ولي الأمر للمتطلبات المشروعة للإقامة. قد نحدّث هذه السياسة، ويظهر تاريخ آخر تحديث في أعلى الصفحة.

## 10. التواصل
لطلب متعلق بالخصوصية، استخدم صفحة اتصل بنا أو راسل البريد الإلكتروني المنشور فيها، مع كتابة «طلب خصوصية» في عنوان الرسالة.
""".strip(),
        "body_en": """
This policy explains how Luxury Smart Apartments handles personal data when you visit the website, contact us or request a booking. We follow the principles of Saudi Arabia’s Personal Data Protection Law and other applicable requirements.

## 1. Data we collect
- Identity and contact data such as your name, email address and telephone number.
- Stay and booking data such as the apartment, dates, guest count and special requests.
- Messages, guest-support history, changes and cancellation requests.
- Technical data such as network address, browser type, security logs and cookies, plus analytics or marketing data when the required consent is given.
- Transaction records and payment status. We do not store full card numbers or security codes.

## 2. How we use data
We use data to answer enquiries, verify price and availability, arrange and manage bookings, send instructions and notices, secure the website and prevent fraud, meet legal obligations and improve the service with consent where required.

## 3. Legal basis
We process data to take steps at your request or perform a contract, comply with law, protect legitimate interests such as service security and misuse prevention, or on the basis of consent for non-essential choices. Where processing relies on consent, you can withdraw it for the future through the cookie settings.

## 4. Data sharing
We may share the minimum necessary data with our booking-management platform, payment providers when enabled, approved hosting, email, support and analytics suppliers, relevant apartment owners or operators, and public authorities where legally required. We do not sell personal data.

## 5. International transfers
Some technology providers may process data outside Saudi Arabia. We use the safeguards and contracts required by law and limit transfers to what is necessary to provide the service.

## 6. Retention
Contact messages are generally kept for no longer than 12 months. Booking and transaction records are retained for operational, claims and legal periods. We delete or anonymise data when it is no longer needed unless a longer period is required.

## 7. Your rights
Subject to applicable law, you may ask to be informed about, access, correct or destroy your data, obtain it in a readable form, withdraw consent or object where relevant. We may verify identity before completing a request.

## 8. Security and cookies
We use appropriate technical and organisational controls, but no transmission or storage method is guaranteed to be completely secure. Essential cookies support sessions, security and language. Non-essential analytics or marketing tools operate only in line with the consent choices shown to you.

## 9. Children and updates
The service is intended for adults able to make a booking. We do not knowingly collect data directly from children except information supplied by a guardian for legitimate stay requirements. We may update this policy; the latest review date appears at the top of the page.

## 10. Contact
For a privacy request, use our contact page or the email address published there and include “Privacy request” in the subject line.
""".strip(),
        "body_fr": """
Cette politique explique comment Luxury Smart Apartments traite les données personnelles lorsque vous consultez le site, nous contactez ou demandez une réservation. Nous respectons les principes de la loi saoudienne sur la protection des données personnelles et les exigences applicables.

## 1. Données collectées
- Données d’identité et de contact : nom, adresse e-mail et téléphone.
- Données de séjour et de réservation : appartement, dates, nombre de voyageurs et demandes particulières.
- Messages, historique d’assistance, demandes de modification ou d’annulation.
- Données techniques : adresse réseau, navigateur, journaux de sécurité et cookies, ainsi que données d’analyse ou de marketing lorsque le consentement requis est donné.
- Enregistrements de transaction et état du paiement. Nous ne conservons ni le numéro complet de la carte ni son cryptogramme.

## 2. Utilisation des données
Nous utilisons les données pour répondre aux demandes, vérifier les prix et disponibilités, organiser et gérer les réservations, envoyer des instructions, sécuriser le site, prévenir la fraude, respecter nos obligations et améliorer le service avec votre consentement lorsque celui-ci est requis.

## 3. Base juridique
Le traitement est nécessaire pour répondre à votre demande ou exécuter un contrat, respecter la loi, protéger des intérêts légitimes tels que la sécurité du service, ou repose sur votre consentement pour les choix non essentiels. Vous pouvez retirer ce consentement pour l’avenir dans les paramètres des cookies.

## 4. Partage
Nous pouvons communiquer le minimum nécessaire à notre plateforme de gestion des réservations, aux prestataires de paiement lorsqu’ils sont activés, aux fournisseurs agréés d’hébergement, de messagerie, d’assistance et d’analyse, aux propriétaires ou opérateurs concernés, ainsi qu’aux autorités lorsque la loi l’exige. Nous ne vendons pas vos données.

## 5. Transferts internationaux
Certains fournisseurs techniques peuvent traiter des données hors d’Arabie saoudite. Nous utilisons les garanties et contrats requis et limitons les transferts à ce qui est nécessaire au service.

## 6. Conservation
Les messages de contact sont généralement conservés au maximum 12 mois. Les dossiers de réservation et de transaction sont gardés pendant les durées nécessaires à l’exploitation, aux réclamations et aux obligations légales. Les données sont ensuite supprimées ou anonymisées, sauf obligation contraire.

## 7. Vos droits
Sous réserve de la loi applicable, vous pouvez demander à être informé, accéder à vos données, les corriger ou les faire détruire, les obtenir sous une forme lisible, retirer votre consentement ou vous opposer au traitement lorsque cela s’applique. Une vérification d’identité peut être demandée.

## 8. Sécurité et cookies
Nous appliquons des mesures techniques et organisationnelles adaptées, sans qu’aucune transmission ou conservation ne puisse être garantie totalement sûre. Les cookies essentiels servent aux sessions, à la sécurité et à la langue. Les outils non essentiels d’analyse ou de marketing respectent les choix de consentement affichés.

## 9. Enfants et mises à jour
Le service est destiné aux adultes en mesure de réserver. Nous ne recueillons pas sciemment de données directement auprès d’enfants, sauf celles fournies par un représentant légal pour les besoins légitimes du séjour. La date de dernière révision figure en haut de la page.

## 10. Contact
Pour une demande relative à la vie privée, utilisez la page de contact ou l’adresse e-mail qui y est publiée et indiquez « Demande de confidentialité » dans l’objet.
""".strip(),
        "last_reviewed_at": date(2026, 8, 1),
    },
    "cancellation": {
        "title_ar": "سياسة الإلغاء",
        "title_en": "Cancellation policy",
        "title_fr": "Politique d’annulation",
        "meta_description_ar": "آلية طلب إلغاء أو تعديل حجز لدى Luxury Smart Apartments وأهلية الاسترداد.",
        "meta_description_en": "How to request a booking cancellation or change with Luxury Smart Apartments and how refunds are assessed.",
        "meta_description_fr": "Comment demander l’annulation ou la modification d’une réservation Luxury Smart Apartments et comment le remboursement est évalué.",
        "body_ar": """
توضح هذه السياسة طريقة طلب إلغاء أو تعديل الحجز. لأن شروط الأسعار قد تختلف بين الوحدات والفترات والعروض، فإن شروط الإلغاء المعروضة قبل الدفع والمثبتة في تأكيد الحجز هي المرجع الأول لذلك الحجز.

## 1. تقديم طلب الإلغاء
قدّم الطلب من صفحة إدارة الحجز إن كانت متاحة، أو عبر صفحة اتصل بنا باستخدام البريد ورقم الحجز نفسيهما. لا يعد إرسال الطلب إلغاءً نهائيًا؛ يصبح نافذًا بعد مراجعته وإرسال تأكيد الإلغاء.

## 2. الأهلية والرسوم
- تُحدد أهلية الاسترداد وقيمته وفق نوع السعر وموعد الطلب والشروط الواردة في تأكيد الحجز.
- إذا تعارض نص عام في هذه الصفحة مع شرط خاص ظهر بوضوح أثناء الحجز، فيطبق الشرط الخاص ما لم يمنع النظام ذلك.
- قد تكون الخدمات المنفذة فعليًا أو رسوم الجهات الخارجية غير قابلة للاسترداد إذا نص تأكيد الحجز على ذلك وكان متوافقًا مع النظام.

## 3. عدم الحضور والمغادرة المبكرة
يخضع عدم الحضور أو الوصول بعد الموعد أو المغادرة قبل التاريخ المؤكد لشروط السعر المحجوز. تواصل معنا سريعًا عند توقع التأخر حتى نتمكن من المساعدة وحماية الحجز قدر الإمكان.

## 4. تعديل الحجز
تخضع تغييرات التواريخ أو الوحدة أو عدد الضيوف للتوافر وإعادة التسعير. لا يصبح التعديل نافذًا إلا بعد قبول الشروط الجديدة ودفع أي فرق مستحق وإصدار تأكيد محدث.

## 5. الإلغاء من جانبنا
إذا تعذر توفير الوحدة المؤكدة لسبب تشغيلي جوهري، سنتواصل مع الضيف لعرض وحدة بديلة مناسبة أو تغيير التواريخ أو إعادة المبلغ المستحق عن الجزء غير المقدم. لا يؤثر ذلك في الحقوق النظامية للضيف.

## 6. معالجة الاسترداد
بعد اعتماد الاسترداد، يُعاد المبلغ المؤهل عادة إلى وسيلة الدفع الأصلية. يعتمد وقت ظهوره على مزود الدفع والبنك المصدر، وقد يستغرق عدة أيام عمل بعد تنفيذ العملية. سنرسل إشعارًا عند تسجيل الاسترداد من جانبنا.

## 7. الظروف الاستثنائية
نراجع الحالات الموثقة الخارجة عن السيطرة بصورة عادلة ووفق شروط الحجز والأنظمة السارية. يُرجى إرفاق المعلومات الضرورية فقط وتجنب إرسال بيانات بطاقات أو وثائق حساسة عبر نموذج التواصل.

## 8. المساعدة
للحصول على مساعدة، استخدم صفحة اتصل بنا واذكر رقم الحجز وتاريخ الوصول المطلوب. لا ترسل بيانات البطاقة أو كلمات المرور أو رموز الدخول.
""".strip(),
        "body_en": """
This policy explains how to request a booking cancellation or change. Because rate conditions may vary by apartment, period and offer, the cancellation terms displayed before payment and recorded in the booking confirmation are the primary terms for that booking.

## 1. Requesting cancellation
Submit a request through the booking-management page when available, or use our contact page with the same email address and booking reference. Sending a request does not itself cancel the booking; cancellation takes effect after review and a cancellation confirmation is issued.

## 2. Eligibility and charges
- Refund eligibility and value depend on the rate type, request time and terms in the booking confirmation.
- If a general statement on this page conflicts with a specific condition clearly shown during booking, the specific condition applies unless prohibited by law.
- Services already supplied or third-party charges may be non-refundable where the confirmation says so and applicable law permits it.

## 3. No-show and early departure
A no-show, late arrival or departure before the confirmed date is handled under the booked rate terms. Contact us promptly if you expect to arrive late so that we can assist and protect the booking where possible.

## 4. Booking changes
Changes to dates, apartment or guest count are subject to availability and repricing. A change takes effect only after the new terms are accepted, any balance is paid and an updated confirmation is issued.

## 5. Cancellation by us
If a confirmed apartment cannot be provided for a material operational reason, we will contact the guest to offer a suitable alternative, different dates or return the amount due for the part not supplied. This does not affect the guest’s statutory rights.

## 6. Refund processing
Once approved, an eligible refund is normally returned to the original payment method. The time it takes to appear depends on the payment provider and issuing bank and may be several business days after processing. We will notify you when the refund is recorded on our side.

## 7. Exceptional circumstances
We review documented circumstances outside a guest’s control fairly and in line with the confirmed terms and applicable law. Provide only necessary supporting information and do not send card data or sensitive documents through the contact form.

## 8. Help
For help, use our contact page and include the booking reference and intended arrival date. Never send card data, passwords or access codes.
""".strip(),
        "body_fr": """
Cette politique explique comment demander l’annulation ou la modification d’une réservation. Les conditions tarifaires pouvant varier selon le logement, la période et l’offre, les modalités affichées avant le paiement et inscrites dans la confirmation constituent la référence principale.

## 1. Demander une annulation
Effectuez la demande depuis la page de gestion de la réservation lorsqu’elle est disponible, ou via notre page de contact en utilisant la même adresse e-mail et la référence de réservation. L’envoi d’une demande n’annule pas à lui seul la réservation ; l’annulation prend effet après examen et émission d’une confirmation.

## 2. Éligibilité et frais
- L’éligibilité et le montant du remboursement dépendent du type de tarif, du moment de la demande et des conditions de la confirmation.
- Si une indication générale de cette page contredit une condition particulière clairement présentée lors de la réservation, cette dernière s’applique sauf interdiction légale.
- Les services déjà fournis ou certains frais de tiers peuvent ne pas être remboursables lorsque la confirmation le prévoit et que la loi le permet.

## 3. Non-présentation et départ anticipé
Une non-présentation, une arrivée tardive ou un départ avant la date confirmée sont traités selon les conditions du tarif réservé. Contactez-nous rapidement en cas de retard afin que nous puissions vous aider et préserver la réservation si possible.

## 4. Modification
Tout changement de dates, de logement ou de nombre de voyageurs dépend des disponibilités et d’un nouveau calcul du prix. Il ne prend effet qu’après acceptation des nouvelles conditions, paiement du solde éventuel et émission d’une confirmation mise à jour.

## 5. Annulation de notre part
Si un logement confirmé ne peut être fourni pour une raison opérationnelle majeure, nous proposerons une solution adaptée, d’autres dates ou le remboursement des sommes dues pour la partie non fournie. Les droits légaux du voyageur restent inchangés.

## 6. Traitement du remboursement
Une fois approuvé, le remboursement éligible est normalement envoyé vers le moyen de paiement d’origine. Son délai d’apparition dépend du prestataire et de la banque émettrice et peut atteindre plusieurs jours ouvrés. Nous vous informerons lorsque l’opération sera enregistrée de notre côté.

## 7. Circonstances exceptionnelles
Nous examinons équitablement les circonstances documentées hors du contrôle du voyageur, conformément aux conditions confirmées et à la loi. Ne transmettez que les justificatifs nécessaires et jamais de données de carte ou de documents sensibles dans le formulaire.

## 8. Assistance
Utilisez notre page de contact en indiquant la référence et la date d’arrivée prévue. Ne transmettez jamais de données de carte, de mot de passe ou de code d’accès.
""".strip(),
        "last_reviewed_at": date(2026, 8, 1),
    },
}


LEGACY_MARKERS = {
    "about": (
        "هذه الصفحة قابلة للتحديث من لوحة الإدارة",
        "This page can be updated from the administration area",
        "Cette page peut être mise à jour depuis l’espace d’administration",
    ),
    "terms": ("نسخة أولية للمراجعة القانونية", "initial draft for legal review", "version initiale"),
    "privacy": ("تحتاج هذه المسودة إلى مراجعة قانونية", "requires legal review", "révision juridique"),
    "cancellation": ("النهائية لم تعتمد بعد", "has not yet been approved", "n’a pas encore été approuvée"),
}


SITE_SETTINGS = {
    "brand_name_ar": "Luxury Smart Apartments",
    "brand_name_en": "Luxury Smart Apartments",
    "brand_name_fr": "Luxury Smart Apartments",
    "tagline_ar": "إقامات ذكية فاخرة في الرياض",
    "tagline_en": "Luxury smart stays in Riyadh",
    "tagline_fr": "Séjours intelligents haut de gamme à Riyad",
    "contact_email": "saeed@luxurysmartapartments.com",
    "contact_phone": "+966501205651",
    "instagram_url": "https://www.instagram.com/luxury_smart_apartments/",
    "facebook_url": "https://www.facebook.com/Luxury.Home23",
    "public_address_ar": "الرياض، المملكة العربية السعودية",
    "public_address_en": "Riyadh, Saudi Arabia",
    "public_address_fr": "Riyad, Arabie saoudite",
    "footer_text_ar": "اكتشف إقامات راقية وذكية في الرياض، المملكة العربية السعودية.",
    "footer_text_en": "Discover refined smart stays in Riyadh, Saudi Arabia.",
    "footer_text_fr": "Découvrez des séjours raffinés et connectés à Riyad, en Arabie saoudite.",
}


def seed_public_information(apps, schema_editor):
    del schema_editor
    site_page = apps.get_model("core", "SitePage")
    site_setting = apps.get_model("core", "SiteSetting")

    for slug, values in PAGES.items():
        page, created = site_page.objects.get_or_create(
            slug=slug,
            defaults={**values, "is_published": True},
        )
        if created:
            continue

        changed = False
        markers = LEGACY_MARKERS.get(slug, ())
        for field, value in values.items():
            current = getattr(page, field)
            replace_legacy_body = field.startswith("body_") and any(
                marker in current for marker in markers
            )
            if not current or replace_legacy_body:
                setattr(page, field, value)
                changed = True
        if changed:
            page.save()

    setting = site_setting.objects.first()
    if setting is None:
        setting = site_setting(site_name="Luxury Smart Apartments")
    changed = setting.pk is None
    for field, value in SITE_SETTINGS.items():
        if not getattr(setting, field):
            setattr(setting, field, value)
            changed = True
    if changed:
        setting.save()


class Migration(migrations.Migration):
    dependencies = [("core", "0007_seed_french_public_content")]

    operations = [
        migrations.AddField(
            model_name="sitepage",
            name="last_reviewed_at",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(seed_public_information, migrations.RunPython.noop),
    ]
