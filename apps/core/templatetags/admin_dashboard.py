from django import template

from apps.core.admin_dashboard import dashboard_payload

register = template.Library()

STATUS_LABELS_AR = {
    "active": "نشط",
    "available": "متاح",
    "awaiting_customer_approval": "بانتظار موافقة العميل",
    "awaiting_payment": "بانتظار الدفع",
    "blocked": "محظور",
    "cancelled": "ملغى",
    "completed": "مكتمل",
    "confirmed": "مؤكد",
    "configured": "مهيأ",
    "consumed": "مستخدم",
    "created": "تم الإنشاء",
    "disabled": "مغلق",
    "dispatch_disabled": "إرسال المهام مغلق",
    "draft": "مسودة",
    "enabled": "مفعّل",
    "expired": "منتهي",
    "failed": "فشل",
    "invalidated": "ملغى",
    "modified": "معدّل",
    "not_configured": "غير مهيأ",
    "pending": "قيد الانتظار",
    "pending_admin_approval": "بانتظار موافقة الإدارة",
    "pending_revalidation": "بانتظار إعادة التحقق",
    "price_changed": "تغيّر السعر",
    "processing": "قيد التنفيذ",
    "ready_for_hostaway": "جاهز لـ Hostaway",
    "rejected": "مرفوض",
    "sent": "تم الإرسال",
    "succeeded": "ناجح",
    "unavailable": "غير متاح",
    "unknown": "غير مؤكّد",
}

ADMIN_LABELS_AR = {
    "action checkbox": "تحديد",
    "amount": "المبلغ",
    "attempt count": "عدد المحاولات",
    "booking intent": "طلب الحجز",
    "check in": "الوصول",
    "check out": "المغادرة",
    "completed at": "وقت الاكتمال",
    "created at": "وقت الإنشاء",
    "currency": "العملة",
    "dry run": "معاينة جافة",
    "error code": "رمز الخطأ",
    "event type": "نوع الحدث",
    "expires at": "ينتهي في",
    "failed count": "عدد الفشل",
    "fetched count": "تم جلبها",
    "guest name": "اسم الضيف",
    "guests": "الضيوف",
    "hostaway reservation id": "رقم حجز Hostaway",
    "hostaway status": "حالة Hostaway",
    "is test": "حجز اختباري",
    "language": "اللغة",
    "last synced at": "آخر مزامنة",
    "modification request": "طلب التعديل",
    "new check in": "الوصول الجديد",
    "new check out": "المغادرة الجديدة",
    "normalized status": "حالة الحجز",
    "old check in": "الوصول السابق",
    "old check out": "المغادرة السابقة",
    "operation type": "نوع العملية",
    "payment status": "حالة الدفع",
    "price difference": "فرق السعر",
    "processed at": "وقت المعالجة",
    "provider": "بوابة الدفع",
    "provider reference": "مرجع بوابة الدفع",
    "public reference": "المرجع",
    "rating": "التقييم",
    "received at": "وقت الاستلام",
    "recipient masked": "المستلم المنقح",
    "request type": "نوع الطلب",
    "requested at": "وقت الطلب",
    "reservation": "الحجز",
    "source type": "مصدر الحجز",
    "started at": "وقت البدء",
    "status": "الحالة",
    "sync type": "نوع المزامنة",
    "total price": "الإجمالي",
    "updated at": "آخر تحديث",
    "live_booking": "إنشاء الحجوزات الحية",
    "live_modification": "تعديل الحجوزات الحية",
    "live_extension": "تمديد الحجوزات الحية",
    "live_cancellation": "إلغاء الحجوزات الحية",
    "webhook_receiver": "استقبال Webhooks",
    "webhook_processing": "معالجة Webhooks",
}


@register.simple_tag(takes_context=True)
def luxury_dashboard(context: template.Context) -> dict[str, object]:
    return dashboard_payload(context["request"])


@register.filter
def admin_status_label(value: object) -> str:
    """Render internal workflow codes as concise Arabic labels."""
    normalized = str(value or "").strip()
    return STATUS_LABELS_AR.get(normalized, normalized.replace("_", " "))


@register.filter
def admin_label(value: object) -> str:
    """Translate common technical Django Admin labels without mutating model state."""
    normalized = str(value or "").strip()
    return ADMIN_LABELS_AR.get(normalized.casefold(), normalized)
