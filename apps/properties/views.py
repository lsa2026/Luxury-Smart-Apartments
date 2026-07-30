from django.db.models import Prefetch, QuerySet
from django.views.generic import DetailView, ListView

from apps.reservations.forms import AvailabilitySearchForm
from apps.reviews.models import Review

from .models import Property, PropertyAmenity, PropertyImage


class PropertyListView(ListView):
    template_name = "properties/property_list.html"
    context_object_name = "properties"
    paginate_by = 9

    def get_queryset(self) -> QuerySet[Property]:
        return Property.objects.public().prefetch_related(
            Prefetch(
                "images",
                queryset=PropertyImage.objects.public(),
                to_attr="_public_images",
            )
        )


class PropertyDetailView(DetailView):
    template_name = "properties/property_detail.html"
    context_object_name = "property"
    slug_url_kwarg = "slug"

    def get_queryset(self) -> QuerySet[Property]:
        return Property.objects.public().prefetch_related(
            Prefetch(
                "images",
                queryset=PropertyImage.objects.public(),
                to_attr="_public_images",
            ),
            Prefetch(
                "property_amenities",
                queryset=PropertyAmenity.objects.filter(
                    is_visible=True,
                    is_active_at_source=True,
                    amenity__is_active=True,
                ).select_related("amenity"),
                to_attr="_public_amenities",
            ),
            Prefetch(
                "reviews",
                queryset=Review.objects.public().select_related("property"),
                to_attr="_public_reviews",
            ),
        )

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        property_obj = self.object
        context["gallery_images"] = property_obj._public_images
        context["visible_amenities"] = property_obj._public_amenities
        context["property_reviews"] = property_obj._public_reviews
        context["availability_form"] = AvailabilitySearchForm(property_obj=property_obj)
        return context
