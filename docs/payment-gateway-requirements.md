# متطلبات ربط بوابة الدفع (HyperPay)

مرجع داخلي + مسودة الرد الرسمي على إيميل Dalia Khammash (HyperPay).

---

## 1. الخلاصة الداخلية

المشروع **تطبيق Django مخصّص** وليس متجرًا على منصة جاهزة، لذلك **لا يوجد
"rebuild" مطلوب**؛ ما نحتاجه هو تكامل API مباشر.

| البند | القيمة |
|---|---|
| التقنية | Django 5.2 / Python 3.13 / PostgreSQL 15+ |
| المهام الخلفية | Celery + Redis |
| اللغات | العربية (افتراضي) + English + Français |
| المنطقة الزمنية | Asia/Riyadh |
| النشاط | تأجير شقق يومي/شهري، المخزون متزامن مع Hostaway |

### حالة الدفع في الكود

- `apps/payments/models.py` — `PaymentAttempt` محايد تجاه المزوّد، فيه
  `provider` و`provider_reference` و`amount` و`currency` و`idempotency_key`
  فريد، وحالات كاملة تشمل `refunded` و`partially_refunded`.
- `apps/payments/providers.py` — `PaymentProvider` Protocol جاهز، وحاليًا
  `DisabledPaymentProvider` يرمي `payment_provider_not_configured`.
- `apps/reservations/services/hostaway_booking.py` — لا يُنشأ حجز في Hostaway
  إلا بعد التحقق من وجود `PaymentAttempt` ناجحة، وإلا يُرفض بالرمز
  `successful_payment_required`.

### حالة تنفيذ TEST/UAT

تم تنفيذ COPYandPAY في مسار خدمة HyperPay مستقل فوق `PaymentAttempt`، مع إنشاء
checkout والتحقق الخادمي من النتيجة وصفحات الدفع والعودة وCSP محدود بالمسار.
التفاصيل التشغيلية وقائمة UAT موجودة في `docs/hyperpay-test-uat.md`.

لم يُضف webhook لأن مواصفات التوقيع لم تصل بعد، ولا يُعفى أي endpoint من CSRF.
Apple Pay وProduction والاسترداد الآلي خارج نطاق هذه المرحلة.

### قيود يجب أن يعرفها المزوّد

- CSP صارم قائم على nonce، بلا `unsafe-inline` وبلا `unsafe-eval`،
  و`frame-src 'self'` حاليًا.
- لا نخزّن بيانات بطاقات إطلاقًا — الهدف البقاء ضمن PCI-DSS SAQ-A.
- العملة ليست ثابتة: تُقرأ من Hostaway لكل وحدة كرمز ISO من ثلاثة أحرف،
  وأي فرق في السعر مرفوض (`BOOKING_PRICE_TOLERANCE=0.00`).

---

## 2. مسودة الرد (للإرسال كما هي)

**Subject:** Re: Payment Gateway Integration — Luxury Smart Apartments

---

Dear Dalia,

Thank you for your email and for the offer to assist.

To clarify one point first: our project is not built on a hosted e-commerce
platform such as Shopify, WooCommerce or Magento, so no rebuild is required on
our side. It is a custom web application developed in-house using Python/Django
with a PostgreSQL database, running on our own infrastructure. What we need is
therefore a direct API integration with HyperPay rather than a ready-made plugin.

Brief technical context:

- Backend: Python 3.13 / Django 5.2 with PostgreSQL, server-side rendered pages.
- Business: short and long term apartment rentals, with inventory synchronised
  from Hostaway.
- Languages: Arabic, English and French. Timezone: Asia/Riyadh.
- Booking flow: availability check -> server-side price quote -> guest details
  -> booking intent (awaiting payment) -> payment -> booking confirmation.
- Our payment layer is already built and provider-neutral; the gateway is the
  only missing component. A booking is confirmed only after a verified
  successful payment.
- We do not store or process card data on our servers and intend to keep it
  that way, so we expect to remain within PCI-DSS SAQ-A scope.

To move forward, could you please provide the following:

**1. Integration**

1.1 Which integration mode do you recommend for a custom backend of this type,
    COPYandPAY (hosted widget) or Server-to-Server, and the complete technical
    documentation for it.
1.2 Sandbox credentials (entity ID and access token), the test environment base
    URL, and test card numbers covering success, failure and 3-D Secure cases.
1.3 Any official SDK or reference implementation. If none is available for
    Python, we will integrate directly over HTTPS.
1.4 The 3-D Secure 2 flow, and the exact return and callback URLs we need to
    register with you.

**2. Notifications and reliability**

2.1 The webhook specification: payload structure, the exact signature
    verification method (algorithm, secret, and which fields are signed), the
    retry policy, and the source IP ranges we should allow.
2.2 How duplicate charges are prevented. Do you accept a merchant-supplied
    idempotency key or a unique merchant transaction reference?
2.3 The APIs for querying payment status and for issuing full and partial
    refunds.
2.4 Whether we must whitelist our server IP addresses with you, and whether you
    require a static outbound IP from our side.

**3. Security requirements**

3.1 Our site enforces a strict nonce-based Content-Security-Policy with no
    'unsafe-inline' and no 'unsafe-eval', and we do not plan to relax it. If you
    recommend the hosted widget, please send the exact domains we must allow for
    script-src, frame-src, connect-src and img-src.
3.2 Any compliance steps expected from us: PCI-DSS SAQ level, a security
    questionnaire, or penetration test evidence.

**4. Payment methods and currencies**

4.1 Which payment methods are available to us (mada, Visa, Mastercard, Apple
    Pay, STC Pay, Tabby, Tamara), and whether each requires a separate entity ID.
4.2 Which settlement currencies you support, and whether more than one currency
    can be processed under a single entity ID. Our units are priced in different
    ISO currencies, so this affects our configuration.

**5. Commercial and account opening**

5.1 The full list of documents required to open a merchant account.
5.2 Pricing: per-transaction fees by payment method, setup fee, any recurring
    fees, and refund and chargeback fees.
5.3 The settlement cycle, and the reporting or reconciliation tools available
    to us.

**6. Timeline and support**

6.1 Expected time from submitting the documents to receiving sandbox access,
    and from sandbox to production.
6.2 The technical contact and escalation channel we should use during the
    integration.

Once we have the documentation and sandbox credentials we can begin the
integration immediately and share our test results with you.

Best regards,

[Your name]
Luxury Smart Apartments
