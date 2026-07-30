"""Public forms for read-only availability checks."""

from datetime import date

from django import forms
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _

from apps.properties.models import Property


class LocalizedPropertyChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj: Property) -> str:
        language = (translation.get_language() or "ar").split("-")[0]
        if language == "en":
            return obj.name_en or obj.hostaway_name or obj.name_ar
        return obj.name_ar or obj.name_en or obj.hostaway_name


class AvailabilitySearchForm(forms.Form):
    city = forms.ChoiceField(label=_("City"), choices=(), required=False)
    property = LocalizedPropertyChoiceField(
        label=_("Property"),
        queryset=Property.objects.none(),
        empty_label=_("Choose a property"),
    )
    check_in = forms.DateField(
        label=_("Check-in"),
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    check_out = forms.DateField(
        label=_("Check-out"),
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    guests = forms.IntegerField(
        label=_("Guests"),
        min_value=1,
        initial=2,
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
    )

    def __init__(
        self,
        *args: object,
        property_obj: Property | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        public_properties = Property.objects.public().only(
            "id",
            "slug",
            "hostaway_listing_id",
            "name_ar",
            "name_en",
            "hostaway_name",
            "person_capacity",
            "currency_code",
            "city_ar",
            "city_en",
            "city",
            "is_visible",
            "hostaway_is_active",
        )
        city_values = list(
            public_properties.exclude(city="")
            .values_list("city", flat=True)
            .distinct()
            .order_by("city")
        )
        self.fields["city"].choices = [("", _("All cities"))] + [
            (city, city) for city in city_values
        ]
        self.fields["property"].queryset = public_properties
        today = timezone.localdate().isoformat()
        self.fields["check_in"].widget.attrs["min"] = today
        self.fields["check_out"].widget.attrs["min"] = today
        if property_obj is not None:
            self.fields["city"].widget = forms.HiddenInput()
            self.fields["city"].initial = property_obj.city
            self.fields["property"].initial = property_obj
            self.fields["property"].widget = forms.HiddenInput()
            if property_obj.person_capacity:
                self.fields["guests"].widget.attrs["max"] = property_obj.person_capacity

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        check_in = cleaned.get("check_in")
        check_out = cleaned.get("check_out")
        property_obj = cleaned.get("property")
        guests = cleaned.get("guests")
        city = cleaned.get("city")
        if isinstance(check_in, date) and isinstance(check_out, date):
            if check_out <= check_in:
                self.add_error("check_out", _("Check-out must be after check-in."))
            elif (check_out - check_in).days > 366:
                self.add_error("check_out", _("The maximum stay is 366 nights."))
        if (
            isinstance(property_obj, Property)
            and isinstance(guests, int)
            and property_obj.person_capacity is not None
            and guests > property_obj.person_capacity
        ):
            self.add_error("guests", _("Guest count exceeds this property's capacity."))
        if (
            isinstance(property_obj, Property)
            and isinstance(city, str)
            and city
            and property_obj.city != city
        ):
            self.add_error("property", _("Choose a property in the selected city."))
        return cleaned
