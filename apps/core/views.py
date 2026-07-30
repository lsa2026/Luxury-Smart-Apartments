from django.views.generic import TemplateView

from apps.reviews.models import Review


class HomeView(TemplateView):
    template_name = "core/home.html"

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        context["featured_reviews"] = Review.objects.public().select_related("property")[:6]
        return context
