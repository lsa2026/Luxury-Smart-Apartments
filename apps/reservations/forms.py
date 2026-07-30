"""Public forms for read-only availability checks."""

from datetime import date

from django import forms
from django.utils import timezone

from apps.properties.models import Property


class AvailabilitySearchForm(forms.Form):
    property = forms.ModelChoiceField(
        label="الوحدة",
        queryset=Property.objects.none(),
        empty_label="اختر الوحدة",
    )
    check_in = forms.DateField(
        label="تاريخ الوصول",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    check_out = forms.DateField(
        label="تاريخ المغادرة",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    guests = forms.IntegerField(
        label="عدد الضيوف",
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
        self.fields["property"].queryset = public_properties
        today = timezone.localdate().isoformat()
        self.fields["check_in"].widget.attrs["min"] = today
        self.fields["check_out"].widget.attrs["min"] = today
        if property_obj is not None:
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
        if isinstance(check_in, date) and isinstance(check_out, date):
            if check_out <= check_in:
                self.add_error("check_out", "يجب أن يكون تاريخ المغادرة بعد الوصول.")
            elif (check_out - check_in).days > 366:
                self.add_error("check_out", "الحد الأقصى للفترة 366 ليلة.")
        if (
            isinstance(property_obj, Property)
            and isinstance(guests, int)
            and property_obj.person_capacity is not None
            and guests > property_obj.person_capacity
        ):
            self.add_error("guests", "عدد الضيوف يتجاوز سعة الوحدة.")
        return cleaned
