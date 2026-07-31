from django.urls import path

from .seo import robots_txt, sitemap_xml
from .views import ContactView, ContentPageView, FAQView, HomeView

app_name = "core"

urlpatterns = [
    path("", HomeView.as_view(), name="home"),
    path("sitemap.xml", sitemap_xml, name="sitemap"),
    path("robots.txt", robots_txt, name="robots"),
    path(
        "about/",
        ContentPageView.as_view(page_slug="about"),
        name="about",
    ),
    path("faq/", FAQView.as_view(), name="faq"),
    path("contact/", ContactView.as_view(), name="contact"),
    path(
        "legal/terms/",
        ContentPageView.as_view(page_slug="terms"),
        name="terms",
    ),
    path(
        "legal/privacy/",
        ContentPageView.as_view(page_slug="privacy"),
        name="privacy",
    ),
    path(
        "legal/cancellation/",
        ContentPageView.as_view(page_slug="cancellation"),
        name="cancellation",
    ),
    path(
        "legal/cookies/",
        ContentPageView.as_view(page_slug="cookies"),
        name="cookies",
    ),
]
