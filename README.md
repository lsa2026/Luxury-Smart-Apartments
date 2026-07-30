# Luxury Smart Apartments

منصة Django عربية لحجز وحدات التأجير اليومي والشهري. تشمل المرحلتان الحاليتان
الأساس الإنتاجي، مزامنة وحدات Hostaway وصورها ومرافقها، طبقة محتوى محلية، ومزامنة
المراجعات المنشورة من نوع `guest-to-host`.

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
- `/admin/`: إدارة المحتوى والصور.

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

لم تُنفذ بعد الأسعار اللحظية أو التوافر أو إنشاء وتعديل الحجوزات أو الدفع أو
Google Analytics أو Google Ads أو رفع الصور المحلية إلى Hostaway أو التصميم
النهائي. صفحات الموقع تقرأ PostgreSQL المحلي ولا تستدعي Hostaway عند الزيارة.

> لا تشارك مفاتيح Hostaway أو رموز الوصول، ولا تضعها في المستودع أو سجلات
> التشغيل. ألغِ أي رمز يُشتبه في تسربه.
