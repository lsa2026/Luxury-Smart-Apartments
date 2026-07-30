from django.views.generic import ListView

from .models import Review, ReviewQuerySet


class ReviewListView(ListView):
    template_name = "reviews/review_list.html"
    context_object_name = "reviews"
    paginate_by = 10

    def get_queryset(self) -> ReviewQuerySet:
        return Review.objects.public().select_related("property")
