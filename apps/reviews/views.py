from django.http import HttpRequest, HttpResponse
from django.utils import translation
from django.views.generic import TemplateView

from apps.properties.models import Property
from apps.properties.trustindex import full_review_widget_id

GENERAL_WIDGETS = {
    "ar": "cf56bfb80db642820b86b5cdc90",
    "en": "018647b80f89428a137637d5e0f",
    # The existing account has no French general widget yet. English review
    # text can still be translated by Trustindex inside this approved widget.
    "fr": "018647b80f89428a137637d5e0f",
}

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
        widget_id = selected_widget_id or GENERAL_WIDGETS.get(
            language,
            GENERAL_WIDGETS["en"],
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
                "trustindex_widget_id": widget_id,
            }
        )
        return context
