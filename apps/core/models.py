"""Small bilingual content layer and privacy-safe contact inbox."""

from django.core.validators import MaxLengthValidator
from django.db import models


class SitePage(models.Model):
    slug = models.SlugField(max_length=80, unique=True)
    title_ar = models.CharField(max_length=200)
    title_en = models.CharField(max_length=200)
    body_ar = models.TextField(blank=True)
    body_en = models.TextField(blank=True)
    meta_description_ar = models.CharField(max_length=320, blank=True)
    meta_description_en = models.CharField(max_length=320, blank=True)
    is_published = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]
        verbose_name = "صفحة محتوى"
        verbose_name_plural = "صفحات المحتوى"

    def __str__(self) -> str:
        return self.title_ar or self.title_en


class FAQItem(models.Model):
    question_ar = models.CharField(max_length=300)
    question_en = models.CharField(max_length=300)
    answer_ar = models.TextField()
    answer_en = models.TextField()
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [models.Index(fields=["is_active", "sort_order"])]
        verbose_name = "سؤال شائع"
        verbose_name_plural = "الأسئلة الشائعة"

    def __str__(self) -> str:
        return self.question_ar or self.question_en


class SiteSetting(models.Model):
    site_name = models.CharField(max_length=120, default="Luxury Smart Apartments")
    tagline_ar = models.CharField(max_length=240, blank=True)
    tagline_en = models.CharField(max_length=240, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=30, blank=True)
    whatsapp_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    office_hours_ar = models.CharField(max_length=200, blank=True)
    office_hours_en = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "إعداد الموقع"
        verbose_name_plural = "إعدادات الموقع"

    def __str__(self) -> str:
        return self.site_name


class ContactMessage(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "جديدة"
        IN_PROGRESS = "in_progress", "قيد المتابعة"
        CLOSED = "closed", "مغلقة"
        SPAM = "spam", "مزعجة"

    name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    subject = models.CharField(max_length=200)
    message = models.TextField(validators=[MaxLengthValidator(2000)])
    language = models.CharField(max_length=10, choices=(("ar", "العربية"), ("en", "English")))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["-created_at"]),
        ]
        verbose_name = "رسالة تواصل"
        verbose_name_plural = "رسائل التواصل"

    def __str__(self) -> str:
        return f"{self.subject} — {self.created_at:%Y-%m-%d}"
