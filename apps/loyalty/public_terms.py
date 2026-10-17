"""Issued guest-facing terms. Preserve this version; issue a new URL for revisions."""
# Complete translated paragraphs are intentionally kept intact.
# ruff: noqa: E501

import hashlib
import json
from copy import deepcopy

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import get_language, override
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .accrual import (
    IDENTITY_TERMS_REFERENCE,
    IDENTITY_TERMS_URL_VERSION,
    IDENTITY_TERMS_VERSION,
    TERMS_REFERENCE,
    TERMS_URL_VERSION,
    TERMS_VERSION,
)

VERSION = "loyalty-public-20261016-v1"
URL_VERSION = "2026-10-16-v1"
REFERENCE = f"/ar/legal/rewards/{URL_VERSION}/"

# Business identity only, visually checked against the renewed commercial
# certificate supplied through the owner's Drive. Never include personal IDs,
# date of birth, signature, QR payload or the private certificate itself.
OPERATOR = {
    "name": "مؤسسة الحياة الذكية التجارية",
    "registration": "1010792573",
    "unified_number": "7028510308",
}

CONTENT = {
    "ar": {
        "title": "شروط وأحكام برنامج مكافآت ضيوفنا",
        "issued": "الإصدار الأول المنشور — ١٦ أكتوبر ٢٠٢٦",
        "intro": "الانضمام مجاني واختياري. اقرأ الشروط وإشعار الخصوصية قبل الموافقة. التسجيل في الموقع أو توثيق الجوال لا يعني الانضمام تلقائيًا، ولا يمنح نقاطًا بمجرده.",
        "operator": "الجهة الملتزمة بنقاط البرنامج هي مؤسسة الحياة الذكية التجارية، المملكة العربية السعودية، الرياض؛ سجل تجاري ١٠١٠٧٩٢٥٧٣، ورقم موحد ٧٠٢٨٥١٠٣٠٨. العنوان المسجل: ٧٢٨٠ الحرفيين، حي اليرموك، رقم إضافي ٢٤٠٥، الرمز البريدي ١٣٢٤٣. تلتزم المؤسسة بالنقاط لجميع العقارات المشاركة، بما فيها عقارات المغرب؛ ولا يحل ذلك محل هوية مؤجر العقار أو شروط إقامته المبينة في تأكيد الحجز.",
        "sections": [
            (
                "١. العضوية والحساب",
                "يجوز لصاحب حساب ضيف نشط وموثق البريد، ممن تتوفر لديه أهلية التعاقد، الانضمام بموافقة صريحة. لا يشترط حجز سابق للانضمام أو لتوثيق الجوال أو لاستلام هدية نقاط من عضو آخر. تُحفظ نسخة الشروط ولغتها وتاريخ الموافقة. البريد وجوجل وأبل خيارات دخول قائمة؛ ويمكن ربط جوال للدخول لاحقًا برمز واتساب بعد إثبات ملكيته. حفظ رقم التواصل وحده ليس توثيقًا.",
            ),
            (
                "٢. الحجوزات المؤهلة والاستحقاق",
                "تُكتسب مكافآت الإقامة عن حجوزات الموقع المؤهلة، بما فيها الحجوزات التي تنشئها الإدارة من خلاله وتثبت مدفوعاتها. لا تمنح حجوزات القنوات الخارجية نقاط إقامة، ولا تمنح الحجوزات السابقة لبدء تشغيل البرنامج مكافآت بأثر رجعي. يُشترط الانضمام وتوثيق الوصول الفعلي للعقار وقبض قيمة الإقامة؛ فتضاف النقاط أثناء الإقامة دون انتظار نهايتها. لا يكفي تسجيل الدخول للحساب أو موعد الوصول المجدول. الحجوزات والاستثناءات الاختبارية المحددة سابقًا لا تمنح الآخرين استثناءً.",
            ),
            (
                "٣. أساس الاحتساب وقيمة النقاط",
                "الأساس المؤهل هو قيمة الإقامة المدفوعة فعليًا بغير النقاط بعد الخصومات والاستردادات، دون الضرائب والخدمات والرسوم المنفصلة إن وُجدت. لا يولد الجزء المسدد بالنقاط مكافآت جديدة. تعادل النقطة ريالًا سعوديًا واحدًا عند استخدامها في حجز يدعم تسوية النقاط. لا تباع النقاط ولا تشحن نقدًا ولا تسترد المكافآت نقدًا. إذا اختلفت عملة الحجز، تُبين العملة وقيمة التسوية المدعومة قبل التأكيد، وتستخدم بيانات التحويل الموثقة، لا سعر صرف مفترض.",
            ),
            (
                "٤. نسب المكافآت وسقفها",
                "يحصل صاحب الإقامة على ٣٪ من الأساس المؤهل، وداعيه المباشر على ٥٪. للداعين الأسبق حصة لا تتجاوز ٧٪ إجمالًا و١٪ للشخص: إذا كانوا من واحد إلى سبعة استحق كل منهم ١٪؛ وإذا زادوا على سبعة وزعت حصة ٧٪ بالتساوي بينهم. لا تنشأ حصة لعضو غير موجود، ولا تتجاوز المكافآت الإجمالية ١٥٪. تحتسب كل مكافأة على الحجز نفسه، ولا تضرب نسبتها ثانية في عدد المدعوين. البرنامج ليس استثمارًا أو وعدًا بدخل أو مكافأة لمجرد التجنيد؛ لا رسوم انضمام ولا شراء مطلوب للتسجيل.",
            ),
            (
                "٥. أمثلة والكسور",
                "إقامة مؤهلة بقيمة ٧٥٠ ريالًا تمنح صاحبها ٢٢٫٥٠ نقطة وداعيه المباشر ٣٧٫٥٠ نقطة؛ وإقامة بقيمة ٩٥٠ ريالًا تمنح صاحبها ٢٨٫٥٠ نقطة وداعيه ٤٧٫٥٠ نقطة. إذا سدد حجز ٦٠٠ ريال باستخدام ٢٠٠ نقطة و٤٠٠ ريال بوسيلة أخرى، فمكافأة صاحبه ١٢ نقطة. تسجل النقاط بخانتين عشريتين مع إسقاط ما دون جزء المئة في المكافآت الفردية. عند توزيع ٥٢٫٥٠ نقطة على ثمانية أسبق، يأخذ شخصان ٦٫٥٧ وستة ٦٫٥٦؛ يحدد ترتيب ثابت محفوظ مستحقي الأجزاء الباقية دون تجاوز الإجمالي أو السقوف.",
            ),
            (
                "٦. الدعوة وعلاقة الأعضاء",
                "يتاح إصدار رابط الدعوة بعد أول إقامة مؤهلة مكتملة ومدفوعة. يظهر الداعي ليؤكده المدعو عند الانضمام؛ ثم تحفظ العلاقة للإقامات المؤهلة المتكررة، ولا يغيرها فتح رابط آخر أو إهداء نقاط. يجوز للداعي دعوة عدة أشخاص، ولكل مدعو داعٍ مباشر واحد. تمنع الدعوة الذاتية والعلاقات الدورية والحسابات الوهمية. تعالج الإدارة خطأ الربط الموثق بإجراء مدقق، دون محو تاريخ الاستحقاقات السابقة. لا تعرض شجرة الأعضاء الكاملة أو بيانات إقامات الآخرين للضيف.",
            ),
            (
                "٧. صلاحية النقاط واستخدامها",
                "لا تنتهي النقاط لمجرد مرور الوقت. يمكن استخدامها جزئيًا أو لتغطية الحجز المؤهل بالكامل عند كفاية الرصيد وتوفر الوحدة. يعرض الموقع النقاط والمبلغ المتبقي قبل التأكيد. قد تحجز النقاط مؤقتًا لمحاولة حجز لمنع استعمالها مرتين، وتحرر عند فشل موثوق. إذا لم تتأكد نتيجة الحجز، تراجع المحاولة قبل إنشاء بديل أو تكرار الخصم. لا تغير النقاط السعر المتعاقد عليه ولا تضمن توفر وحدة.",
            ),
            (
                "٨. إهداء النقاط والدعوات عبر واتساب",
                "يمكن إهداء الرصيد المتاح كله أو بعضه لعضو نشط، مع التحقق من جواله الموثق أو رقم عضويته وعرض اسم مختصر قبل التأكيد. ينفذ الخصم والإضافة معًا، ويظهر للطرفين سجل العملية؛ ولا يحتاج المستلم للاتصال وقت التنفيذ. إذا لم يكن الرقم مؤهلًا، يمكن للمرسل اختيار إرسال دعوة متوقعة وموافق عليها عبر رقم الأعمال وبلغة المستلم المعروفة أو اللغة المختارة. لا تنضم العضوية ولا تحجز أو تحول نقاط لمجرد إرسال الدعوة، ولا يثبت قبول خدمة الإرسال وصول الرسالة أو قراءتها.",
            ),
            (
                "٩. الإلغاء قبل الوصول",
                "للحجز المشمول بسياسة الولاء الموضحة في تأكيده، يسترد العضو قبل بدء الإقامة الفعلية كامل ما سدده، مهما كان السبب أو قرب موعد الوصول: تعاد النقاط إلى حسابه ويرد الدفع الآخر إلى مصدره الأصلي، دون اشتراط إعادة تأجير العقار. تلغى المكافآت الناتجة عن الحجز الملغى، ولا تخلط بالنقاط السابقة المستخدمة للدفع. لا يتحول الجزء المسدد بالنقاط إلى استرداد نقدي، ولا يرد أكثر مما دفع. الانضمام اللاحق لا يعيد توصيف حجز خارج نطاق البرنامج.",
            ),
            (
                "١٠. المغادرة المبكرة والاسترداد المختلط",
                "بعد بدء الإقامة تخصم الليالي المستهلكة وترد الليالي المتبقية. عند الإخلاء الفعلي قبل الثامنة مساءً بالتوقيت المحلي للعقار، ترد ليلة تبدأ في تاريخ المغادرة وما بعدها ضمن الحجز؛ وعند الثامنة تمامًا أو بعدها تحسب ليلة ذلك التاريخ. مثال: وصول الاثنين ومغادرة الأربعاء السابعة مساءً يحتسب ليلتي الاثنين والثلاثاء. هذه قاعدة تسوية وليست إذنًا بتمديد وقت الخروج المعتاد. في الدفع المختلط يوزع الاسترداد بنسبة مساهمة النقاط والدفع الآخر: من حجز ١٨٠٠ ريال، ٦٠٠ نقطة و١٢٠٠ بالبطاقة، يكون استرداد ١٢٠٠ موزعًا إلى ٤٠٠ نقطة و٨٠٠ للبطاقة. تعرض حالة الاسترداد ومرجعه؛ وقد يختلف وقت ظهوره بحسب مزود الدفع والبنك.",
            ),
            (
                "١١. تعديل الإقامة وتصحيح النقاط",
                "عند زيادة الحجز أو تخفيضه أو استرداده، يعاد احتساب مكافآت المستفيدين على المبلغ المؤهل الصحيح. يسجل الفرق بحركة جديدة مرتبطة بالأصل، دون حذف الحركة السابقة أو تغيير نسبتها التاريخية. إذا انخفضت إقامة من ٧٥٠ إلى ٦٠٠ ريال، تصحح مكافأة صاحبها من ٢٢٫٥٠ إلى ١٨ نقطة. إذا لم يكف رصيده لتصحيح الفرق بسبب الصرف أو الإهداء، يعوض النقص من مكافآته القادمة دون إظهار رصيد متاح سالب؛ لا تخصم بطاقته ولا يسترد تلقائيًا من مستلم هديته أو من تحويل وارد جديد.",
            ),
            (
                "١٢. كشف الحساب والأمان",
                "يظهر للعضو كشف نقاطه ودائنه ومدينه ورصيده بعد كل عملية ومراجعها المتاحة له. رصيد العضو يختلف عن إجمالي رصيد البرنامج، وتحويل النقاط لا يزيد إجمالي النقاط. يحافظ العضو على حسابه ولا يشارك رموز الدخول، ويبلغ عن الخطأ أو الوصول غير المأذون. تراجع الاعتراضات بأدلتها، ولا يمنع ظهور حركة في السجل من تصحيح خطأ مثبت أو ممارسة الحقوق المقررة.",
            ),
            (
                "١٣. إلغاء العضوية والتغييرات",
                "عند طلب إلغاء العضوية نهائيًا، يبين للعضو رصيده الذي سيسقط ويطلب تأكيده الصريح؛ تسقط نقاطه المتبقية عند إتمام الإلغاء. تسوى الحجوزات والحقوق القائمة قبل الإقفال، ولا يلغى حجز أو تصادر دفعة بطاقة تلقائيًا. إيقاف التسويق أو عدم استخدام الحساب ليس إلغاءً للعضوية ولا يسقط النقاط. يعلن أي إصدار جديد وتاريخ نفاذه، ولا تغير نسب العمليات السابقة بأثر رجعي. إيقاف البرنامج من المشغّل لا يعد انسحابًا اختياريًا من الأعضاء، وتبقى معالجة الحقوق والنقاط المكتسبة واجبة وفق التسوية المعلنة والأنظمة المنطبقة.",
            ),
            (
                "١٤. الخصوصية والتواصل والحقوق",
                "تستخدم البيانات اللازمة لإدارة الحساب والنقاط والحجوزات وعلاقة الدعوة والتحويل والتدقيق والحماية، بحسب إشعار الخصوصية المرتبط أدناه. لا تعرض بيانات إقامة شخص آخر لمجرد استحقاق مكافأة عنه. موافقة العضوية منفصلة عن التسويق؛ يمكن إيقاف الرسائل الترويجية دون خسارة العضوية أو النقاط، مع بقاء الإشعارات الضرورية للخدمة. للاستفسار أو الاعتراض أو طلبات الخصوصية استخدم صفحة تواصل معنا. تسري تعليمات العقار وشروط إقامته والحقوق الآمرة بحسب بلد العقار؛ لا تمنع هذه الشروط الشكوى للجهة المختصة ولا تنتقص حقًا لا يجوز التنازل عنه. تتاح هذه النسخة بالعربية والإنجليزية والفرنسية وتحفظ لغة الموافقة. وجود سجل تجاري أو نشر هذه الشروط لا يعني اعتماد البرنامج أو تصنيفه من جهة تنظيمية.",
            ),
        ],
        "contact": "التواصل والشكاوى",
        "privacy": "إشعار الخصوصية",
        "account": "حجوزاتي ومكافآتي",
    },
    "en": {
        "title": "Guest rewards programme terms and conditions",
        "issued": "First published edition — 16 October 2026",
        "intro": "Joining is free and optional. Read these terms and the privacy notice before consenting. Creating an account or verifying a mobile does not automatically enrol you or earn points.",
        "operator": "The entity responsible for programme points is مؤسسة الحياة الذكية التجارية (Smart Life Trading Establishment), Riyadh, Saudi Arabia, commercial registration 1010792573, unified number 7028510308. Registered address: 7280 Al-Harfiyyin, Al-Yarmouk, additional number 2405, postal code 13243. The establishment is responsible for points for all participating properties, including properties in Morocco. This does not replace the accommodation provider's identity or stay conditions shown in the booking confirmation.",
        "sections": [
            (
                "1. Membership and account",
                "An active guest account with a verified email, held by a person legally able to contract, may join by explicit consent. No previous booking is required to join, verify a mobile or receive gifted points. We record the terms version, consent language and time. Email, Google and Apple remain sign-in options. A mobile can be linked for subsequent WhatsApp-code sign-in after ownership verification; saving a contact number alone does not verify it.",
            ),
            (
                "2. Eligible bookings and accrual",
                "Stay rewards apply to eligible website bookings, including bookings created by site administration with evidenced payment. External-channel bookings do not earn stay points, and bookings predating programme commencement do not receive retrospective rewards. Membership, verified physical arrival at the property and receipt of eligible payment are required. Points are added during the stay, without waiting for checkout; account sign-in or a scheduled arrival date is insufficient. Previously designated acceptance-test exceptions do not give other guests an exception.",
            ),
            (
                "3. Eligible amount and points value",
                "The eligible amount is accommodation actually paid for other than with points, after discounts and refunds, excluding taxes, services and separately stated fees if any. Points-funded amounts earn no further rewards. One point represents one Saudi riyal when used for a booking supporting points settlement. Points are not sold, cash-loaded or redeemable for cash. For a different booking currency, supported settlement values and currency are shown before confirmation using documented conversion information, never an assumed exchange rate.",
            ),
            (
                "4. Reward rates and ceiling",
                "The staying member earns 3% of the eligible amount; the direct inviter earns 5%. Earlier inviters share no more than 7% in total, capped at 1% each: one to seven earlier inviters receive 1% each; more than seven share 7% equally. No share is created for a person absent from the chain, and aggregate rewards never exceed 15%. Each reward uses the individual booking amount; its percentage is not multiplied again by invitee count. This is not an investment, income promise or reward merely for recruitment; joining is free and no purchase is required to register.",
            ),
            (
                "5. Examples and decimal handling",
                "Eligible accommodation of SAR 750 earns 22.50 points for its guest and 37.50 for the direct inviter; SAR 950 earns 28.50 and 47.50 respectively. A SAR 600 booking funded by 200 points and SAR 400 otherwise earns its guest 12 points. Points use two decimal places; smaller fractions of individual rewards are discarded. For 52.50 points shared among eight earlier inviters, two receive 6.57 and six receive 6.56. A recorded fixed order assigns remaining hundredths without exceeding totals or caps.",
            ),
            (
                "6. Invitations and attribution",
                "An invitation link becomes available after the first completed, paid, eligible stay. The invitee confirms the displayed inviter when joining; attribution then applies to repeated eligible stays and is not changed by another link or a gift. An inviter may invite multiple people, but an invitee has one direct inviter. Self-invitations, cycles and fictitious accounts are prohibited. Administration may correct an evidenced missing link through an audited process without erasing historical accrual records. Guests cannot view the complete member tree or another guest's stay details.",
            ),
            (
                "7. Validity and redemption",
                "Points do not expire merely with time. They may cover part or all of an eligible booking, subject to sufficient balance and availability. Points and remaining payment are shown before confirmation. Points may be temporarily held for a booking attempt to prevent duplicate use and released following a confirmed failure. An uncertain booking outcome is checked before another booking or debit. Points neither alter an agreed price nor guarantee availability.",
            ),
            (
                "8. Gifts and WhatsApp invitations",
                "Available points may be gifted wholly or partly to an active member after checking their verified mobile or membership number and showing a shortened name for confirmation. Debit and credit are completed together and recorded for both members; the recipient need not be online. If the number is not eligible, the sender may choose an expected, consented invitation through our business WhatsApp number, in the recipient's known language or the selected language. Sending an invitation neither enrols its recipient nor reserves or transfers points. Provider acceptance does not prove delivery or reading.",
            ),
            (
                "9. Cancellation before physical arrival",
                "For a booking covered by the loyalty cancellation policy shown in its confirmation, a member cancelling before the stay physically starts receives everything paid back, regardless of reason or proximity to arrival: points return to their account and other payment returns to its original source, without requiring the property to be relet. Rewards generated by the cancelled booking are reversed separately from earlier points used to pay. Points-funded amounts are not refunded as cash, and refunds never exceed payment. Later enrolment does not reclassify an out-of-scope booking.",
            ),
            (
                "10. Early departure and mixed-payment refunds",
                "After a stay starts, consumed nights are charged and remaining nights refunded. Actual vacancy before 8 p.m. in the property's local time refunds the night starting on the departure date and later booked nights; at exactly 8 p.m. or later that night's charge applies. Monday arrival and Wednesday departure at 7 p.m. charges Monday and Tuesday nights. This is a settlement rule, not permission to extend ordinary checkout. A mixed refund follows original funding proportions: for SAR 1,800 paid as 600 points and SAR 1,200 by card, a SAR 1,200 refund returns 400 points and SAR 800 to the card. Refund status and reference are provided; appearance in the account depends on provider and bank processing.",
            ),
            (
                "11. Booking changes and corrections",
                "Extensions, reductions and refunds recalculate all beneficiaries' rewards using the corrected eligible amount. Differences are new linked entries; original entries and historical rates are not deleted or rewritten. Reducing accommodation from SAR 750 to SAR 600 changes its guest's reward from 22.50 to 18 points. If spent or gifted rewards leave insufficient balance, the difference is recovered from future earned rewards without showing a negative available balance. We do not debit a card or automatically recover from the gift recipient or new incoming transfers.",
            ),
            (
                "12. Statements and security",
                "Members can view their own credits, debits, balance after each entry and permitted references. A personal balance is not the total programme balance, and a transfer does not increase total points. Protect account access, do not share sign-in codes and report errors or unauthorised access. Evidence-backed disputes are reviewed; a ledger entry does not prevent correction of an established error or exercise of applicable rights.",
            ),
            (
                "13. Closing membership and changes",
                "A request for permanent membership closure shows the remaining points to be forfeited and requires explicit confirmation. Remaining points are forfeited when closure completes. Outstanding bookings and rights must be settled before closing; a booking is not automatically cancelled and card payments are not confiscated. Marketing opt-out or inactivity is not membership closure and does not forfeit points. New versions and effective dates are announced without retrospectively changing past transaction rates. Operator discontinuation is not voluntary withdrawal by members; accrued rights and points remain subject to an announced settlement and applicable law.",
            ),
            (
                "14. Privacy, contact and rights",
                "Information necessary for accounts, points, bookings, attribution, gifts, audit and security is processed under the privacy notice linked below. Earning a referral reward does not reveal another guest's stay information. Membership consent is separate from marketing; promotional messages can be stopped without losing membership or points, while necessary service notices remain. Use the contact page for enquiries, disputes and privacy requests. Property stay rules and mandatory rights under the property's local law remain applicable. These terms do not prevent complaints to competent authorities or waive non-waivable rights. Arabic, English and French versions are available and the consent language is recorded. Commercial registration or publication does not establish regulatory approval or classification of this programme.",
            ),
        ],
        "contact": "Contact and complaints",
        "privacy": "Privacy notice",
        "account": "My bookings & rewards",
    },
    "fr": {
        "title": "Conditions du programme d’avantages clients",
        "issued": "Première édition publiée — 16 octobre 2026",
        "intro": "L’adhésion est gratuite et facultative. Lisez ces conditions et la notice de confidentialité avant de consentir. Créer un compte ou vérifier un mobile ne constitue pas une adhésion et n’accorde pas de points en soi.",
        "operator": "L’entité responsable des points est مؤسسة الحياة الذكية التجارية (établissement commercial Smart Life), Riyad, Arabie saoudite, registre du commerce 1010792573, numéro unifié 7028510308. Adresse enregistrée : 7280 Al-Harfiyyin, Al-Yarmouk, numéro complémentaire 2405, code postal 13243. L’établissement répond des points pour tous les logements participants, y compris au Maroc. Cela ne remplace ni l’identité du prestataire d’hébergement ni les conditions du séjour figurant dans la confirmation.",
        "sections": [
            (
                "1. Adhésion et compte",
                "Un titulaire de compte client actif, dont l’adresse électronique est vérifiée et qui a la capacité de contracter, peut adhérer par consentement explicite. Aucune réservation préalable n’est nécessaire pour adhérer, vérifier un mobile ou recevoir des points offerts. La version des conditions, la langue et l’instant du consentement sont enregistrés. Les connexions par courriel, Google et Apple restent disponibles. Un mobile peut être associé pour une connexion ultérieure par code WhatsApp après vérification de sa possession ; enregistrer un numéro de contact ne le vérifie pas.",
            ),
            (
                "2. Réservations admissibles et acquisition",
                "Les récompenses concernent les réservations admissibles du site, y compris celles créées par son administration avec paiement justifié. Les réservations de canaux externes et celles antérieures au démarrage du programme ne donnent pas lieu à des points de séjour rétroactifs. L’adhésion, l’arrivée physique vérifiée et l’encaissement admissible sont nécessaires. Les points sont ajoutés pendant le séjour sans attendre sa fin ; une connexion au compte ou une date d’arrivée prévue ne suffit pas. Les dérogations d’essai précédemment désignées ne créent aucun droit à une dérogation pour d’autres clients.",
            ),
            (
                "3. Assiette et valeur des points",
                "L’assiette est l’hébergement effectivement payé autrement qu’en points, après réductions et remboursements, hors taxes, services et frais distincts éventuels. Les montants réglés en points ne génèrent pas de nouvelles récompenses. Un point vaut un riyal saoudien lors de son utilisation pour une réservation acceptant ce règlement. Les points ne sont ni vendus, ni rechargeables en espèces, ni remboursables en argent. Pour une autre devise, les valeurs et la devise de règlement prises en charge sont indiquées avant confirmation, selon une conversion documentée et non un taux supposé.",
            ),
            (
                "4. Taux et plafond",
                "Le client hébergé reçoit 3 % de l’assiette et son parrain direct 5 %. Les parrains antérieurs se partagent au maximum 7 % au total, plafonnés à 1 % par personne : de un à sept parrains antérieurs, chacun reçoit 1 % ; au-delà de sept, ils se partagent 7 % à parts égales. Aucune part n’est créée pour une personne absente de la chaîne et le total ne dépasse jamais 15 %. Chaque taux porte sur la réservation concernée, sans nouvelle multiplication par le nombre de filleuls. Le programme n’est ni un investissement, ni une promesse de revenu, ni une récompense du seul recrutement ; l’inscription est gratuite et sans achat obligatoire.",
            ),
            (
                "5. Exemples et décimales",
                "Un hébergement admissible de 750 riyals donne 22,50 points au client et 37,50 au parrain direct ; pour 950 riyals, les montants sont 28,50 et 47,50. Une réservation de 600 riyals réglée par 200 points et 400 riyals autrement donne 12 points au client. Les points sont comptabilisés à deux décimales ; les fractions individuelles inférieures au centième sont supprimées. Pour 52,50 points répartis entre huit parrains antérieurs, deux reçoivent 6,57 et six reçoivent 6,56. Un ordre fixe enregistré attribue les centièmes restants sans dépasser le total ni les plafonds.",
            ),
            (
                "6. Invitations et rattachement",
                "Le lien d’invitation devient disponible après un premier séjour admissible terminé et payé. Le filleul confirme le parrain affiché à l’adhésion ; le rattachement s’applique aux séjours admissibles répétés et n’est pas modifié par un autre lien ou un cadeau. Un parrain peut inviter plusieurs personnes, mais chaque filleul a un seul parrain direct. Auto-parrainages, cycles et comptes fictifs sont interdits. L’administration peut corriger un lien manquant justifié dans une procédure tracée, sans effacer les droits historiques. Les clients ne voient ni l’arbre complet des membres ni les séjours d’autrui.",
            ),
            (
                "7. Validité et utilisation",
                "Les points n’expirent pas par le seul écoulement du temps. Ils peuvent régler une partie ou la totalité d’une réservation admissible, selon le solde et la disponibilité. Les points et le paiement restant sont affichés avant confirmation. Des points peuvent être réservés temporairement pour éviter leur double utilisation, puis libérés après un échec confirmé. Une issue incertaine est vérifiée avant une autre réservation ou un autre débit. Les points ne modifient pas un prix convenu et ne garantissent pas un logement disponible.",
            ),
            (
                "8. Cadeaux et invitations WhatsApp",
                "Tout ou partie des points disponibles peut être offert à un membre actif, après vérification de son mobile authentifié ou numéro d’adhérent et présentation d’un nom abrégé. Débit et crédit sont réalisés ensemble et enregistrés pour les deux membres ; le destinataire n’a pas besoin d’être connecté. Si le numéro n’est pas admissible, l’expéditeur peut choisir une invitation attendue et consentie par notre numéro WhatsApp professionnel, dans la langue connue du destinataire ou celle sélectionnée. Une invitation ne crée aucune adhésion et ne réserve ni ne transfère de points. L’acceptation du prestataire ne prouve ni réception ni lecture.",
            ),
            (
                "9. Annulation avant l’arrivée physique",
                "Pour une réservation couverte par la politique de fidélité indiquée dans sa confirmation, le membre annulant avant le début physique du séjour récupère tout son règlement, quel que soit le motif ou la proximité de l’arrivée : les points reviennent au compte et les autres paiements à leur source initiale, sans condition de relocation du logement. Les récompenses issues de la réservation annulée sont corrigées séparément des anciens points utilisés pour payer. Le règlement en points n’est pas remboursé en argent et aucun remboursement ne dépasse le paiement. Une adhésion ultérieure ne requalifie pas une réservation hors programme.",
            ),
            (
                "10. Départ anticipé et règlement mixte",
                "Après le début du séjour, les nuits consommées sont facturées et les autres remboursées. Si le logement est effectivement libéré avant 20 h, heure locale, la nuit commençant à la date de départ et les nuits réservées suivantes sont remboursées ; à 20 h exactement ou après, cette nuit est due. Une arrivée lundi et un départ mercredi à 19 h facturent lundi et mardi. Cette règle de règlement n’autorise pas à prolonger l’heure normale de départ. Le remboursement mixte suit les proportions initiales : pour 1 800 riyals réglés par 600 points et 1 200 par carte, un remboursement de 1 200 rend 400 points et 800 riyals à la carte. Son état et sa référence sont communiqués ; l’apparition du crédit dépend du prestataire et de la banque.",
            ),
            (
                "11. Modifications et corrections",
                "Prolongations, réductions et remboursements entraînent un nouveau calcul des récompenses sur l’assiette correcte. Les écarts sont de nouvelles écritures liées, sans supprimer l’original ni réécrire son taux historique. Un hébergement réduit de 750 à 600 riyals fait passer la récompense du client de 22,50 à 18 points. Si les points ont été dépensés ou offerts et que le solde est insuffisant, l’écart est compensé sur les futures récompenses acquises, sans afficher de solde disponible négatif. Aucune carte n’est débitée et aucune reprise automatique n’est faite auprès du bénéficiaire du cadeau ou sur un nouveau transfert entrant.",
            ),
            (
                "12. Relevés et sécurité",
                "Le membre consulte ses propres crédits, débits, soldes après écriture et références autorisées. Son solde n’est pas le solde global du programme ; un transfert n’augmente pas le total des points. Protégez l’accès au compte, ne partagez pas les codes et signalez erreurs ou accès non autorisés. Les contestations sont examinées sur justificatifs ; une écriture n’empêche ni correction d’une erreur établie ni exercice des droits applicables.",
            ),
            (
                "13. Résiliation et modifications",
                "Une demande de résiliation définitive indique les points restants qui seront perdus et nécessite une confirmation explicite. Ces points sont perdus à la clôture effective. Les réservations et droits en cours doivent être réglés avant clôture ; aucune réservation n’est automatiquement annulée et aucun paiement par carte n’est confisqué. Refuser le marketing ou ne pas utiliser le compte ne résilie pas l’adhésion et ne supprime pas les points. Les nouvelles versions et dates d’effet sont annoncées, sans modifier rétroactivement les taux d’écritures antérieures. L’arrêt du programme par l’opérateur n’est pas un retrait volontaire des membres ; les droits acquis et points restent à traiter selon le règlement annoncé et le droit applicable.",
            ),
            (
                "14. Confidentialité, contact et droits",
                "Les données nécessaires au compte, aux points, réservations, parrainages, cadeaux, contrôles et à la sécurité sont traitées selon la notice liée ci-dessous. Une récompense de parrainage ne révèle pas le séjour d’autrui. Le consentement à l’adhésion est distinct du marketing ; les promotions peuvent être arrêtées sans perte d’adhésion ni de points, les notifications nécessaires au service restant possibles. Utilisez la page de contact pour questions, contestations et demandes relatives aux données. Les règles du logement et droits impératifs du pays du logement demeurent applicables. Ces conditions n’empêchent ni réclamation aux autorités compétentes ni exercice des droits auxquels il ne peut être renoncé. Les versions arabe, anglaise et française sont disponibles et la langue du consentement est enregistrée. Un registre du commerce ou cette publication ne constitue pas une approbation ni une qualification réglementaire du programme.",
            ),
        ],
        "contact": "Contact et réclamations",
        "privacy": "Notice de confidentialité",
        "account": "Mes réservations et avantages",
    },
}


UPDATED_CONTENT = deepcopy(CONTENT)
for code, issued in {
    "ar": "الإصدار الثاني — ١٦ أكتوبر ٢٠٢٦؛ تحديث موعد استحقاق المكافآت",
    "en": "Second edition — 16 October 2026; reward eligibility timing updated",
    "fr": "Deuxième édition — 16 octobre 2026 ; mise à jour du moment d’acquisition des points",
}.items():
    UPDATED_CONTENT[code]["issued"] = issued
for code, paragraph in {
    "ar": "تُكتسب مكافآت الإقامة عن حجوزات الموقع المؤهلة، بما فيها الحجوزات التي تنشئها الإدارة من خلاله وتثبت مدفوعاتها. لا تمنح حجوزات القنوات الخارجية نقاط إقامة، ولا تمنح الحجوزات السابقة لبدء تشغيل البرنامج مكافآت بأثر رجعي. يُشترط الانضمام وتأكيد سداد قيمة الإقامة. يتحقق الاستحقاق تلقائيًا عند الثالثة عصرًا من يوم الدخول بتوقيت العقار، أو عند توثيق الإدارة للوصول في أي وقت، ولو قبل يوم الدخول، أيهما أسبق. يعالج الفحص الدوري الاستحقاق من ذلك الموعد؛ وإذا اكتمل السداد أو الانضمام بعده أثناء الإقامة، يعاد التحقق عند اكتمالهما. لا يلزم انتظار نهاية الإقامة، ولا تكفي كتابة رقم حجز أو تسجيل الدخول للحساب وحدهما. حلول الموعد لا يسجل وصولًا فعليًا، ولا تكرر المكافأة عند توثيق الوصول لاحقًا. تطبق تصحيحات التعديل والإلغاء والاسترداد الواردة أدناه، دون إعادة كتابة العمليات أو نسبها التاريخية. الحجوزات والاستثناءات الاختبارية المحددة سابقًا لا تمنح الآخرين استثناءً.",
    "en": "Stay rewards apply to eligible website bookings, including those created by administration through the website with verified payments. External-channel bookings earn no stay rewards, and bookings made before programme launch receive no retrospective rewards. Membership and confirmed payment for the stay are required. Eligibility begins automatically at 3 pm on check-in day in the property’s local time, or when administration records arrival at any time, including before check-in day, whichever comes first. A periodic check processes rewards from that time; payment or enrolment completed later during the stay triggers another eligibility check. There is no need to wait until departure; entering a booking number or signing in alone is insufficient. Reaching the scheduled time does not record physical arrival, and later arrival confirmation never repeats the reward. The modification, cancellation and refund corrections below still apply without rewriting historical entries or rates. Previously authorised test bookings and exceptions do not create exceptions for other guests.",
    "fr": "Les récompenses de séjour concernent les réservations admissibles du site, y compris celles créées par l’administration sur le site avec paiement vérifié. Les réservations de canaux externes ne donnent pas de points de séjour ; celles antérieures au lancement n’en donnent pas rétroactivement. L’adhésion et le paiement confirmé du séjour sont obligatoires. L’admissibilité commence automatiquement à 15 h le jour d’arrivée, heure locale du logement, ou lorsque l’administration enregistre l’arrivée à tout moment, même avant le jour prévu, selon la première éventualité. Un contrôle périodique traite les points à partir de cet instant ; un paiement ou une adhésion finalisés plus tard pendant le séjour déclenchent un nouveau contrôle. Il n’est pas nécessaire d’attendre le départ ; saisir un numéro de réservation ou se connecter ne suffit pas. L’heure prévue ne constate pas une arrivée physique et une confirmation ultérieure ne répète jamais la récompense. Les corrections pour modification, annulation et remboursement ci-dessous restent applicables, sans réécrire les écritures ni leurs taux historiques. Les anciennes dérogations d’essai ne créent aucune exception pour d’autres clients.",
}.items():
    UPDATED_CONTENT[code]["sections"][1] = (CONTENT[code]["sections"][1][0], paragraph)

# The first edition and its consent hashes remain addressable, unchanged.
IDENTITY_CONTENT = deepcopy(UPDATED_CONTENT)
for code, issued, paragraph in [
    (
        "ar",
        "الإصدار الثالث — ١٧ أكتوبر ٢٠٢٦؛ الجوال الموثق وإدارة بيانات العضوية",
        "يجوز لصاحب حساب ضيف نشط ممن تتوفر لديه أهلية التعاقد الانضمام بموافقة صريحة بعد توثيق جواله أو بريده. لا يشترط حجز سابق للانضمام أو لتوثيق الجوال أو لاستلام هدية نقاط. الجوال الموثق هو وسيلة التعرف الأساسية على حساب الضيف وعضويته وربط حجوزاته المؤهلة؛ يلزم إثبات ملكيته برمز واتساب عند الدخول أو الربط غير الموثق، ولا تكفي كتابة رقم شخص آخر. يبقى البريد وجوجل وأبل خيارات إضافية قائمة، ولا يتطلب الدخول بالجوال الوصول إلى البريد. تغيير الجوال أو البريد المرتبط بالعضوية من صلاحية الإدارة فقط بعد التحقق من هوية صاحبها، ويسجل المنفذ والوقت والسبب. لا يعد اعتماد الإدارة للرقم الجديد توثيقًا لملكيته حتى يؤكده صاحبه عبر واتساب، ولا يعد تعديل البريد إثباتًا لملكيته. تبقى العضوية ورصيدها وسجلها وعلاقة الداعي محفوظة عند التعديل، وتنتهي جلسات الدخول السابقة لحماية الحساب. توثيق الجوال لأول مرة متاح للضيف، وحفظ رقم تواصل وحده لا يوثقه. تحفظ نسخة الشروط ولغتها وتاريخ الموافقة.",
    ),
    (
        "en",
        "Third edition — 17 October 2026; verified mobile and membership identity management",
        "An active guest with contractual capacity may join with explicit consent after verifying a mobile or email. No prior booking is needed to join, verify a mobile or receive a points gift. The verified mobile is the primary identifier for the guest account, membership and eligible booking links; WhatsApp proof of possession is required for mobile sign-in or an unverified link, not merely entering somebody’s number. Email, Google and Apple remain additional sign-in choices; mobile sign-in does not require access to the email inbox. Only administration may change a membership’s mobile or email after checking the owner’s identity; the operator, time and reason are recorded. Administrative approval of a new number is not proof of possession until its owner verifies it with WhatsApp; changing an email does not verify it. Membership, points, history and inviter relationship are preserved, and previous sessions end to protect the account. Guests may verify their first mobile themselves; saving a contact number does not verify it. The terms edition, language and consent time are retained.",
    ),
    (
        "fr",
        "Troisième édition — 17 octobre 2026 ; mobile vérifié et gestion de l’identité du membre",
        "Un client actif ayant la capacité de contracter peut adhérer avec son consentement explicite après vérification de son mobile ou de son e-mail. Aucune réservation préalable n’est nécessaire pour adhérer, vérifier un mobile ou recevoir des points. Le mobile vérifié est l’identifiant principal du compte, de l’adhésion et des réservations admissibles ; une preuve de possession par code WhatsApp est requise pour la connexion ou un rattachement non vérifié, et non la simple saisie du numéro d’autrui. L’e-mail, Google et Apple restent des options supplémentaires ; la connexion mobile ne nécessite pas l’accès à la messagerie. Seule l’administration peut modifier le mobile ou l’e-mail d’une adhésion après contrôle de l’identité du titulaire ; l’auteur, l’heure et le motif sont enregistrés. L’approbation administrative d’un nouveau numéro ne prouve pas sa possession : son titulaire doit le vérifier par WhatsApp ; modifier un e-mail ne le vérifie pas. L’adhésion, les points, l’historique et le parrain sont conservés, et les anciennes sessions sont fermées pour protéger le compte. Le client peut vérifier lui-même son premier mobile ; enregistrer un numéro de contact ne le vérifie pas. L’édition des conditions, la langue et la date du consentement sont conservées.",
    ),
]:
    IDENTITY_CONTENT[code]["issued"] = issued
    paragraph += {
        "ar": " تربط حجوزات الموقع الجديدة المسددة بالعضوية تلقائيًا عند تطابق جوال الحجز مع جوال موثق للعضو قبل إنشاء الحجز، حتى دون تسجيل الدخول وبغض النظر عن اختلاف البريد. لا يمنح هذا التطابق جلسة دخول أو حق صرف النقاط لمن كتب الرقم؛ الدخول واستخدام الرصيد يتطلبان إثبات صاحب الحساب. تبقى الحجوزات القديمة خاضعة لمسار الاسترجاع المعتمد.",
        "en": " New paid website bookings link automatically to a membership when the booking mobile matches a number already verified for that member before the booking was created, even without sign-in and regardless of email differences. This matching grants no login session or right to spend points to whoever entered the number; account access and spending require authentication. Historical bookings retain the approved recovery process.",
        "fr": " Les nouvelles réservations du site réglées sont rattachées automatiquement à l’adhésion lorsque leur mobile correspond à un numéro déjà vérifié pour ce membre avant la création de la réservation, même sans connexion et indépendamment de l’e-mail. Cette correspondance ne donne ni session de connexion ni droit de dépenser des points à la personne ayant saisi le numéro ; l’accès au compte et l’utilisation du solde exigent une authentification. Les anciennes réservations conservent la procédure de récupération approuvée.",
    }[code]
    IDENTITY_CONTENT[code]["sections"][0] = (CONTENT[code]["sections"][0][0], paragraph)

EDITIONS = {
    VERSION: (URL_VERSION, REFERENCE, CONTENT),
    TERMS_VERSION: (TERMS_URL_VERSION, TERMS_REFERENCE, UPDATED_CONTENT),
    IDENTITY_TERMS_VERSION: (
        IDENTITY_TERMS_URL_VERSION,
        IDENTITY_TERMS_REFERENCE,
        IDENTITY_CONTENT,
    ),
}


def document(language=None, *, version=None):
    version = version or settings.LOYALTY_TERMS_VERSION
    version = version if version in EDITIONS else VERSION
    content_by_language = EDITIONS[version][2]
    code = (language or get_language() or "ar").split("-")[0]
    code = code if code in content_by_language else "ar"
    content = content_by_language[code]
    digest = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {**content, "version": version, "language": code, "sha256": digest}


def terms_reference():
    edition = EDITIONS.get(settings.LOYALTY_TERMS_VERSION)
    if edition and settings.LOYALTY_TERMS_REFERENCE == edition[1]:
        with override(document()["language"]):
            return reverse("core:rewards_terms", kwargs={"version": edition[0]})
    return settings.LOYALTY_TERMS_REFERENCE


def consent_metadata(version, reference, language):
    edition = EDITIONS.get(version)
    if not edition:
        return {}
    if language not in CONTENT:
        raise ValidationError("Unsupported terms language.")
    with override(language):
        expected = reverse("core:rewards_terms", kwargs={"version": edition[0]})
    if reference != expected:
        raise ValidationError("Use the issued terms reference for this language.")
    return {"terms_content_sha256": document(language, version=version)["sha256"]}


@require_safe
@never_cache
def terms_view(request, version):
    issued_version = next((key for key, value in EDITIONS.items() if value[0] == version), None)
    if not issued_version:
        raise Http404
    issued = document(version=issued_version)
    response = render(request, "loyalty/public_terms.html", {"reward_terms": issued})
    response["X-Document-Version"] = issued_version
    response["X-Document-SHA256"] = issued["sha256"]
    return response
