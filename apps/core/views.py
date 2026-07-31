"""Public marketing and content views backed by local PostgreSQL data."""

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import translation
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from apps.properties.models import Property, PropertyImage
from apps.reservations.forms import AvailabilitySearchForm
from apps.reservations.security import is_rate_limited
from apps.reviews.models import Review

from .forms import ContactForm
from .models import ContactMessage, FAQItem, SitePage


def _card_image_queryset() -> object:
    return PropertyImage.objects.public().order_by(
        "-is_cover",
        "sort_order",
        "hostaway_sort_order",
        "id",
    )


class HomeView(TemplateView):
    template_name = "core/home.html"

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        featured_properties = list(
            Property.objects.public()
            .prefetch_related(
                Prefetch(
                    "images",
                    queryset=_card_image_queryset()[:1],
                    to_attr="_public_images",
                )
            )
            .order_by("-is_featured", "sort_order", "id")[:6]
        )
        city_rows = (
            Property.objects.public()
            .exclude(city="")
            .values("city", "city_ar", "city_en", "city_fr")
            .distinct()
            .order_by("city")
        )
        context.update(
            {
                "featured_properties": featured_properties,
                "hero_property": featured_properties[0] if featured_properties else None,
                "featured_reviews": Review.objects.public()
                .select_related("property")
                .order_by("-is_featured", "-departure_date")[:3],
                "cities": list(city_rows),
                "availability_form": AvailabilitySearchForm(),
            }
        )
        return context


class ContentPageView(TemplateView):
    template_name = "core/content_page.html"
    page_slug = ""

    def get_template_names(self) -> list[str]:
        template_map = {
            "terms": "legal/terms.html",
            "privacy": "legal/privacy.html",
            "cancellation": "legal/cancellation_policy.html",
            "cookies": "legal/cookies.html",
        }
        return [template_map.get(self.page_slug, self.template_name)]

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        context["page"] = get_object_or_404(
            SitePage,
            slug=self.page_slug,
            is_published=True,
        )
        return context


class FAQView(TemplateView):
    template_name = "core/faq.html"

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        context["faq_items"] = FAQItem.objects.filter(is_active=True)
        return context


class ContactView(View):
    http_method_names = ["get", "post"]

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "core/contact.html", {"contact_form": ContactForm()})

    def post(self, request: HttpRequest) -> HttpResponse:
        if is_rate_limited(
            request,
            scope="contact",
            requests=settings.CONTACT_RATE_LIMIT_REQUESTS,
            window=settings.CONTACT_RATE_LIMIT_WINDOW,
        ):
            return render(
                request,
                "core/contact.html",
                {
                    "contact_form": ContactForm(request.POST),
                    "rate_limited": True,
                },
                status=429,
            )
        form = ContactForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "core/contact.html",
                {"contact_form": form},
                status=400,
            )
        with transaction.atomic():
            contact_message = ContactMessage.objects.create(
                name=form.cleaned_data["name"],
                email=form.cleaned_data["email"],
                phone=form.cleaned_data["phone"],
                subject=form.cleaned_data["subject"],
                message=form.cleaned_data["message"],
                language=translation.get_language() or "ar",
            )
            from apps.notifications.services.events import handle_contact_created

            transaction.on_commit(
                lambda: handle_contact_created(
                    contact_message.pk,
                    email=contact_message.email,
                    language=contact_message.language,
                )
            )
        messages.success(request, _("Your message has been received."))
        request.session["analytics_event"] = "generate_lead"
        return redirect("core:contact")


def error_400(request: HttpRequest, exception: Exception) -> HttpResponse:
    request._disable_google_integrations = True
    return render(request, "errors/400.html", {"error_code": 400}, status=400)


def error_403(request: HttpRequest, exception: Exception) -> HttpResponse:
    request._disable_google_integrations = True
    return render(request, "errors/403.html", {"error_code": 403}, status=403)


def error_404(request: HttpRequest, exception: Exception) -> HttpResponse:
    request._disable_google_integrations = True
    return render(request, "errors/404.html", {"error_code": 404}, status=404)


def error_500(request: HttpRequest) -> HttpResponse:
    request._disable_google_integrations = True
    return render(request, "errors/500.html", {"error_code": 500}, status=500)


def error_429(request: HttpRequest) -> HttpResponse:
    request._disable_google_integrations = True
    return render(request, "errors/429.html", {"error_code": 429}, status=429)


def error_503(request: HttpRequest) -> HttpResponse:
    request._disable_google_integrations = True
    return render(request, "errors/503.html", {"error_code": 503}, status=503)
