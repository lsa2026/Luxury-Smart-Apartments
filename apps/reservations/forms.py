"""Public forms for read-only availability checks."""

from datetime import date

from django import forms
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _

from apps.properties.cities import canonical_city, supported_city_choices
from apps.properties.models import Property


class ReservationAccessForm(forms.Form):
    booking_reference = forms.CharField(
        label=_("Booking number"),
        max_length=32,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
                "autocapitalize": "none",
                "spellcheck": "false",
                "placeholder": _("For example: your confirmation number"),
            }
        ),
    )
    email = forms.EmailField(
        label=_("Booking email"),
        max_length=254,
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "placeholder": _("The email used for booking"),
            }
        ),
    )

    def clean_booking_reference(self) -> str:
        return self.cleaned_data["booking_reference"].strip()

    def clean_email(self) -> str:
        return self.cleaned_data["email"].strip().casefold()


class LocalizedPropertyChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj: Property) -> str:
        return obj.display_name


class PropertyCitySelect(forms.Select):
    """Expose safe city and capacity metadata for dependent filtering."""

    def create_option(
        self,
        name: str,
        value: object,
        label: object,
        selected: bool,
        index: int,
        subindex: int | None = None,
        attrs: dict[str, object] | None = None,
    ) -> dict[str, object]:
        option = super().create_option(
            name,
            value,
            label,
            selected,
            index,
            subindex=subindex,
            attrs=attrs,
        )
        instance = getattr(value, "instance", None)
        if isinstance(instance, Property):
            option["attrs"]["data-city"] = canonical_city(instance.city)
            if instance.person_capacity:
                option["attrs"]["data-capacity"] = str(instance.person_capacity)
        return option


class AvailabilitySearchForm(forms.Form):
    city = forms.ChoiceField(label=_("City (optional)"), choices=(), required=False)
    property = LocalizedPropertyChoiceField(
        label=_("Property (optional)"),
        queryset=Property.objects.none(),
        empty_label=_("All available properties"),
        required=False,
        widget=PropertyCitySelect(attrs={"data-property-select": ""}),
    )
    check_in = forms.DateField(
        label=_("Check-in"),
        widget=forms.DateInput(
            attrs={"type": "date", "dir": "ltr", "lang": "en-CA"},
            format="%Y-%m-%d",
        ),
    )
    check_out = forms.DateField(
        label=_("Check-out"),
        widget=forms.DateInput(
            attrs={"type": "date", "dir": "ltr", "lang": "en-CA"},
            format="%Y-%m-%d",
        ),
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
            "name_fr",
            "hostaway_name",
            "person_capacity",
            "currency_code",
            "city_ar",
            "city_en",
            "city_fr",
            "city",
            "is_visible",
            "hostaway_is_active",
        )
        language = (translation.get_language() or "ar").split("-")[0]
        self.fields["city"].choices = [("", _("All cities"))] + supported_city_choices(language)
        self.fields["city"].widget.attrs["data-city-select"] = ""
        self.fields["property"].queryset = public_properties.order_by("sort_order", "id")
        today = timezone.localdate().isoformat()
        self.fields["check_in"].widget.attrs["min"] = today
        self.fields["check_out"].widget.attrs["min"] = today
        if property_obj is not None:
            self.fields["city"].widget = forms.HiddenInput()
            self.fields["city"].initial = canonical_city(property_obj.city)
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
            and canonical_city(property_obj.city) != city
        ):
            self.add_error("property", _("Choose a property in the selected city."))
        return cleaned
