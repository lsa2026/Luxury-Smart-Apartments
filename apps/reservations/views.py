"""Public quote and local booking-intent flow; no Hostaway reservation is created."""

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View

from apps.properties.models import Property, PropertyImage

from .booking_forms import GuestDetailsForm
from .forms import AvailabilitySearchForm
from .models import (
    BookingIntent,
    BookingModificationRequest,
    BookingQuote,
    Reservation,
)
from .modification_forms import (
    CancellationRequestForm,
    DateChangeRequestForm,
    ExtensionRequestForm,
    GuestChangeRequestForm,
)
from .security import (
    is_rate_limited,
    mask_email,
    mask_phone,
    session_key_hash,
    session_owns,
)
from .services.availability import AvailabilityRequest, AvailabilityService
from .services.booking import consume_revalidated_quote
from .services.modifications import ModificationService
from .signing import (
    quote_id_from_reference,
    quote_reference,
    verify_quote_fingerprint,
)


def _owned_quote(request: HttpRequest, reference: str) -> BookingQuote:
    try:
        quote_id = quote_id_from_reference(reference)
        quote = BookingQuote.objects.select_related("property").get(pk=quote_id)
    except (signing.BadSignature, ValueError, BookingQuote.DoesNotExist) as exc:
        raise Http404 from exc
    if not session_owns(request, quote.session_key_hash):
        raise Http404
    return quote


def _quote_context(quote: BookingQuote, form: GuestDetailsForm) -> dict[str, object]:
    cover_image = (
        PropertyImage.objects.public()
        .filter(property=quote.property)
        .order_by("-is_cover", "sort_order", "hostaway_sort_order", "id")
        .first()
    )
    return {
        "quote": quote,
        "property": quote.property,
        "cover_image": cover_image,
        "guest_form": form,
        "quote_reference": quote_reference(quote),
        "quote_is_usable": (
            quote.status == BookingQuote.Status.ACTIVE
            and not quote.is_expired
            and verify_quote_fingerprint(quote)
        ),
    }


class AvailabilitySearchView(View):
    """Create a persisted, session-owned quote after live verification."""

    http_method_names = ["post"]
    service_class = AvailabilityService

    def post(self, request: HttpRequest) -> HttpResponse:
        if is_rate_limited(
            request,
            scope="quote-create",
            requests=settings.AVAILABILITY_RATE_LIMIT_REQUESTS,
            window=settings.AVAILABILITY_RATE_LIMIT_WINDOW,
        ):
            return render(
                request,
                "reservations/availability_result.html",
                {
                    "rate_limited": True,
                    "user_message": _("Too many checks. Please wait and try again."),
                },
                status=429,
            )

        form = AvailabilitySearchForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "reservations/availability_result.html",
                {"form": form},
                status=400,
            )

        property_obj = form.cleaned_data["property"]
        assert isinstance(property_obj, Property)
        availability_request = AvailabilityRequest(
            property=property_obj,
            check_in=form.cleaned_data["check_in"],
            check_out=form.cleaned_data["check_out"],
            guests=form.cleaned_data["guests"],
        )
        try:
            with self.service_class() as service:
                creation = service.create_booking_quote(
                    availability_request,
                    session_hash=session_key_hash(request),
                )
        except ValueError:
            return render(
                request,
                "reservations/availability_result.html",
                {
                    "property": property_obj,
                    "user_message": _("A secure quote could not be created for these dates."),
                },
                status=503,
            )
        if creation.quote is not None:
            return redirect(
                "reservations:quote_detail",
                reference=quote_reference(creation.quote),
            )
        return render(
            request,
            "reservations/availability_result.html",
            {
                "availability": creation.availability,
                "property": property_obj,
            },
        )


class BookingQuoteDetailView(View):
    http_method_names = ["get"]

    def get(self, request: HttpRequest, reference: str) -> HttpResponse:
        if is_rate_limited(
            request,
            scope="quote-read",
            requests=settings.BOOKING_READ_RATE_LIMIT_REQUESTS,
            window=settings.BOOKING_READ_RATE_LIMIT_WINDOW,
        ):
            return HttpResponse(_("Too many requests."), status=429)
        quote = _owned_quote(request, reference)
        return render(
            request,
            "reservations/booking_quote_detail.html",
            _quote_context(quote, GuestDetailsForm()),
        )


class GuestDetailsView(View):
    http_method_names = ["post"]
    service_class = AvailabilityService

    def post(self, request: HttpRequest, reference: str) -> HttpResponse:
        if is_rate_limited(
            request,
            scope="intent-create",
            requests=settings.BOOKING_INTENT_RATE_LIMIT_REQUESTS,
            window=settings.BOOKING_INTENT_RATE_LIMIT_WINDOW,
        ):
            return HttpResponse(_("Too many requests."), status=429)
        quote = _owned_quote(request, reference)
        form = GuestDetailsForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                "reservations/booking_quote_detail.html",
                _quote_context(quote, form),
                status=400,
            )
        if quote.is_expired and quote.status == BookingQuote.Status.ACTIVE:
            BookingQuote.objects.filter(
                pk=quote.pk,
                status=BookingQuote.Status.ACTIVE,
            ).update(status=BookingQuote.Status.EXPIRED)
            quote.status = BookingQuote.Status.EXPIRED
        if not verify_quote_fingerprint(quote):
            BookingQuote.objects.filter(pk=quote.pk).update(
                status=BookingQuote.Status.INVALIDATED,
                invalidated_at=timezone.now(),
            )
            quote.status = BookingQuote.Status.INVALIDATED
        if quote.status != BookingQuote.Status.ACTIVE:
            messages.error(request, _("This quote is no longer valid. Please request a new one."))
            return redirect("reservations:quote_detail", reference=reference)

        availability_request = AvailabilityRequest(
            property=quote.property,
            check_in=quote.check_in,
            check_out=quote.check_out,
            guests=quote.guests,
        )
        with self.service_class() as service:
            revalidated = service.check(availability_request, bypass_cache=True)

        outcome = consume_revalidated_quote(
            quote_id=quote.pk,
            session_hash=session_key_hash(request),
            idempotency_key=form.cleaned_data["idempotency_key"],
            guest_data={
                "guest_first_name": form.cleaned_data["guest_first_name"],
                "guest_last_name": form.cleaned_data["guest_last_name"],
                "guest_email": form.cleaned_data["guest_email"],
                "guest_phone": form.cleaned_data["guest_phone"],
                "guest_country_code": form.cleaned_data["guest_country_code"],
                "special_requests": form.cleaned_data["special_requests"],
                "marketing_consent": form.cleaned_data["marketing_consent"],
            },
            revalidated=revalidated,
        )
        if outcome.intent is not None:
            return redirect(
                "reservations:intent_detail",
                public_reference=outcome.intent.public_reference,
            )
        if outcome.code == "price_changed" and outcome.replacement_quote:
            messages.warning(
                request,
                _(
                    "The price changed from %(old)s to %(new)s %(currency)s. "
                    "Please review and approve it again."
                )
                % {
                    "old": outcome.old_total,
                    "new": outcome.new_total,
                    "currency": outcome.replacement_quote.currency,
                },
            )
            return redirect(
                "reservations:quote_detail",
                reference=quote_reference(outcome.replacement_quote),
            )
        if outcome.code == "unavailable":
            messages.error(
                request,
                _("This property is unavailable for the selected dates. Try other dates."),
            )
        else:
            messages.error(
                request,
                _("The request could not continue. Please request a new quote."),
            )
        return redirect("reservations:quote_detail", reference=reference)


class BookingIntentDetailView(View):
    http_method_names = ["get"]

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        if is_rate_limited(
            request,
            scope="intent-read",
            requests=settings.BOOKING_READ_RATE_LIMIT_REQUESTS,
            window=settings.BOOKING_READ_RATE_LIMIT_WINDOW,
        ):
            return HttpResponse(_("Too many requests."), status=429)
        try:
            intent = BookingIntent.objects.select_related("property", "quote").get(
                public_reference=public_reference
            )
        except BookingIntent.DoesNotExist as exc:
            raise Http404 from exc
        if not session_owns(request, intent.session_key_hash):
            raise Http404
        return render(
            request,
            "reservations/booking_intent_detail.html",
            {
                "intent": intent,
                "masked_email": mask_email(intent.guest_email),
                "masked_phone": mask_phone(intent.guest_phone),
            },
        )


def _owned_reservation(request: HttpRequest, public_reference: str) -> Reservation:
    try:
        reservation = Reservation.objects.select_related(
            "property",
            "booking_intent",
        ).get(public_reference=public_reference)
    except Reservation.DoesNotExist as exc:
        raise Http404 from exc
    if reservation.booking_intent is None or not session_owns(
        request,
        reservation.booking_intent.session_key_hash,
    ):
        raise Http404
    return reservation


def _owned_modification(
    request: HttpRequest,
    public_reference: str,
) -> BookingModificationRequest:
    try:
        modification = BookingModificationRequest.objects.select_related(
            "reservation__property",
            "reservation__booking_intent",
        ).get(public_reference=public_reference)
    except BookingModificationRequest.DoesNotExist as exc:
        raise Http404 from exc
    if not session_owns(request, modification.session_key_hash):
        raise Http404
    return modification


class ReservationManageView(View):
    http_method_names = ["get"]

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        reservation = _owned_reservation(request, public_reference)
        return render(
            request,
            "reservations/manage_reservation.html",
            {
                "reservation": reservation,
                "extension_form": ExtensionRequestForm(),
                "date_form": DateChangeRequestForm(
                    initial={
                        "new_check_in": reservation.check_in,
                        "new_check_out": reservation.check_out,
                        "new_guests": reservation.guests,
                    }
                ),
                "guest_form": GuestChangeRequestForm(initial={"new_guests": reservation.guests}),
                "cancellation_form": CancellationRequestForm(),
            },
        )


class ModificationCreateView(View):
    http_method_names = ["post"]
    service_class = ModificationService

    def post(
        self,
        request: HttpRequest,
        public_reference: str,
        action: str,
    ) -> HttpResponse:
        if is_rate_limited(
            request,
            scope="modification-create",
            requests=settings.BOOKING_MODIFICATION_RATE_LIMIT_REQUESTS,
            window=settings.BOOKING_MODIFICATION_RATE_LIMIT_WINDOW,
        ):
            return HttpResponse(_("Too many requests."), status=429)
        reservation = _owned_reservation(request, public_reference)
        session_hash = session_key_hash(request)
        if action == "extend":
            form = ExtensionRequestForm(request.POST)
            if form.is_valid():
                with self.service_class() as service:
                    outcome = service.create_extension_quote(
                        reservation,
                        new_check_out=form.cleaned_data["new_check_out"],
                        session_hash=session_hash,
                        reason=form.cleaned_data["reason"],
                    )
            else:
                return self._invalid(request, reservation, form, "extension_form")
        elif action == "dates":
            form = DateChangeRequestForm(request.POST)
            if form.is_valid():
                with self.service_class() as service:
                    outcome = service.create_change_quote(
                        reservation,
                        new_check_in=form.cleaned_data["new_check_in"],
                        new_check_out=form.cleaned_data["new_check_out"],
                        new_guests=form.cleaned_data["new_guests"],
                        session_hash=session_hash,
                        reason=form.cleaned_data["reason"],
                    )
            else:
                return self._invalid(request, reservation, form, "date_form")
        elif action == "guests":
            form = GuestChangeRequestForm(request.POST)
            if form.is_valid():
                with self.service_class() as service:
                    outcome = service.create_change_quote(
                        reservation,
                        new_check_in=reservation.check_in,
                        new_check_out=reservation.check_out,
                        new_guests=form.cleaned_data["new_guests"],
                        session_hash=session_hash,
                        reason=form.cleaned_data["reason"],
                    )
            else:
                return self._invalid(request, reservation, form, "guest_form")
        elif action == "cancel":
            form = CancellationRequestForm(request.POST)
            if form.is_valid():
                with self.service_class() as service:
                    outcome = service.create_cancellation_request(
                        reservation,
                        session_hash=session_hash,
                        reason=form.cleaned_data["reason"],
                    )
            else:
                return self._invalid(request, reservation, form, "cancellation_form")
        else:
            raise Http404
        if outcome.request is not None:
            return redirect(
                "reservations:modification_detail",
                public_reference=outcome.request.public_reference,
            )
        messages.error(
            request,
            _("The change request could not be created. The booking was not changed."),
        )
        return redirect(
            "reservations:manage",
            public_reference=reservation.public_reference,
        )

    @staticmethod
    def _invalid(
        request: HttpRequest,
        reservation: Reservation,
        form: object,
        form_name: str,
    ) -> HttpResponse:
        context = {
            "reservation": reservation,
            "extension_form": ExtensionRequestForm(),
            "date_form": DateChangeRequestForm(),
            "guest_form": GuestChangeRequestForm(),
            "cancellation_form": CancellationRequestForm(),
        }
        context[form_name] = form
        return render(request, "reservations/manage_reservation.html", context, status=400)


class ModificationDetailView(View):
    http_method_names = ["get"]

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        modification = _owned_modification(request, public_reference)
        return render(
            request,
            "reservations/modification_detail.html",
            {"modification": modification},
        )
