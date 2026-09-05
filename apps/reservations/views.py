"""Public quote and local booking-intent flow; no Hostaway reservation is created."""

import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.db import DatabaseError
from django.db.models import Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.views import View

from apps.payments.currency import selected_currency
from apps.properties.cities import supported_city_choices
from apps.properties.models import Property, PropertyImage

from .booking_forms import GuestDetailsForm
from .forms import AvailabilitySearchForm, ReservationAccessForm
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
    grant_reservation_access,
    is_ip_rate_limited,
    is_rate_limited,
    mask_email,
    mask_phone,
    revoke_reservation_access,
    session_can_manage,
    session_key_hash,
    session_owns,
)
from .services.automatic_modifications import execute_automatic_modification
from .services.availability import AvailabilityRequest, AvailabilityService
from .services.booking import consume_revalidated_quote
from .services.modifications import ModificationService
from .services.stay_policy import stay_policy_for
from .signing import (
    quote_id_from_reference,
    quote_reference,
    verify_quote_fingerprint,
)

logger = logging.getLogger(__name__)


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
        # Shared by every path that renders the review page, so the terms the
        # guest accepts are the ones shown directly above the checkbox.
        "stay_policy": stay_policy_for(quote.property),
        "quote_reference": quote_reference(quote),
        "quote_is_usable": (
            quote.status == BookingQuote.Status.ACTIVE
            and not quote.is_expired
            and verify_quote_fingerprint(quote)
        ),
        "guest_form_initial_step": (
            2
            if form.is_bound
            and any(
                form.errors.get(name)
                for name in (
                    "billing_street1",
                    "billing_city",
                    "billing_state",
                    "billing_country",
                    "billing_postcode",
                    "terms_accepted",
                    "privacy_accepted",
                )
            )
            else 1
        ),
    }


class AvailabilitySearchView(View):
    """Create a persisted, session-owned quote after live verification."""

    http_method_names = ["get", "post"]
    service_class = AvailabilityService

    def get(self, request: HttpRequest) -> HttpResponse:
        """Re-render a read-only portfolio search after a preference change."""

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
        form = AvailabilitySearchForm(request.GET)
        if not form.is_valid() or form.cleaned_data.get("property") is not None:
            return redirect("properties:list")
        return self._render_city_search(request, form)

    def _render_city_search(
        self,
        request: HttpRequest,
        form: AvailabilitySearchForm,
    ) -> HttpResponse:
        city = str(form.cleaned_data.get("city") or "")
        check_in = form.cleaned_data["check_in"]
        check_out = form.cleaned_data["check_out"]
        guests = form.cleaned_data["guests"]
        properties = Property.objects.public()
        if city:
            properties = properties.filter(city=city)
        properties = (
            properties.filter(Q(person_capacity__gte=guests) | Q(person_capacity__isnull=True))
            .prefetch_related(
                Prefetch(
                    "images",
                    queryset=PropertyImage.objects.public().order_by(
                        "-is_cover",
                        "sort_order",
                        "hostaway_sort_order",
                        "id",
                    )[:5],
                    to_attr="_public_images",
                )
            )
            .order_by("-is_featured", "sort_order", "id")
        )
        available_results: list[dict[str, object]] = []
        with self.service_class() as service:
            for candidate in properties:
                # Portfolio browsing reads the shared Hostaway cache. The binding
                # quote and payment steps still revalidate independently.
                availability = service.check(
                    AvailabilityRequest(
                        property=candidate,
                        check_in=check_in,
                        check_out=check_out,
                        guests=guests,
                    ),
                    bypass_cache=False,
                )
                if availability.is_available and availability.quote is not None:
                    available_results.append(
                        {
                            "property": candidate,
                            "availability": availability,
                            "detail_url": (
                                f"{candidate.get_absolute_url()}?"
                                + urlencode(
                                    {
                                        "source": "availability",
                                        "check_in": check_in.isoformat(),
                                        "check_out": check_out.isoformat(),
                                        "guests": guests,
                                    }
                                )
                            ),
                        }
                    )
        language = translation.get_language() or "ar"
        city_label = dict(supported_city_choices(language)).get(city, city) if city else ""
        search = {
            "city": city,
            "check_in": check_in,
            "check_out": check_out,
            "guests": guests,
        }
        query = urlencode(
            {
                "city": city,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "guests": guests,
            }
        )
        return render(
            request,
            "reservations/availability_result.html",
            {
                "city_search": True,
                "availability_results": available_results,
                "searched_city": city_label,
                "search": search,
                "currency_return_url": f"{reverse('reservations:quote_create')}?{query}",
            },
        )

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

        property_obj = form.cleaned_data.get("property")
        check_in = form.cleaned_data["check_in"]
        check_out = form.cleaned_data["check_out"]
        guests = form.cleaned_data["guests"]

        if property_obj is None:
            return self._render_city_search(request, form)

        assert isinstance(property_obj, Property)
        availability_request = AvailabilityRequest(
            property=property_obj,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
        )
        try:
            with self.service_class() as service:
                creation = service.create_booking_quote(
                    availability_request,
                    session_hash=session_key_hash(request),
                    selected_display_currency=selected_currency(request),
                    bypass_cache=True,
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
        guest_country_code = "SA"
        initial: dict[str, str] = {}
        if request.user.is_authenticated:
            initial = {
                "guest_first_name": request.user.first_name,
                "guest_last_name": request.user.last_name,
                "guest_email": request.user.email,
            }
        return render(
            request,
            "reservations/booking_quote_detail.html",
            _quote_context(
                quote,
                GuestDetailsForm(
                    default_country_code=guest_country_code,
                    initial=initial,
                ),
            ),
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
        guest_country_code = "SA"
        form = GuestDetailsForm(
            request.POST,
            default_country_code=guest_country_code,
        )
        if not form.is_valid():
            return render(
                request,
                "reservations/booking_quote_detail.html",
                _quote_context(quote, form),
                status=400,
            )
        if not verify_quote_fingerprint(quote):
            BookingQuote.objects.filter(pk=quote.pk).update(
                status=BookingQuote.Status.INVALIDATED,
                invalidated_at=timezone.now(),
            )
            quote.status = BookingQuote.Status.INVALIDATED
        availability_request = AvailabilityRequest(
            property=quote.property,
            check_in=quote.check_in,
            check_out=quote.check_out,
            guests=quote.guests,
        )
        if quote.is_expired and quote.status == BookingQuote.Status.ACTIVE:
            BookingQuote.objects.filter(
                pk=quote.pk,
                status=BookingQuote.Status.ACTIVE,
            ).update(status=BookingQuote.Status.EXPIRED)
            quote.status = BookingQuote.Status.EXPIRED
            logger.info(
                "FX_QUOTE_EXPIRED quote_id=%s source_currency=%s",
                quote.pk,
                quote.currency,
            )
            try:
                with self.service_class() as service:
                    replacement = service.create_booking_quote(
                        availability_request,
                        session_hash=session_key_hash(request),
                        selected_display_currency=selected_currency(request),
                        bypass_cache=True,
                    )
            except ValueError:
                replacement = None
            if replacement and replacement.quote:
                messages.warning(
                    request,
                    _("The previous quote expired. Please review the refreshed price."),
                )
                return redirect(
                    "reservations:quote_detail",
                    reference=quote_reference(replacement.quote),
                )
        if quote.status != BookingQuote.Status.ACTIVE:
            messages.error(request, _("This quote is no longer valid. Please request a new one."))
            return redirect("reservations:quote_detail", reference=reference)

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
                "guest_country_code": form.cleaned_data["billing_country"],
                "billing_street1": form.cleaned_data["billing_street1"],
                "billing_city": form.cleaned_data["billing_city"],
                "billing_state": form.cleaned_data["billing_state"],
                "billing_country": form.cleaned_data["billing_country"],
                "billing_postcode": form.cleaned_data["billing_postcode"],
                "language": (translation.get_language() or "ar").split("-")[0],
                "special_requests": form.cleaned_data["special_requests"],
                "marketing_consent": form.cleaned_data["marketing_consent"],
            },
            revalidated=revalidated,
            selected_display_currency=selected_currency(request),
        )
        if outcome.intent is not None:
            if (
                request.user.is_authenticated
                and request.user.email
                and request.user.email.casefold() == outcome.intent.guest_email.casefold()
                and outcome.intent.customer_id != request.user.pk
            ):
                BookingIntent.objects.filter(pk=outcome.intent.pk).update(customer=request.user)
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
        owns_account = request.user.is_authenticated and intent.customer_id == request.user.pk
        if not session_owns(request, intent.session_key_hash) and not owns_account:
            raise Http404
        cover_image = (
            PropertyImage.objects.public()
            .filter(property=intent.property)
            .order_by("-is_cover", "sort_order", "hostaway_sort_order", "id")
            .first()
        )
        return _private_response(
            render(
                request,
                "reservations/booking_intent_detail.html",
                {
                    "intent": intent,
                    "cover_image": cover_image,
                    "masked_email": mask_email(intent.guest_email),
                    "masked_phone": mask_phone(intent.guest_phone),
                    "hyperpay_enabled": settings.HYPERPAY_ENABLED,
                },
            )
        )


def _owned_reservation(request: HttpRequest, public_reference: str) -> Reservation:
    try:
        reservation = Reservation.objects.select_related(
            "property",
            "booking_intent",
        ).get(public_reference=public_reference)
    except Reservation.DoesNotExist as exc:
        raise Http404 from exc
    if reservation.booking_intent is None:
        raise Http404
    owns_booking_session = session_owns(
        request,
        reservation.booking_intent.session_key_hash,
    )
    owns_account = (
        request.user.is_authenticated and reservation.booking_intent.customer_id == request.user.pk
    )
    if (
        not owns_booking_session
        and not owns_account
        and not session_can_manage(request, reservation.public_reference)
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
    owns_booking_session = session_owns(request, modification.session_key_hash)
    has_management_access = session_can_manage(
        request,
        modification.reservation.public_reference,
    )
    owns_account = (
        request.user.is_authenticated
        and modification.reservation.booking_intent is not None
        and modification.reservation.booking_intent.customer_id == request.user.pk
    )
    if not owns_booking_session and not owns_account and not has_management_access:
        raise Http404
    return modification


def _private_response(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    # Keep booking data private while allowing same-origin CSRF origin checks in Chrome.
    response["Referrer-Policy"] = "same-origin"
    return response


def _management_context(
    reservation: Reservation,
    **overrides: object,
) -> dict[str, object]:
    cover_image = (
        PropertyImage.objects.public()
        .filter(property=reservation.property)
        .order_by("-is_cover", "sort_order", "hostaway_sort_order", "id")
        .first()
    )
    context: dict[str, object] = {
        "reservation": reservation,
        "cover_image": cover_image,
        "masked_email": mask_email(reservation.booking_intent.guest_email),
        "modification_requests": reservation.modification_requests.order_by("-requested_at")[:6],
        "can_request_changes": (
            reservation.normalized_status == Reservation.Status.CONFIRMED
            and reservation.source_type == Reservation.SourceType.DIRECT_WEBSITE
        ),
        "stay_is_active": reservation.normalized_status in Reservation.ACTIVE_STATUSES,
        "stay_is_closed": reservation.normalized_status in Reservation.CLOSED_STATUSES,
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
    }
    context.update(overrides)
    extension_form = context["extension_form"]
    date_form = context["date_form"]
    guest_form = context["guest_form"]
    extension_form.fields["new_check_out"].widget.attrs["min"] = (
        reservation.check_out + timedelta(days=1)
    ).isoformat()
    date_form.fields["new_check_in"].widget.attrs["min"] = timezone.localdate().isoformat()
    date_form.fields["new_check_out"].widget.attrs["min"] = (
        timezone.localdate() + timedelta(days=1)
    ).isoformat()
    if reservation.property and reservation.property.person_capacity:
        capacity = reservation.property.person_capacity
        date_form.fields["new_guests"].widget.attrs["max"] = capacity
        guest_form.fields["new_guests"].widget.attrs["max"] = capacity
    return context


class ReservationAccessView(View):
    """Open a limited management session using the booking reference and email."""

    http_method_names = ["get", "post"]

    def get(self, request: HttpRequest) -> HttpResponse:
        request._disable_google_integrations = True
        return _private_response(
            render(
                request,
                "reservations/reservation_access.html",
                {"reservation_access_form": ReservationAccessForm()},
            )
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        request._disable_google_integrations = True
        form = ReservationAccessForm(request.POST)
        if is_ip_rate_limited(
            request,
            scope="reservation-management-access",
            requests=settings.BOOKING_MANAGEMENT_ACCESS_RATE_LIMIT_REQUESTS,
            window=settings.BOOKING_MANAGEMENT_ACCESS_RATE_LIMIT_WINDOW,
        ):
            form.add_error(
                None,
                _("Too many attempts. Please wait a few minutes and try again."),
            )
            return _private_response(
                render(
                    request,
                    "reservations/reservation_access.html",
                    {"reservation_access_form": form, "rate_limited": True},
                    status=429,
                )
            )
        reservation = None
        if form.is_valid():
            reservation = (
                Reservation.objects.select_related("booking_intent")
                .filter(
                    public_reference=form.cleaned_data["booking_reference"],
                    booking_intent__guest_email__iexact=form.cleaned_data["email"],
                )
                .first()
            )
        if reservation is None:
            if form.is_valid():
                form.add_error(
                    None,
                    _(
                        "We could not match these details. Check the booking number "
                        "and the email used for booking."
                    ),
                )
            return _private_response(
                render(
                    request,
                    "reservations/reservation_access.html",
                    {"reservation_access_form": form},
                    status=400,
                )
            )

        grant_reservation_access(request, reservation.public_reference)
        if (
            request.user.is_authenticated
            and request.user.email
            and request.user.email.casefold() == reservation.booking_intent.guest_email.casefold()
            and reservation.booking_intent.customer_id != request.user.pk
        ):
            BookingIntent.objects.filter(pk=reservation.booking_intent_id).update(
                customer=request.user
            )
        try:
            from apps.notifications.services.audit import record_audit

            record_audit(
                action="reservation.management_accessed",
                object_type="Reservation",
                object_reference=reservation.public_reference,
                summary="Customer management session opened.",
                request=request,
                metadata={"source": "booking_reference_email"},
            )
        except DatabaseError:
            logger.warning("Reservation management audit could not be recorded.")
        messages.success(request, _("Welcome back. Your booking is ready to manage."))
        return _private_response(
            redirect(
                "reservations:manage",
                public_reference=reservation.public_reference,
            )
        )


class ReservationLogoutView(View):
    http_method_names = ["post"]

    def post(self, request: HttpRequest) -> HttpResponse:
        request._disable_google_integrations = True
        revoke_reservation_access(request)
        messages.success(request, _("You have securely left booking management."))
        return _private_response(redirect("reservations:manage_access"))


class ReservationManageView(View):
    http_method_names = ["get"]

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        request._disable_google_integrations = True
        reservation = _owned_reservation(request, public_reference)
        return _private_response(
            render(
                request,
                "reservations/manage_reservation.html",
                _management_context(reservation),
            )
        )


# What the guest is told when a change request is refused. Anything absent falls
# back to the neutral sentence below, so a new internal code never leaks out.
MODIFICATION_REFUSAL_MESSAGES = {
    "minimum_stay_not_met": _(
        "The stay would be shorter than the minimum nights this property allows."
    ),
    "maximum_stay_exceeded": _(
        "The stay would be longer than the maximum nights this property allows."
    ),
    "same_day_change_not_allowed": _(
        "This property does not accept a stay that starts today."
    ),
    "arrival_lead_time_not_met": _(
        "This change is too close to the arrival time to be requested online."
    ),
    "closed_on_arrival": _("The property does not accept arrivals on that date."),
    "closed_on_departure": _("The property does not accept departures on that date."),
    "capacity_exceeded": _("The guest count exceeds this property's capacity."),
    "unavailable_dates": _("The requested dates are no longer available."),
    "inventory_conflict": _("The requested dates are no longer available."),
    "past_check_in": _("Check-in cannot be in the past."),
    "invalid_dates": _("The stay dates are invalid."),
    "extension_must_add_nights": _("An extension has to add at least one night."),
    "extension_limit_exceeded": _("This extension is longer than we can take online."),
    "hostaway_temporarily_unavailable": _(
        "Live availability could not be reached. Please try again shortly."
    ),
    "pricing_unavailable": _("A price for these dates could not be prepared."),
    "cancellation_requests_disabled": _(
        "Cancellation requests are unavailable right now. Please contact guest support."
    ),
    "external_channel_requires_admin": _(
        "Please complete changes through the booking platform or contact management."
    ),
}


class ModificationCreateView(View):
    http_method_names = ["post"]
    service_class = ModificationService

    def post(
        self,
        request: HttpRequest,
        public_reference: str,
        action: str,
    ) -> HttpResponse:
        request._disable_google_integrations = True
        if is_rate_limited(
            request,
            scope="modification-create",
            requests=settings.BOOKING_MODIFICATION_RATE_LIMIT_REQUESTS,
            window=settings.BOOKING_MODIFICATION_RATE_LIMIT_WINDOW,
        ):
            return _private_response(HttpResponse(_("Too many requests."), status=429))
        reservation = _owned_reservation(request, public_reference)
        assert reservation.booking_intent is not None
        session_hash = reservation.booking_intent.session_key_hash
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
            if outcome.request.status == BookingModificationRequest.Status.READY_FOR_HOSTAWAY:
                execute_automatic_modification(outcome.request)
            return _private_response(
                redirect(
                    "reservations:modification_detail",
                    public_reference=outcome.request.public_reference,
                )
            )
        messages.error(
            request,
            MODIFICATION_REFUSAL_MESSAGES.get(
                outcome.code,
                _("The change request could not be created. The booking was not changed."),
            ),
        )
        return _private_response(
            redirect(
                "reservations:manage",
                public_reference=reservation.public_reference,
            )
        )

    @staticmethod
    def _invalid(
        request: HttpRequest,
        reservation: Reservation,
        form: object,
        form_name: str,
    ) -> HttpResponse:
        context = _management_context(reservation, **{form_name: form})
        return _private_response(
            render(request, "reservations/manage_reservation.html", context, status=400)
        )


class ModificationDetailView(View):
    http_method_names = ["get"]

    def get(self, request: HttpRequest, public_reference: str) -> HttpResponse:
        request._disable_google_integrations = True
        modification = _owned_modification(request, public_reference)
        return _private_response(
            render(
                request,
                "reservations/modification_detail.html",
                {
                    "modification": modification,
                    "hyperpay_enabled": settings.HYPERPAY_ENABLED,
                },
            )
        )
