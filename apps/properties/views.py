from django.db.models import Prefetch, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.generic import DetailView, ListView

from apps.reservations.forms import AvailabilitySearchForm
from apps.reviews.models import Review

from .models import Property, PropertyAmenity, PropertyImage


def _card_images() -> QuerySet[PropertyImage]:
    return PropertyImage.objects.public().order_by(
        "-is_cover",
        "sort_order",
        "hostaway_sort_order",
        "id",
    )


class PropertyListView(ListView):
    template_name = "properties/property_list.html"
    context_object_name = "properties"
    paginate_by = 9

    def get_queryset(self) -> QuerySet[Property]:
        queryset = Property.objects.public().prefetch_related(
            Prefetch(
                "images",
                queryset=_card_images()[:1],
                to_attr="_public_images",
            )
        )
        city = self.request.GET.get("city", "").strip()
        guests = self.request.GET.get("guests", "").strip()
        bedrooms = self.request.GET.get("bedrooms", "").strip()
        room_type = self.request.GET.get("room_type", "").strip()
        ordering = self.request.GET.get("ordering", "featured")
        if city:
            queryset = queryset.filter(city=city)
        if guests.isdigit():
            queryset = queryset.filter(person_capacity__gte=int(guests))
        if bedrooms.isdigit():
            queryset = queryset.filter(bedrooms_number__gte=int(bedrooms))
        if room_type:
            queryset = queryset.filter(room_type=room_type)
        ordering_map = {
            "featured": ("-is_featured", "sort_order", "id"),
            "rating": ("-average_review_rating", "-is_featured", "id"),
            "capacity": ("-person_capacity", "-is_featured", "id"),
        }
        return queryset.order_by(*ordering_map.get(ordering, ordering_map["featured"]))

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        self.request._local_public_context = True
        context = super().get_context_data(**kwargs)
        page_properties = list(context["properties"])
        context["properties"] = page_properties
        if context.get("page_obj") is not None:
            context["page_obj"].object_list = page_properties
        context["filter_cities"] = sorted({item.city for item in page_properties if item.city})
        context["footer_cities"] = context["filter_cities"]
        context["filter_room_types"] = sorted(
            {item.room_type for item in page_properties if item.room_type}
        )
        context["active_filters"] = self.request.GET
        query = self.request.GET.copy()
        query.pop("page", None)
        context["pagination_query"] = f"{query.urlencode()}&" if query else ""
        return context


class PropertyDetailView(DetailView):
    template_name = "properties/property_detail.html"
    context_object_name = "property"
    slug_url_kwarg = "slug"

    def get_queryset(self) -> QuerySet[Property]:
        return Property.objects.public().prefetch_related(
            Prefetch(
                "images",
                queryset=_card_images()[:8],
                to_attr="_public_images",
            ),
            Prefetch(
                "property_amenities",
                queryset=PropertyAmenity.objects.filter(
                    is_visible=True,
                    is_active_at_source=True,
                    amenity__is_active=True,
                )
                .select_related("amenity")
                .order_by("sort_order", "id"),
                to_attr="_public_amenities",
            ),
            Prefetch(
                "reviews",
                queryset=Review.objects.public().select_related("property")[:6],
                to_attr="_public_reviews",
            ),
        )

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        self.request._local_public_context = True
        context = super().get_context_data(**kwargs)
        property_obj = self.object
        similar = list(
            Property.objects.public()
            .filter(city=property_obj.city)
            .exclude(pk=property_obj.pk)
            .prefetch_related(
                Prefetch(
                    "images",
                    queryset=_card_images()[:1],
                    to_attr="_public_images",
                )
            )[:3]
        )
        if len(similar) < 3:
            excluded = [property_obj.pk, *(item.pk for item in similar)]
            similar.extend(
                Property.objects.public()
                .exclude(pk__in=excluded)
                .prefetch_related(
                    Prefetch(
                        "images",
                        queryset=_card_images()[:1],
                        to_attr="_public_images",
                    )
                )[: 3 - len(similar)]
            )
        context.update(
            {
                "gallery_images": property_obj._public_images,
                "visible_amenities": property_obj._public_amenities,
                "property_reviews": property_obj._public_reviews,
                "availability_form": AvailabilitySearchForm(property_obj=property_obj),
                "similar_properties": similar,
                "total_image_count": property_obj.images.public().count(),
                "footer_cities": sorted(
                    {item.city for item in [property_obj, *similar] if item.city}
                ),
                "breadcrumb_items": [
                    {"label": _("Properties"), "url": reverse("properties:list")},
                    {"label": property_obj.display_name, "url": ""},
                ],
                "property_structured_data": {
                    "@context": "https://schema.org",
                    "@type": "VacationRental",
                    "name": property_obj.display_name,
                    "description": (
                        property_obj.description_ar
                        or property_obj.description_en
                        or property_obj.hostaway_description
                    )[:500],
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": property_obj.display_city,
                        "addressCountry": property_obj.country_code,
                    },
                    "occupancy": {
                        "@type": "QuantitativeValue",
                        "maxValue": property_obj.person_capacity,
                    },
                },
                "breadcrumb_structured_data": {
                    "@context": "https://schema.org",
                    "@type": "BreadcrumbList",
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": 1,
                            "name": _("Home"),
                            "item": self.request.build_absolute_uri(reverse("core:home")),
                        },
                        {
                            "@type": "ListItem",
                            "position": 2,
                            "name": _("Properties"),
                            "item": self.request.build_absolute_uri(reverse("properties:list")),
                        },
                        {
                            "@type": "ListItem",
                            "position": 3,
                            "name": property_obj.display_name,
                            "item": self.request.build_absolute_uri(
                                property_obj.get_absolute_url()
                            ),
                        },
                    ],
                },
            }
        )
        return context


class PropertyGalleryView(ListView):
    template_name = "properties/gallery.html"
    context_object_name = "gallery_images"
    paginate_by = 12

    def dispatch(
        self,
        request: HttpRequest,
        *args: object,
        **kwargs: object,
    ) -> HttpResponse:
        try:
            self.property_obj = Property.objects.public().get(slug=kwargs["slug"])
        except Property.DoesNotExist as exc:
            raise Http404 from exc
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[PropertyImage]:
        return _card_images().filter(property=self.property_obj)

    def get_context_data(self, **kwargs: object) -> dict[str, object]:
        context = super().get_context_data(**kwargs)
        context["property"] = self.property_obj
        return context
