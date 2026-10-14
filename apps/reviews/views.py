from django.http import HttpRequest, HttpResponse
from django.utils import translation
from django.views.generic import TemplateView

from apps.properties.models import Property
from apps.properties.trustindex import full_review_widget_id

REVIEW_EXPLORER_HINTS = {
    "ar": (
        "اختر الوحدة، ثم استخدم ألسنة Google أو Booking.com أو Airbnb أدناه "
        "للتركيز على منصة مراجعات محددة."
    ),
    "en": (
        "Select a property, then use the Google, Booking.com or Airbnb tabs below "
        "to focus on one review source."
    ),
    "fr": (
        "Choisissez un hébergement, puis utilisez les onglets Google, Booking.com "
        "ou Airbnb ci-dessous pour consulter une source d’avis précise."
    ),
}


class ReviewListView(TemplateView):
    template_name = "reviews/review_list.html"

    def dispatch(
        self,
        request: HttpRequest,
        *args: object,
        **kwargs: object,
    ) -> HttpResponse:
        request._trustindex_widget_enabled = True
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        language = (translation.get_language() or "ar").split("-")[0]
        # Include every public stay, even if its Trustindex sources are not
        # ready yet. Selecting one without a full-review widget leads to a
        # clear preparation notice rather than silently hiding the stay.
        review_properties = tuple(Property.objects.public().order_by("city", "id"))
        requested_slug = self.request.GET.get("property", "").strip()
        selected_property = next(
            (
                property_obj
                for property_obj in review_properties
                if property_obj.slug == requested_slug
            ),
            None,
        )
        selected_widget_id = (
            full_review_widget_id(selected_property, language)
            if selected_property is not None
            else ""
        )
        # A combined widget is capped across all properties. Use each stay's
        # complete list so the all-properties page cannot silently truncate it.
        review_sections = tuple(
            {
                "property": property_obj,
                "widget_id": full_review_widget_id(property_obj, language),
            }
            for property_obj in review_properties
            if selected_property is None or property_obj.pk == selected_property.pk
        )
        context.update(
            {
                "review_properties": review_properties,
                "selected_review_property": selected_property,
                "selected_review_widget_id": selected_widget_id,
                "review_explorer_hint": REVIEW_EXPLORER_HINTS.get(
                    language,
                    REVIEW_EXPLORER_HINTS["en"],
                ),
                "review_sections": review_sections,
            }
        )
        return context
