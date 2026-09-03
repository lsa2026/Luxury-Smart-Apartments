# تشغيل بريد العملاء

يدعم النظام رسائل الحجز والتعديل بالعربية والإنجليزية والفرنسية. يتم إنشاء الرسالة
مرة واحدة فقط بعد حفظ الحدث، وتُرسل من طابور البريد دون تعطيل عملية الحجز الأساسية.

## إعدادات الإنتاج

اضبط القيم التالية في بيئة الخادم، ولا تحفظ كلمة مرور SMTP في المستودع:

```env
EMAIL_DELIVERY_ENABLED=True
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
DEFAULT_FROM_EMAIL=Luxury Smart Apartments <bookings@your-domain.example>
SUPPORT_EMAIL=care@your-domain.example
EMAIL_HOST=smtp.your-provider.example
EMAIL_PORT=587
EMAIL_HOST_USER=bookings@your-domain.example
EMAIL_HOST_PASSWORD=replace-in-secret-manager
EMAIL_USE_TLS=True
EMAIL_USE_SSL=False

BOOKING_NOTIFICATION_EMAIL_ENABLED=True
MODIFICATION_NOTIFICATION_EMAIL_ENABLED=True
EMAIL_TASK_SCHEDULE_ENABLED=True

SITE_BASE_URL=https://luxurysmartapartments.com
EMAIL_LOGO_URL=https://luxurysmartapartments.com/static/images/logo.jpeg
EMAIL_CONTACT_PHONE=+966500000000
```

يأخذ البريد اسم المنصة ورقم التواصل أولًا من `إعدادات الموقع` في لوحة الإدارة، ثم
يستخدم قيم البيئة كبديل. إذا كانت `EMAIL_LOGO_URL` فارغة فسيستخدم النظام تلقائيًا
`/static/images/logo.jpeg` على النطاق المحدد في `SITE_BASE_URL`.

## الخدمات المطلوبة

شغّل عامل Celery وجدولة Celery Beat مع Redis. تقوم الجدولة بمعالجة طابور البريد كل
دقيقة، مع إعادة محاولات الأخطاء المؤقتة ضمن الحدود المعرفة في الإعدادات.

قبل الإطلاق:

1. شغّل `python manage.py check` للتأكد من اكتمال إعدادات البريد.
2. تأكد أن رابط الشعار متاح علنًا عبر HTTPS.
3. اختبر الرسائل في اللغات الثلاث على الهاتف وسطح المكتب.
4. فعّل SPF وDKIM وDMARC لنطاق المرسل لدى مزود البريد.
5. راقب `الإشعارات والتشغيل ← تسليم البريد` في لوحة الإدارة.

## الأحداث المرتبطة

- إنشاء طلب حجز مبدئي.
- تأكيد الحجز بعد استلام رقم Hostaway.
- استلام طلب تمديد أو تغيير تواريخ أو ضيوف أو إلغاء.
- نجاح التعديل النهائي بعد مطابقته مع Hostaway.
- نجاح الإلغاء النهائي بعد مطابقته مع Hostaway.

رسالة استلام طلب التعديل لا تدّعي أن الحجز تغير. رسالة النجاح النهائية لا تُنشأ
إلا بعد وصول الحالة المتطابقة من Hostaway، سواء عبر التنفيذ المباشر أو Webhook.

## استئناف مرحلة البريد

البنية جاهزة بالكامل والإرسال معطّل عمداً. عند العودة لهذه المرحلة:

### قبل التفعيل

1. **تأكّدي أن `SITE_BASE_URL` يستجيب.** كل روابط التحقق واستعادة كلمة المرور تُبنى منه في
   `_account_links()` بملف `apps/notifications/services/email.py`. نطاق لا يستجيب يعني روابط
   ميتة في رسائل تصل نزلاء حقيقيين.

2. **تحقّقي من أركان النطاق الأربعة.** كانت جميعها سليمة عند آخر فحص:

   ```
   MX     aspmx.l.google.com
   SPF    v=spf1 include:_spf.google.com ~all
   DKIM   google._domainkey
   DMARC  p=quarantine; adkim=r; aspf=r
   ```

   `p=quarantine` يعني أن أي رسالة تفشل في المحاذاة تذهب للسبام. إن أُضيف مزوّد إرسال آخر
   فادمجي `include` الخاص به في **سجل SPF واحد**؛ سجلان يعنيان `permerror` وسقوط كل البريد.

### التفعيل

اضبطي متغيرات `EMAIL_*` على الخادم ثم `EMAIL_DELIVERY_ENABLED=True`.
`DEFAULT_FROM_EMAIL` يجب أن يطابق `EMAIL_HOST_USER`، وإلا أعاد المزوّد كتابة المُرسِل
وكسر محاذاة DMARC.

### بعد التفعيل

- الرسائل المُدرَجة أثناء التعطيل محفوظة بحالة `disabled` في `EmailDelivery`، وتحمل سجل
  التدقيق كاملاً. أعيدي إدراج ما يلزم منها إن أردتِ.
- أضيفي رابط "نسيت كلمة المرور" إلى `templates/accounts/login.html`. تُرك بلا رابط عمداً حتى
  لا يصل الزائر إلى صفحة تَعِد برسالة لا تصل:

  ```html
  <a href="{% url 'accounts:password_reset' %}">{% trans "Forgot your password?" %}</a>
  ```

- شغّلي `EMAIL_TASK_SCHEDULE_ENABLED=True` ليعالج `lsa-beat` طابور البريد كل دقيقة.
