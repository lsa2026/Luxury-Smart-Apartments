from django.http import HttpRequest, HttpResponse
from django.utils import translation
from django.views.generic import TemplateView

from apps.properties.models import Property
from apps.properties.trustindex import full_review_widget_id

GENERAL_WIDGETS = {
    "ar": "cf56bfb80db642820b86b5cdc90",
    "en": "018647b80f89428a137637d5e0f",
    "fr": "018647b80f89428a137637d5e0f",
}


class ReviewListView(TemplateView):
    template_name = "reviews/review_list.html"

    def dispatch(self, request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        request._trustindex_widget_enabled = True
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        language = (translation.get_language() or "ar").split("-")[0]
        properties = tuple(Property.objects.public().order_by("city", "id"))
        requested_slug = self.request.GET.get("property", "").strip()
        selected = next((item for item in properties if item.slug == requested_slug), None)
        selected_widget = full_review_widget_id(selected, language) if selected else ""
        context.update(
            {
                "review_properties": properties,
                "selected_review_property": selected,
                "selected_review_widget_id": selected_widget,
                "trustindex_widget_id": selected_widget
                or GENERAL_WIDGETS.get(language, GENERAL_WIDGETS["en"]),
            }
        )
        return context
