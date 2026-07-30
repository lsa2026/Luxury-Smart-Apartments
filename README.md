# Luxury Smart Apartments

منصة Django عربية لحجز وحدات التأجير اليومي والشهري. تشمل المراحل الحالية
الأساس الإنتاجي، مزامنة وحدات Hostaway وصورها ومرافقها، طبقة محتوى محلية، ومزامنة
المراجعات المنشورة من نوع `guest-to-host`، والتحقق اللحظي من التقويم وحساب السعر.

## المتطلبات

- Python 3.13
- PostgreSQL 15 أو أحدث
- Git
- Pillow للتحقق من الصور المحلية

لا يُستخدم SQLite إلا داخل إعدادات الاختبارات المحلية المؤقتة.

## الإعداد على Windows PowerShell

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env
```

أنشئ دور PostgreSQL مخصصًا للتطبيق وقاعدة مستقلة. نفّذ أوامر الإنشاء بحساب
إداري، لكن لا تستخدم الحساب الإداري داخل `DATABASE_URL`:

```sql
CREATE ROLE luxury_apartments LOGIN
    PASSWORD 'choose-a-local-password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE DATABASE luxury_apartments OWNER luxury_apartments;
```

صيغة `DATABASE_URL`:

```text
postgresql://luxury_apartments:<local-password>@127.0.0.1:5432/luxury_apartments
```

لا تنسخ كلمة المرور النموذجية حرفيًا، ولا تضع كلمة المرور الفعلية في README أو
`.env.example`.

استخدم `config.settings.development` محليًا و`config.settings.production` في
الإنتاج. اضبط `HOSTAWAY_IMAGE_CSP_SOURCES` بقائمة مصادر HTTPS الدقيقة إذا أضاف
الحساب نطاقات صور غير المصادر الافتراضية. لا تستخدم `*` ولا تضف `.env` إلى Git.
أظهر التحقق الفعلي أيضًا نطاق `https://a0.muscache.com` لبعض صور Airbnb القادمة
ضمن استجابة Hostaway، ولذلك أضيف إلى قائمة CSP الدقيقة.

## migrations والتشغيل

```powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

تتضمن migrations المرحلة الثانية:

- `properties/0002`: إعادة تسمية `hostaway_listing_id` إلى
  `hostaway_listing_map_id` باستخدام `RenameField` مع الحفاظ على البيانات.
- `properties/0003`: البيانات التشغيلية والصور والمرافق والفهارس والقيود.
- `properties/0004`: إعادة تسمية `property_type` إلى
  `hostaway_property_type_id` باستخدام `RenameField`، وإضافة
  `hostaway_special_status` مع الحفاظ على حالة السجلات السابقة.
- `properties/0005`: إعادة تسمية الحقل السابق إلى `hostaway_listing_id`
  باستخدام `RenameField` مع الحفاظ على البيانات، ثم إضافة
  `hostaway_listing_map_id` اختياريًا بقيد فريد شرطي عند وجود قيمة.
- `integrations/0001`: سجل تشغيل عمليات المزامنة.

الصفحات:

- `/properties/`: قائمة الوحدات المنشورة.
- `/properties/<slug>/`: تفاصيل الوحدة والمعرض والمرافق والمراجعات.
- `POST /reservations/quotes/`: تحقق خادمي وإنشاء عرض سعر مؤقت.
- `/reservations/quotes/<signed-reference>/`: عرض السعر ونموذج بيانات الضيف.
- `/reservations/requests/<public-reference>/`: مراجعة طلب الحجز المبدئي.
- `/admin/`: إدارة المحتوى والصور.

## التوافر وحساب السعر

Hostaway هو المصدر النهائي للتوافر والسعر. لا يُستدعى عند تحميل الصفحة؛ يبدأ
الاتصال فقط بعد أن يرسل الزائر تواريخ الوصول والمغادرة وعدد الضيوف إلى Django
باستخدام POST محمي بـCSRF. يتحقق الخادم محليًا من التواريخ والسعة وحالة الوحدة،
ثم يقرأ:

```text
GET /v1/listings/{listingId}/calendar?includeResources=0
```

تشمل ليالي الإقامة تاريخ الوصول وحتى اليوم السابق للمغادرة. يلزم وجود كل يوم
مطلوب، وأن يكون متاحًا، وألا تمنع قيود الوصول أو المغادرة الفترة، وأن يتحقق الحد
الأدنى والأقصى للإقامة. أي يوم مفقود أو غير مفهوم يعامل بصورة محافظة على أنه غير
متاح. الحد الأقصى للطلب 366 ليلة، وعدد الضيوف موجب ولا يتجاوز
`person_capacity`.

يحدد النظام المخزون من شكل يوم التقويم. الأيام المفردة تعتمد على `isAvailable`.
أما عند ظهور حقول مخزون متعددة الوحدات، فالأولوية هي
`availableUnitsToSell` ثم `countAvailableUnits` ثم `desiredUnitsToSell`.
لا يستخدم `countReservedUnits` منفردًا لإثبات التوفر ولا يشتق المخزون من
الحجوزات. تعارض أي إشارة مخزون مع `isAvailable` ينتج
`inventory_conflict` ويمنع طلب السعر.

بعد نجاح التقويم فقط، يحسب الخادم السعر من:

```text
POST /v1/listings/{listingId}/calendar/priceDetails
```

يرسل `startingDate` و`endingDate` و`numberOfGuests` و`version=2` فقط. عملية POST
هذه آلة حاسبة موثقة للسعر وليست إنشاء حجز. يستخدم النظام `totalPrice` القادم من
Hostaway و`Decimal` للقيم المالية، ولا يعيد جمع الأسعار اليومية أو يضيف خصمًا أو
ضريبة محلية. السعر والتوافر مؤقتان ويجب إعادة التحقق منهما قبل إنشاء الحجز
والدفع مستقبلًا.

تُخزن نتائج التقويم والسعر في Django Cache لمدة 60 ثانية. تشمل المفاتيح الوحدة
والتواريخ والضيوف وإصدار الحاسبة، ولا تشمل الرمز أو بيانات ضيف. Cache ليس ضمانًا
عند الحجز. نقطة البحث محدودة افتراضيًا إلى 20 محاولة خلال 5 دقائق لكل مفتاح جلسة
وعنوان مؤقت مشفّر، دون حفظ IP خام طويلًا.

إعداد التطوير الحالي يستخدم `LocMemCache`، لذلك يظهر cache hit بين طلبات الويب
داخل process نفسه، لكنه لا ينتقل بين عمليتي `manage.py` منفصلتين. يجب استخدام
Redis مشترك عند النشر بعدة workers؛ لا تُخزن بيانات التقويم اليومية في
PostgreSQL. Cache ليس ضمانًا للتوافر، ويجب إعادة التحقق مباشرة قبل إنشاء الحجز
والدفع مستقبلًا.

أمر تحقق آمن لوحدة محلية واحدة:

```powershell
python manage.py verify_hostaway_availability `
  --listing-id 315816 `
  --scan-days 60 `
  --stay-nights 2 `
  --guests 2 `
  --show-schema `
  --strict `
  --timeout 20
```

إذا لم تُحدد التواريخ، يقرأ الأمر 60 يومًا ويبحث محليًا عن أول فترة من ليلتين،
ثم يرسل طلب سعر واحد على الأكثر. يمكن تحديد `--check-in` و`--check-out`، واستخدام
`--bypass-cache` للتحقق الإداري فقط. لا يحفظ الأمر التقويم أو السعر أو JSON الخام،
ولا يعرض موارد الحجوزات أو الملاحظات، ولا ينشئ Reservation.

لتشخيص مخزون وحدة واحدة على مدى سنة دون كشف محتوى الحجوزات:

```powershell
python manage.py verify_hostaway_availability `
  --listing-id 315816 `
  --scan-days 365 `
  --stay-nights 2 `
  --guests 2 `
  --diagnose-inventory `
  --show-schema `
  --strict `
  --bypass-cache `
  --timeout 20
```

يعرض التشخيص توزيعات وإحصاءات مجمعة فقط. إذا أعادت Hostaway مفتاح
`reservations`، يتجاهل validator محتواه قبل إنشاء DTO ولا يسجل الملاحظات أو
الأسماء أو المعرفات. لا يستدعي `priceDetails` ما لم يثبت التقويم توفر الفترة.

## عروض السعر وطلبات الحجز المبدئية

`BookingQuote` لقطة محلية مؤقتة للسعر الذي أعادته Hostaway بعد إثبات التوافر.
صلاحيتها الافتراضية عشر دقائق (`BOOKING_QUOTE_TTL_SECONDS=600`). لا يخزن النموذج
استجابة Hostaway الخام؛ تحفظ فقط المكونات المنقحة اللازمة للعرض. تحمل بيانات
العرض بصمة HMAC من الخادم، ولا تؤخذ العملة أو الإجمالي أو معرّف Hostaway من
حقول المتصفح.

`BookingIntent` طلب محلي مبدئي ينشأ بعد إدخال بيانات الضيف والموافقة الإلزامية
على الشروط والخصوصية. قبل إنشائه يعيد الخادم قراءة التقويم وحساب السعر مباشرة
مع تجاوز Cache. إذا اختفى التوافر فلا ينشأ طلب قابل للدفع. وإذا تغيرت العملة أو
السعر بأي مقدار، يصبح العرض القديم `price_changed` وينشأ عرض جديد يحتاج موافقة
جديدة. قيمة `BOOKING_PRICE_TOLERANCE` حاليًا `0.00`، ولا تُتجاهل فروق التقريب.

بعد نجاح إعادة التحقق تكون حالة الطلب `awaiting_payment`، لكنها لا تعني أن
الحجز مؤكد. `BookingIntent` ليس `Reservation` ولا ينشئ حجزًا أو حظرًا في
Hostaway. لم تُربط بوابة دفع بعد؛ `DisabledPaymentProvider` يمنع إنشاء جلسة دفع
ويرجع الرمز الداخلي `payment_provider_not_configured`، ولا ينشئ
`PaymentAttempt` وهميًا.

يحمي النظام العرض والطلب بجلسة Django، ومرجع عام عشوائي، وhash للجلسة لا يكشف
مفتاحها الخام. تعيد محاولة الوصول من جلسة أخرى 404 عامة. تنفذ عمليات POST
باستخدام CSRF وPost/Redirect/Get، مع rate limiting بواسطة Django Cache. لا
تُعرض معرفات Hostaway أو UUID الداخلي في الواجهة العامة.

تنتهي عروض السعر والطلبات غير المكتملة دون حذف فوري:

```powershell
python manage.py expire_booking_intents --dry-run
python manage.py expire_booking_intents
```

صلاحية الطلب المبدئي الافتراضية 30 دقيقة
(`BOOKING_INTENT_TTL_SECONDS=1800`). السياسة التقنية المقترحة هي تنقيح أو حذف
البيانات الشخصية للطلبات غير المكتملة بعد 30 يومًا
(`BOOKING_INCOMPLETE_RETENTION_DAYS=30`) بأمر صيانة مستقل يدعم dry-run؛ لم يُفعّل
الحذف التلقائي في هذه المرحلة. لا تسجل الشيفرة البريد أو الهاتف كاملين، ولا ترسل
بيانات الضيف إلى Hostaway.

## الحجز المستقبلي وUnified Webhooks

تضيف المرحلة الخامسة ثلاث طبقات منفصلة:

- `Reservation`: الحالة المحلية المنقحة للحجز. قد ترتبط بـ`BookingIntent` للحجز
  المباشر، أو تكون بلا طلب عند وصول حجز من قناة خارجية. لا تكون مؤكدة إلا عند
  وجود `hostaway_reservation_id` حقيقي.
- `PaymentAttempt`: سجل محاولة دفع مستقبلية. لا ينشأ تلقائيًا، ولا توجد بوابة
  دفع مفعلة حاليًا.
- `HostawayReservationOperation`: سجل idempotency منقح يمنع إرسال طلب إنشاء
  الحجز مرتين ولا يخزن request أو response خامًا.

كود إنشاء حجز Hostaway موجود خلف حواجز متعددة، وكلها مغلقة افتراضيًا:

```dotenv
HOSTAWAY_LIVE_BOOKING_ENABLED=False
HOSTAWAY_RESERVATION_PROVIDER=LuxurySmartApartments
HOSTAWAY_DIRECT_CHANNEL_ID=
HOSTAWAY_RESERVATION_REQUEST_TIMEOUT_SECONDS=20
```

لا يبدأ الاستدعاء إلا بعد دفع ناجح حقيقي، وإعادة تحقق مباشرة من التوافر
و`priceDetails`، وتطابق السعر والعملـة، ووجود `listingMapId` و`channelId`
موثقين. لا يُستخدم Listing ID بديلًا لـListing Map ID. يبنى `financeField` من
مكونات السعر المعاد التحقق منها. لا يرسل النظام `forceOverbooking` أو
`validatePaymentMethod` أو بيانات بطاقة أو Door Code أو Notes. عند timeout أو
5xx بعد محاولة POST تصبح العملية `unknown` ولا يعاد POST تلقائيًا؛ يجب إجراء
مصالحة آمنة أو مراجعة إدارية أولًا.

لفحص المتطلبات المسبقة عبر GET فقط:

```powershell
python manage.py verify_hostaway_booking_prerequisites --listing-id 315816 --strict
python manage.py verify_hostaway_booking_prerequisites --listing-id 315816 --show-schema
```

مستقبل Unified Webhook المحلي:

```text
POST /integrations/hostaway/webhooks/unified/
```

وهو معطل افتراضيًا، ويستخدم Basic Authentication ببيانات منفصلة لا تدخل Git:

```dotenv
HOSTAWAY_WEBHOOK_RECEIVER_ENABLED=False
HOSTAWAY_WEBHOOK_PROCESSING_ENABLED=False
HOSTAWAY_WEBHOOK_BASIC_AUTH_USERNAME=
HOSTAWAY_WEBHOOK_BASIC_AUTH_PASSWORD=
HOSTAWAY_WEBHOOK_MAX_BODY_BYTES=262144
HOSTAWAY_WEBHOOK_ALLOWED_EVENTS=reservation.created,reservation.updated
```

لا يخزن المستقبل body الخام أو Authorization أو بيانات الضيف أو الباب أو
الملاحظات. يحتفظ فقط بقائمة حقول تشغيلية مسموحة وhash للجسم ومفتاح deduplication.
تقبل الأحداث المكررة بصورة idempotent، وتُسجّل الأنواع غير المعروفة
`ignored` مع HTTP 200. طلب HTTP لا يستدعي Hostaway؛ تعالج الأحداث لاحقًا:

```powershell
python manage.py process_hostaway_webhooks --dry-run
python manage.py process_hostaway_webhooks --limit 50
python manage.py process_hostaway_webhooks --event-id <event-uuid>
python manage.py process_hostaway_webhooks --retry-failed
```

المعالج يجلب الحجز الحالي عبر GET ويطابق الوحدة بـListing Map ID أولًا، ثم
Listing ID فقط عندما يكون fallback فريدًا. يدعم وصول `reservation.updated` قبل
`reservation.created`، ولا يسمح لحدث قديم باستبدال `source_updated_at` أحدث.
لا يوجد Celery أو worker دائم حاليًا؛ يلزم جدولة أمر الإدارة بواسطة worker
موثوق عند الإنتاج. يجب وضع سياسة احتفاظ للأحداث المنقحة حسب متطلبات التشغيل
والخصوصية؛ لا يحذف النظام الأحداث تلقائيًا في هذه المرحلة.

### Production Readiness Blockers

- Payment provider not configured.
- Hostaway live booking disabled.
- Listing Map ID not verified (when absent from the trusted listing response).
- Direct Channel ID not verified.
- Unified Webhook not registered in Hostaway.
- Background worker not configured.
- Redis shared cache required for multi-worker production.
- Confirmed direct reservation required for live modification verification.
- Refund workflow and cancellation policy approval not configured.
- Live modification, extension, and cancellation flags disabled.
- External-channel modification policy not approved.

لا يجوز رفع مفاتيح API أو رمز الوصول أو كلمة مرور Webhook إلى Git. تسجيل
Unified Webhook في Hostaway وتفعيل flags خطوات إنتاج يدوية مستقلة لا تنفذها
هذه المرحلة.

## إدارة الحجز وطلبات التعديل

`BookingModificationRequest` هو طلب محلي يملكه العميل عبر جلسة Django ومرجع عام
عشوائي. لا يعني أن Reservation تغيرت في Hostaway. يدعم تمديد الإقامة وتغيير
التواريخ أو عدد الضيوف وطلب الإلغاء المحلي.

يعيد التمديد التحقق من الأيام الإضافية مباشرة، ثم يستخدم `priceDetails` version
2 للفترة الكاملة. يحسب `price_difference = new_total - old_total` باستخدام
`Decimal`. الفرق الموجب ينتظر الدفع مستقبلًا، والفرق الصفري أو السالب ينتظر
مراجعة الإدارة. لا ينشأ PaymentAttempt أو Refund، ولا تتغير Reservation الأصلية
إلا بعد نجاح كتابة Hostaway مستقبلًا ثم GET للمصالحة.

طلبات القنوات الخارجية لا تصبح قابلة للتعديل أو الإلغاء التلقائي. يجب أن يجري
العميل العملية عبر القناة أو أن تراجعها الإدارة وفق سياسة موثقة. طلب الإلغاء
المحلي لا يغير حالة Reservation إلى `cancelled` ولا يؤكد قيمة استرداد.

كل حدود الكتابة مغلقة افتراضيًا:

```dotenv
HOSTAWAY_LIVE_MODIFICATION_ENABLED=False
HOSTAWAY_LIVE_EXTENSION_ENABLED=False
HOSTAWAY_LIVE_CANCELLATION_ENABLED=False
BOOKING_MODIFICATION_REQUEST_TTL_SECONDS=1800
BOOKING_EXTENSION_MAX_NIGHTS=30
BOOKING_MODIFICATION_CUTOFF_HOURS=48
BOOKING_CANCELLATION_REQUEST_ENABLED=True
BOOKING_AUTOMATIC_MODIFICATION_APPROVAL=False
BOOKING_AUTOMATIC_CANCELLATION_ENABLED=False
```

حتى عند الموافقة المحلية، لا تنفذ لوحة الإدارة PUT ولا تسمح بوضع الطلب
`completed` يدويًا. يحفظ `HostawayModificationOperation` بصمة HMAC وحالة
idempotency فقط، دون request أو response خام. timeout أو 5xx بعد محاولة الكتابة
المستقبلية يجعل الحالة `unknown` ولا يعاد PUT تلقائيًا.

التوثيق الرسمي الحالي يحدد تعديل الحجز عبر
`PUT /reservations/{reservationId}`، والإلغاء عبر
`PUT /reservations/{reservationId}/statuses/cancelled` مع `cancelledBy`. الكود
لا يضيف `forceOverbooking` أو بيانات بطاقة، وهو غير مفعّل حتى تثبت المعرفات
وقيود القناة عمليًا.

أوامر التحقق المقروء:

```powershell
python manage.py verify_hostaway_reservation_identifiers `
  --listing-id 315816 --reservation-limit 20 `
  --include-direct-reservations --show-schema --strict --timeout 20

python manage.py verify_hostaway_modification_prerequisites `
  --listing-id 315816 --show-schema --strict --timeout 20
```

الأمر الأول يعرض Channel IDs وstatus وsource وأسماء الحقول وأنواعها فقط؛ لا
يعرض أسماء الضيوف أو البريد أو الهاتف أو أرقام الحجوزات كاملة. لا يكتب أي قيمة
مكتشفة إلى `.env`.

تنتهي الطلبات غير المكتملة دون حذفها:

```powershell
python manage.py expire_booking_modification_requests --dry-run
python manage.py expire_booking_modification_requests --limit 500
```

تؤكد Webhooks التعديل فقط عندما يطابق GET Reservation التواريخ والضيوف والسعر
والعملة المطلوبة. الحدث الأقدم لا يستبدل حالة أحدث، ولا ينشئ حدث غير معروف طلب
تعديل محليًا.

## ملكية البيانات

Hostaway هو المصدر للمعرّف والحالة التشغيلية والسعة والغرف والأسرة والحمامات
والعنوان والموقع والصور الأصلية وروابط المرافق.

تُخزن قيمة `specialStatus` الخام في `hostaway_special_status`.
`hostaway_is_active` قيمة مشتقة: تكون `False` فقط عندما تكون الحالة
`archived`. تُحفظ أي حالة مستقبلية غير معروفة وتبقى الوحدة نشطة حتى تُراجع
السياسة، بدل إسقاط القيمة أو إيقاف المزامنة.

في العينة الفعلية أعادت Listing الحقل `id` ولم تعد `listingMapId`. لذلك يُحفظ
`Listing.id` دائمًا في `hostaway_listing_id`، ولا يُملأ
`hostaway_listing_map_id` إلا إذا ظهر الحقل فعليًا في استجابة موثقة؛ لا ينسخ
النظام `id` إليه. لم يظهر `updatedOn` في الوحدات، ويستخدم النظام
`latestActivityOn` كأفضل مصدر متاح لـ`source_updated_at`. يمثل هذا الحقل آخر
نشاط لدى Hostaway، وليس بالضرورة وقت تعديل محتوى الوحدة.

قاعدة الموقع هي المصدر للمحتوى التسويقي العربي والإنجليزي وSEO و`slug` والترتيب
والظهور والتمييز والصور المحلية وحقول عرض الصور. تملأ المزامنة `name_en` و`slug`
و`city_en` مبدئيًا عند إنشاء الوحدة فقط. بعد ذلك لا تدخل أي من الحقول المحلية في
تحديثات المزامنة. يوضّح `content_is_customized` أن المسؤول عدّل المحتوى، بينما
الحماية الأساسية مطبقة بفصل حقول المصدر عن `defaults` التشغيلية.

لا توجد ترجمة آلية ولا يُولّد محتوى عربي.

## مزامنة الوحدات

الوضع الافتراضي يشمل الصور والمرافق:

```powershell
python manage.py sync_hostaway_properties
python manage.py sync_hostaway_properties --listing-id 40160
python manage.py sync_hostaway_properties --limit 20
python manage.py sync_hostaway_properties --dry-run
python manage.py sync_hostaway_properties --force
python manage.py sync_hostaway_properties --no-include-images
python manage.py sync_hostaway_properties --no-include-amenities
```

`--limit` يحدد الحد الأقصى للوحدات في التشغيل. `--force` يسمح بتطبيق استجابة ذات
`source_updated_at` أقدم من المخزن؛ لا يتجاوز قفل التزامن. يستخدم الإنتاج
PostgreSQL advisory lock لمنع تشغيل مزامنتين في الوقت نفسه، كما يمنع قيد قاعدة
البيانات وجود سجلين بحالة `running`.

تعتمد صفحات Listing على `limit` و`offset` و`count`. لا يُشترط وجود `page` أو
`totalPages`، وتتوقف القراءة عند بلوغ `count` أو وصول صفحة قصيرة أو فارغة.

وضع `--dry-run` يجلب البيانات ويتحقق منها ويحسب التقرير دون تعديل قاعدة البيانات
أو إنشاء سجل مزامنة.

## المصادقة والتحقق الفعلي

يستخدم `HostawayTokenProvider` السياسة التالية:

1. يفضّل `HOSTAWAY_ACCESS_TOKEN` إذا كان مضبوطًا.
2. وإلا ينشئ رمزًا من `HOSTAWAY_ACCOUNT_ID` و`HOSTAWAY_API_SECRET` بواسطة
   `POST /v1/accessTokens` فقط.
3. يخزن الرمز في Django Cache مع هامش قبل انتهاء `expires_in`.
4. عند `403` يلغي الرمز المخزن، وينشئ رمزًا جديدًا مرة واحدة ويعيد GET مرة
   واحدة فقط.

في الإنتاج، توليد الرموز يتطلب cache مشتركًا من Redis أو Memcached. إذا كان
المشروع يستخدم cache محليًا لكل process، يجب توفير `HOSTAWAY_ACCESS_TOKEN`
مباشرة؛ يفشل الإعداد بدل إنشاء رمز جديد من كل process. لا تُستخدم
`DatabaseCache` لتخزين رمز Hostaway في هذا التدفق.

أمر التحقق للقراءة فقط:

```powershell
python manage.py verify_hostaway_integration
python manage.py verify_hostaway_integration --listing-limit 3 --show-schema
python manage.py verify_hostaway_integration --listing-id 40160
python manage.py verify_hostaway_integration --include-amenities --include-reviews
python manage.py verify_hostaway_integration --strict --timeout 20
```

الوضع الافتراضي يفحص ثلاث وحدات ولا يكتب في PostgreSQL. لا يحفظ الاستجابة
الخام، ولا ينزل الصور، ولا يعرض روابط الصور أو العناوين الخاصة أو معلومات
الضيف أو أرقام الحجوزات أو الرموز. يعرض أسماء الحقول وأنواعها، أعداد الصور
والمرافق، نطاقات استضافة الصور، والتغييرات المحتملة في عقد Hostaway.

بعد نجاح التحقق:

```powershell
python manage.py sync_hostaway_properties --dry-run --limit 3
python manage.py sync_hostaway_reviews --listing-id 40160 --dry-run
```

كلا الأمرين يجلبان عبر GET فقط في وضع dry-run ولا ينشئان سجلات مزامنة أو وحدات
أو صورًا أو مراجعات.

متغيرات Hostaway المطلوبة في `.env`، بأحد الأسلوبين:

```dotenv
# رمز جاهز:
HOSTAWAY_ACCESS_TOKEN=<local-access-token>

# أو إنشاء رمز مؤقتًا من بيانات التكامل:
HOSTAWAY_ACCOUNT_ID=<account-id>
HOSTAWAY_API_SECRET=<api-secret>

HOSTAWAY_BASE_URL=https://api.hostaway.com/v1
```

لا تضع الأقواس أو القيم النموذجية نفسها، ولا تشارك `.env`.

## الصور والمرافق

- صور Hostaway تبقى روابط مصدر ولا تُحمّل إلى الخادم.
- اختفاء صورة Hostaway يجعلها غير نشطة محليًا ولا يحذف السجل.
- إخفاء الصورة أو نصوصها المحلية لا يتغيران بالمزامنة.
- الصور المحلية لا تُرفع إلى Hostaway ولا تتأثر بالمزامنة.
- يمكن رفع JPEG أو PNG أو WebP فقط، بحد 10 MB وأبعاد وعدد pixels محددين.
- تُفحص محتويات الملف بواسطة Pillow ولا يُعتمد على الامتداد وحده، وSVG غير مسموح.
- مسارات الرفع مولدة ولا تحتوي أجزاء يتحكم بها المستخدم.
- `ImageField` يبقي طبقة التخزين قابلة للاستبدال لاحقًا بـ S3 أو Cloudflare R2.
- يُستخدم `sort_order` حاليًا؛ السحب والإفلات مؤجل للوحة تحكم مخصصة.
- حذف صور Hostaway ممنوع من الإدارة. حذف الصورة المحلية يستخدم صفحة تأكيد Django.
- المرفق القادم من Hostaway يمكن إخفاؤه، ولا تعدل المزامنة الاسم العربي أو بيانات
  التصنيف والأيقونة المحلية.

يجب أن يكون `MEDIA_ROOT` قابلًا للكتابة من عملية Django في التطوير، ويُخدم عبر
`MEDIA_URL`. في الإنتاج يجب تقديم ملفات media من تخزين كائنات أو خادم ملفات
مخصص، وليس من Django.

## مزامنة المراجعات

```powershell
python manage.py sync_hostaway_reviews
python manage.py sync_hostaway_reviews --listing-id 40160
python manage.py sync_hostaway_reviews --departure-from 2026-01-01 --departure-to 2026-12-31
python manage.py sync_hostaway_reviews --dry-run
```

تطابق المراجعات الوحدة أولًا بواسطة `Review.listingMapId` مقابل
`Property.hostaway_listing_map_id`. إذا لم توجد نتيجة، تستخدم
`Property.hostaway_listing_id` كـfallback. تبقى المراجعة غير مرتبطة إذا لم توجد
وحدة، ولا تنشأ وحدة جديدة من بيانات مراجعة. يعرض التقرير أعداد الاستراتيجيات
`listing_map_id` و`listing_id_fallback` و`unmatched`.

## الاختبارات والجودة

```powershell
pytest
ruff check .
ruff format --check .
python manage.py check
python manage.py check --deploy --settings=config.settings.production
python manage.py makemigrations --check
```

كل اتصالات Hostaway في الاختبارات mocked ولا تُنفذ طلبات حقيقية.

## الحدود الحالية

نُفذ التحقق اللحظي من السعر والتوافر، وعرض سعر مؤقت، وطلب حجز مبدئي، وحالة
Reservation محلية، وكود إنشاء Hostaway معطل افتراضيًا، ومستقبل Webhook محلي
غير مسجل خارجيًا. لم يُنفذ أي إنشاء أو تعديل حي للحجوزات في Hostaway، ولم يُنفذ
الدفع أو الأسعار المحفوظة أو تقويم دائم في PostgreSQL أو Google Analytics أو
Google Ads أو رفع الصور المحلية إلى Hostaway أو التصميم النهائي. صفحات العرض
تقرأ PostgreSQL المحلي ولا تستدعي Hostaway إلا عند إنشاء العرض وعند إعادة
التحقق قبل إنشاء الطلب المبدئي.

> لا تشارك مفاتيح Hostaway أو رموز الوصول، ولا تضعها في المستودع أو سجلات
> التشغيل. ألغِ أي رمز يُشتبه في تسربه.

## المزامنة التلقائية والنشر (المرحلة السابعة)

صفحات الموقع تقرأ الوحدات والصور والمرافق والمراجعات من PostgreSQL المحلي،
ولا تتصل بـHostaway عند التحميل. التوافر والسعر فقط يعاد التحقق منهما مباشرة
وبـCache قصير عند طلب الزائر.

أوامر المزامنة اليدوية:

```powershell
python manage.py verify_hostaway_listings
python manage.py sync_hostaway_properties --dry-run
python manage.py sync_hostaway_properties
python manage.py sync_hostaway_reviews --dry-run
python manage.py sync_hostaway_reviews
```

يمكن تثبيت Listing Map ID بعد التحقق منه من مصدر موثق فقط:

```powershell
python manage.py set_verified_hostaway_identifiers `
  --listing-id <LISTING_ID> `
  --listing-map-id <VERIFIED_LISTING_MAP_ID>
```

لا يستنتج الأمر المعرف ولا يستبدل قيمة مختلفة. يبقى
`HOSTAWAY_DIRECT_CHANNEL_ID` في `.env` المحلي فقط.

### سياسة النشر

تبدأ الوحدة الجديدة بـ`visibility_management=automatic`. إذا كان
`HOSTAWAY_AUTO_PUBLISH_NEW_LISTINGS=True` فلن تظهر إلا عندما تكون نشطة وغير
مؤرشفة، ولها اسم وصورة ظاهرة وسعة موجبة وعملة صحيحة ومعرف Listing صالح.
يمكن جعل المدينة شرطًا أيضًا. تحفظ أسباب عدم النشر كرموز منقحة للمراجعة
الإدارية.

أي تغيير يدوي لـ`is_visible` يحول الإدارة إلى `manual`، ولذلك لا تعيد
المزامنة إظهار وحدة أخفاها المسؤول. المحتوى المحلي وSEO وترتيب العرض لا
تستبدلها المزامنة. الوحدة المؤرشفة لا تحذف، لكنها تصبح غير نشطة ومخفية.
الوحدة الغائبة لا تتغير بعد تشغيل واحد؛ بعد غيابها في عمليتي مزامنة كاملتين
ناجحتين متتاليتين توسم `source_missing` وتخفى مع الاحتفاظ بتاريخها وصورها
ومراجعاتها.

### Celery وRedis

التشغيل التلقائي مغلق افتراضيًا. اضبط `REDIS_URL` و`CACHE_URL` قبل تفعيل
`HOSTAWAY_AUTO_SYNC_ENABLED`. ابدأ العامل والجدولة في عمليتين مستقلتين:

```powershell
celery -A config worker --loglevel=INFO
celery -A config beat --loglevel=INFO
```

الجدول الافتراضي عند التفعيل:

- الوحدات كل 5 دقائق.
- المراجعات كل 15 دقيقة.
- معالجة أحداث Webhook المحلية كل دقيقة، وتظل غير فعالة ما دام
  `HOSTAWAY_WEBHOOK_PROCESSING_ENABLED=False`.
- انتهاء عروض السعر والطلبات كل 5 دقائق.

كل فترة قابلة للضبط من البيئة. تستخدم المهام قفلًا عبر Django Cache؛ لذلك
يجب استخدام Redis Cache مشتركًا عند تعدد العمال. `LocMemCache` مناسب للتطوير
بعملية واحدة فقط. لا تحتوي مفاتيح Cache على Tokens أو بيانات ضيف، ولا يعد
Cache مرجعًا نهائيًا للتوافر أو السعر.

تعرض لوحة **سجل المزامنة** في Django Admin رابط **حالة التكامل والمزامنة**.
الصفحة محلية للقراءة فقط وتعرض آخر تشغيل، الوحدات المعلقة والمخفية
والمؤرشفة، وحالة إعداد Redis وCelery والـFeature Flags دون أسرار. أزرار
المزامنة تتطلب Superuser وPOST مع CSRF. وعندما لا يكون عامل المهام مفعّلًا،
تعرض أمر التشغيل بدل تنفيذ اتصال طويل داخل HTTP.

إعدادات البيئة الجديدة موثقة في `.env.example`. عوائق الإنتاج الحالية تشمل
إعداد Redis وعامل Celery وBeat ومراقبتها. لا تزال بوابة الدفع وعمليات كتابة
الحجز وWebhooks الحقيقية غير مفعلة.

## واجهة المرحلة الثامنة

تستخدم الواجهة العامة مكونات Django Templates قابلة لإعادة الاستخدام، ونظام
Design Tokens موحدًا في `static/css/site.css`، وJavaScript صغيرًا دون إطار
واجهة في `static/js/site.js`. التصميم متجاوب للجوال والجهاز اللوحي وسطح
المكتب، ويدعم لوحة المفاتيح وحالات التركيز الواضحة و`prefers-reduced-motion`.

العربية هي اللغة الافتراضية باتجاه RTL حقيقي، والإنجليزية تستخدم LTR. توجد
كتالوجات الترجمة تحت `locale/ar` و`locale/en`. عند توفر GNU gettext يمكن
تحديثها وتجميعها بالأوامر:

```powershell
.\.venv\Scripts\python.exe manage.py makemessages -l ar -l en
.\.venv\Scripts\python.exe manage.py compilemessages
```

يدير `SitePage` و`FAQItem` و`SiteSetting` محتوى الصفحات العامة دون CMS معقد.
يخزن نموذج التواصل الرسائل محليًا في `ContactMessage` مع CSRF وHoneypot وحدود
طول وتنقية HTML وRate Limit عبر Cache. لا يرسل بريدًا حتى يضبط مزود بريد
صراحة. ما يزال مطلوبًا من العميل اعتماد بيانات الهاتف والبريد والشبكات
الاجتماعية والنصوص القانونية والمحتوى التسويقي النهائي لكل وحدة.

يتضمن أساس SEO عناوين ووصفًا ديناميكيًا، Canonical، وOpen Graph وTwitter
Cards، وBreadcrumbs، وStructured Data للمنظمة والوحدات. صفحات عرض السعر
وبيانات الضيف والطلبات الخاصة مضبوطة على `noindex`. لم تبدأ أدوات Google أو
النشر الإنتاجي.

تقرأ صفحات الوحدات من PostgreSQL المحلي ولا تتصل بـHostaway عند تحميلها.
يحدث التحقق المباشر فقط بعد إرسال التواريخ. عرض السعر مؤقت، والدفع والحجز
الحي غير مفعّلين، ولا تنشئ زيارة الواجهة `Reservation` أو `PaymentAttempt`.

فحوصات الواجهة:

```powershell
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe manage.py check
```

## البريد والإشعارات والتشغيل (المرحلة التاسعة)

طبقة البريد مستقلة عن مزود الإرسال. يستخدم التطوير
`django.core.mail.backends.console.EmailBackend` افتراضيًا، ويبقى
`EMAIL_DELIVERY_ENABLED=False` حتى يضبط مزود إنتاجي صراحة. يمنع فحص النظام
تفعيل TLS وSSL معًا، ويرفض إعداد إنتاج مفعّلًا دون مرسل ومزود مكتمل. لا تخزن
قاعدة البيانات جسم البريد أو Headers أو عنوان المستلم كاملًا؛ يحتفظ
`EmailDelivery` ببصمة HMAC، وعنوان مخفي، ومرجع محلي موثوق لحل المستلم عند
الإرسال.

توجد قوالب HTML ونص عادي بالعربية والإنجليزية للتواصل، وطلبات الحجز
المبدئية، وتغير السعر والتوافر، وطلبات التعديل والإلغاء، والحالات المستقبلية
للدفع والحجز. القوالب المستقبلية لا تستخدم إلا بعد إثبات حالة الدفع ووجود
Hostaway Reservation ID. لا توجد خدمة WhatsApp فعلية في هذه المرحلة.

ينشئ Event Dispatcher صريح إشعارات إدارية منقحة، ويمكنه إضافة رسالة إلى
الطابور وفق Feature Flags النوعية. فشل الإشعار الثانوي لا يفشل العملية
الأساسية، ومعرفات idempotency تمنع التكرار. المهام المتاحة:

- `send_email_delivery_task`
- `process_email_queue_task`
- `cleanup_expired_notifications_task`
- `send_daily_operations_summary_task`

جدولة البريد مغلقة عبر `EMAIL_TASK_SCHEDULE_ENABLED=False`. عند تفعيلها
إنتاجيًا يلزم Redis، وتعالج الطابور كل دقيقة، وتنظف الإشعارات يوميًا، وترسل
ملخص العمليات الساعة 08:00 بتوقيت الرياض. لا تنهار صفحات الموقع عند غياب
Redis ما دامت الجدولة معطلة.

توفر لوحة الإدارة:

- مركز إشعارات مع فلاتر وحالة قراءة.
- لوحة تقارير تجمع البيانات من PostgreSQL فقط.
- تصدير CSV منقح مع UTF-8 BOM وحماية CSV Injection وإخفاء PII افتراضيًا.
- سجل Audit غير قابل للتعديل من الإدارة، ولا يخزن IP خامًا أو payloads.
- صفحة حالة للنظام دون أسرار أو اتصال حي بـHostaway.

المسارات العامة السريعة:

```text
/health/live/
/health/ready/
```

يتحقق `live` من عمل Django فقط. يتحقق `ready` من قاعدة البيانات وCache
والمigrations، ويعيد 503 عند تعطل اعتماد ضروري دون كشف عناوين الخوادم أو
متغيرات الاتصال.

سياسة الاحتفاظ الافتراضية: رسائل التواصل 365 يومًا، سجلات تسليم البريد
والإشعارات 180 يومًا، وسجل التدقيق 730 يومًا. راجع الأعداد أولًا:

```powershell
python manage.py purge_expired_operational_data --dry-run
python manage.py purge_expired_operational_data --dry-run --model contacts
python manage.py purge_expired_operational_data --dry-run --before 2026-01-01 --limit 500
```

بعد المراجعة يمكن تشغيل الأمر دون `--dry-run`. لا يشمل الأمر الحجوزات
المؤكدة أو سجلات الدفع، ولا يحذف Audit Log قبل مدة الاحتفاظ.

عوائق الإنتاج الحالية: اختيار مزود بريد واعتماد بيانات SMTP أو API، وضبط
عناوين المرسل والدعم والعمليات، وتشغيل Redis وCelery ومراقبتهما، واعتماد
سياسة خصوصية واحتفاظ نهائية. لا توجد بوابة دفع أو حجز حي أو Webhooks حقيقية
أو Google Analytics في هذه المرحلة.
