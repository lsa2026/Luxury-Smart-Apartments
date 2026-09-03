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

SITE_BASE_URL=https://your-public-domain.example
EMAIL_LOGO_URL=https://your-public-domain.example/static/images/logo.jpeg
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
