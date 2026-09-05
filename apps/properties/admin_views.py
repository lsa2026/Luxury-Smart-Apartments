"""Staff-only bulk editor for the trilingual image alternative text."""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.notifications.services.audit import record_audit

from .models import Property, PropertyImage

LANGUAGE_FIELDS = ("alt_text_ar", "alt_text_en", "alt_text_fr")
# Filtering PropertyImage directly uses bare field names; annotating Property has to
# traverse the reverse relation, so the same condition needs an "images__" prefix.
MISSING_ALT = Q(alt_text_ar="") | Q(alt_text_en="") | Q(alt_text_fr="")
MISSING_ALT_VIA_IMAGES = (
    Q(images__alt_text_ar="") | Q(images__alt_text_en="") | Q(images__alt_text_fr="")
)


def _property_rows() -> list[dict[str, object]]:
    """Summarise how much alternative text each property is still missing."""
    return list(
        Property.objects.annotate(
            image_count=Count("images", distinct=True),
            missing_count=Count("images", filter=MISSING_ALT_VIA_IMAGES, distinct=True),
        )
        .order_by("-missing_count", "sort_order", "id")
        .values("id", "slug", "name_ar", "name_en", "name_fr", "image_count", "missing_count")
    )


@staff_member_required
def image_alt_text_editor(request: HttpRequest) -> HttpResponse:
    if not (request.user.is_superuser or request.user.has_perm("properties.change_propertyimage")):
        raise PermissionDenied

    summary = _property_rows()
    selected_id = request.GET.get("property") or request.POST.get("property") or ""
    selected = next(
        (row for row in summary if str(row["id"]) == str(selected_id)),
        next((row for row in summary if row["missing_count"]), None) if summary else None,
    )
    only_missing = (request.GET.get("missing") or "1") != "0"

    if request.method == "POST":
        if selected is None:
            raise PermissionDenied
        images = {
            image.pk: image for image in PropertyImage.objects.filter(property_id=selected["id"])
        }
        changed: list[PropertyImage] = []
        for pk, image in images.items():
            touched = False
            for field in LANGUAGE_FIELDS:
                posted = request.POST.get(f"{field}-{pk}")
                if posted is None:
                    continue
                value = posted.strip()[:255]
                if value != getattr(image, field):
                    setattr(image, field, value)
                    touched = True
            if touched:
                changed.append(image)
        if changed:
            PropertyImage.objects.bulk_update(changed, LANGUAGE_FIELDS)
            record_audit(
                request=request,
                action="property.image_alt_text_changed",
                object_type="Property",
                object_reference=str(selected["id"]),
                summary="Image alternative text was updated in bulk.",
                metadata={"count": len(changed), "fields": list(LANGUAGE_FIELDS)},
            )
            messages.success(
                request,
                _("Saved alternative text for %(count)d image(s).") % {"count": len(changed)},
            )
        else:
            messages.info(request, _("No alternative text was changed."))
        target = f"{reverse('properties_admin:image_alt_text')}?property={selected['id']}"
        return redirect(f"{target}&missing={'1' if only_missing else '0'}")

    images: list[PropertyImage] = []
    if selected is not None:
        queryset = PropertyImage.objects.filter(property_id=selected["id"])
        if only_missing:
            queryset = queryset.filter(MISSING_ALT)
        images = list(queryset.order_by("-is_cover", "sort_order", "hostaway_sort_order", "id"))

    return render(
        request,
        "admin/properties/image_alt_text.html",
        {
            "title": _("Image alternative text"),
            "summary": summary,
            "selected": selected,
            "images": images,
            "only_missing": only_missing,
            "total_missing": sum(row["missing_count"] for row in summary),
        },
    )
