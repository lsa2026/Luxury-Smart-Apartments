"""Private source-owned invitation permit, independently bound before provider claim."""

from contextlib import contextmanager
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.debug import sensitive_variables

from apps.loyalty import invitation_registry as registry
from apps.loyalty.invitation_api import ready
from apps.loyalty.models import JoinInvitation
from apps.properties.models import Property

from . import message_source_authorization as authority
from . import message_source_relay as relay
from . import website_financial_source_v2 as financial
from .message_receipt_models import WebsiteMessageExpectation

KIND = "loyalty_invitation"


@contextmanager
def source_lock(*, is_loyalty=True):
    # Same order as enrollment and every local invitation consume: advisory lock
    # precedes relay/property/stay locks. No provider network runs inside it.
    with registry.invitation_lock() if is_loyalty else transaction.atomic():
        yield


def admitted(event):
    try:
        at = parse_datetime(getattr(settings, "MESSAGE_LOYALTY_SOURCE_CUTOVER_AT", ""))
    except (ValueError, TypeError):
        return False
    return bool(
        getattr(settings, "MESSAGE_LOYALTY_SOURCE_V2_ENABLED", False)
        and getattr(settings, "MESSAGE_SOURCE_V2_ENABLED", False)
        and relay.enabled()
        and at
        and timezone.is_aware(at)
        and event.stay.paid_at
        and at <= event.stay.paid_at <= event.created_at <= timezone.now()
        and relay.cutover() <= event.stay.paid_at
    )


def booking_key(stay, scenario):
    return "morning07:" + str(stay.pk) if scenario == "07" else registry.day_use_booking_key(stay)


def row_matches(invitation, stay, scenario):
    key = booking_key(stay, scenario)
    return (
        invitation.channel == "ycloud"
        and invitation.origin == "website"
        and invitation.source == ("morning07" if scenario == "07" else "day_use08")
        and invitation.reservation_key == key
        and invitation.event_key
        == registry.keyed("website:" + invitation.source + ":" + key + ":paid-invitation-v1")
        and invitation.phone == registry.normalize_phone(stay.guest_phone)
        and invitation.email.casefold() == stay.guest_email.casefold()
        and invitation.language == stay.guest_language
    )


def binding(invitation, expectation):
    return {
        "contract_version": "site01-loyalty-expectation-1",
        "invitation_id": str(invitation.pk),
        "provider_reference": invitation.provider_reference,
        "generation": invitation.generation,
        "registry_source": invitation.source,
        "registry_key": invitation.reservation_key,
        "registry_phone_digest": invitation.phone_digest,
        "registry_email_digest": invitation.email_digest,
        "registry_language": invitation.language,
        "request_id": expectation.request_id,
        "prepare_ref": expectation.prepare_ref,
        "identity": dict(expectation.identity),
    }


def payment_notice_delivered(event, scenario, result):
    rows = WebsiteMessageExpectation.objects.filter(
        relay__scenario=scenario,
        relay__stay_id=event.stay_id,
        relay__event_id=event.pk,
        relay__financial=True,
        relay__message_type="payment_success",
        relay__revision=str(event.stay.communication_revision),
        attempts_claimed=1,
        state__in=["delivered", "read"],
    )
    return any(
        item.identity.get("source_family") == f"lsa{scenario}_payment"
        and item.identity.get("source_event_key") == f"website:{scenario}:payment:{event.pk}"
        and item.identity.get("reservation_key") == result["reservation_key"]
        and item.identity.get("reservation_revision") == result["reservation_revision"]
        and item.identity.get("recipient_ref") == result["recipient_ref"]
        and item.identity.get("purpose") == "payment_confirmation"
        and item.identity.get("source_message_type") == "payment_success"
        and item.provider_message_id
        and item.wamid
        and item.receipt_evidence_ref
        for item in rows
    )


@sensitive_variables()
def source(event_id, scenario):
    """Read only. A caller's READ cannot create, renew or consume a Registry row."""
    if scenario not in authority.SOURCES:
        return financial.hold("source_owner_mismatch")
    stays, _, events, _, comms, payments = authority.SOURCES[scenario]
    try:
        pointer = events.objects.filter(pk=event_id).values("stay_id", "stay__property_id").first()
        if not pointer:
            return financial.hold("event_missing")
        with transaction.atomic():
            Property.objects.select_for_update().get(pk=pointer["stay__property_id"])
            stay = (
                stays.objects.select_for_update(of=("self",))
                .select_related("property")
                .get(pk=pointer["stay_id"])
            )
            event = events.objects.select_for_update().get(pk=event_id, stay=stay)
            event.stay = stay
            if stay.property_id != pointer["stay__property_id"] or not comms.admitted(stay):
                return financial.hold("current_collection_unverified")
            verified = financial.verified_confirmation_fields(event, scenario)
            result = financial.snapshot(stay, event, scenario, KIND, verified["financial_identity"])
            result.update(
                source_owner_family="website",
                source_occurred_at=stay.paid_at.isoformat(),
                payment_verified=True,
                payment_status="paid",
                payment_revision=event.payment_revision,
                financial_proof_ref=event.proof_key,
                core_delivery_verified=False,
                payment_notice_delivery_verified=False,
                loyalty_source_eligible=False,
                legacy_state="unknown",
            )
            from .telegram_stay_transport import selected

            if (
                selected(stay)
                or not admitted(event)
                or not ready()
                or result["consent"]["revoked"] is True
            ):
                return authority.scoped_facts(result)
            core = stay.communication_events.filter(revision=stay.communication_revision).first()
            if not financial.core_delivered(stay, core, scenario):
                return authority.scoped_facts(result)
            result["core_delivery_verified"] = True
            if not payment_notice_delivered(event, scenario, result):
                return authority.scoped_facts(result)
            result["payment_notice_delivery_verified"] = True
            phone = registry.normalize_phone(stay.guest_phone)
            key = booking_key(stay, scenario)
            invitation = JoinInvitation.objects.filter(reservation_key=key).first()
            deliveries = (
                payments.Morning07PaymentDelivery
                if scenario == "07"
                else payments.DayUse08PaymentDelivery
            )
            if (
                registry.member_exists(phone, stay.guest_email)
                or registry.competing(
                    key, phone, stay.guest_email, exclude_id=invitation.pk if invitation else None
                )
                or registry.legacy_exists(key, phone, stay.guest_email)
                or deliveries.objects.filter(event__stay=stay, message_type=KIND)
                .exclude(state="queued")
                .exists()
            ):
                return authority.scoped_facts(result)
            result["loyalty_source_eligible"] = True
            if invitation is None:
                return authority.scoped_facts(result)
            if not row_matches(invitation, stay, scenario):
                return authority.scoped_facts(result)
            locator = result["job_sha256"]
            expectation = WebsiteMessageExpectation.objects.filter(
                relay__scenario=scenario,
                relay__event_id=event.pk,
                relay__job_sha256=locator,
                relay__message_type=KIND,
            ).first()
            unused = (
                invitation.state == "claimed"
                and not invitation.dispatched_at
                and invitation.expires_at > timezone.now()
            )
            own_permit = (
                invitation.state == "unknown"
                and invitation.dispatched_at
                and expectation
                and expectation.attempts_claimed == 0
                and expectation.state == "prepared"
                and not expectation.provider_message_id
                and not expectation.wamid
                and expectation.source_binding == binding(invitation, expectation)
            )
            if expectation and (
                expectation.attempts_claimed != 0
                or expectation.state != "prepared"
                or expectation.provider_message_id
                or expectation.wamid
                or unused
                and expectation.source_binding
            ):
                return authority.scoped_facts(result)
            if not unused and not own_permit:
                return authority.scoped_facts(result)
            url = settings.SITE_BASE_URL.rstrip("/") + "/my-bookings/rewards/"
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                return authority.scoped_facts(result)
            result["loyalty_identity"] = {
                "invitation_id": str(invitation.pk),
                "provider_reference": invitation.provider_reference,
                "generation": invitation.generation,
                "invitation_claim_expires_at": invitation.expires_at.isoformat(),
                "loyalty_url_expires_at": None,
            }
            result["variables"]["loyalty_url"] = url
            result["legacy_state"] = "not_attempted"
            result = financial.scoped_snapshot(result, stay)
            if result.get("template_context"):
                result["template_context"]["loyalty_identity"] = dict(result["loyalty_identity"])
            return result
    except Exception:
        return financial.hold("loyalty_source_unverified")


@sensitive_variables()
def prepare_for_relay(row):
    if row.message_type != KIND or not row.financial or row.scenario not in authority.SOURCES:
        return
    if WebsiteMessageExpectation.objects.filter(relay=row).exists():
        return  # An existing first binding is never a reason to renew a claim.
    facts = source(row.event_id, row.scenario)
    if (
        facts.get("loyalty_source_eligible") is not True
        or facts.get("reservation_key")
        != ("morning07:" if row.scenario == "07" else "dayuse08:") + str(row.stay_id)
        or facts.get("reservation_revision") != row.revision
        or facts.get("job_sha256") != row.job_sha256
        or facts.get("source_event_id") != str(row.event_id)
    ):
        return
    stay = authority.SOURCES[row.scenario][0].objects.get(pk=row.stay_id)
    key = booking_key(stay, row.scenario)
    previous = JoinInvitation.objects.select_for_update().filter(reservation_key=key).first()
    if previous and (
        not row_matches(previous, stay, row.scenario)
        or previous.state != "claimed"
        or previous.dispatched_at
    ):
        return
    registry.claim(
        key=key,
        phone=registry.normalize_phone(stay.guest_phone),
        email=stay.guest_email,
        customer_id=None,
        language=stay.guest_language,
        event_id=key + ":paid-invitation-v1",
        source="morning07" if row.scenario == "07" else "day_use08",
        origin="website",
        channel="ycloud",
    )


@sensitive_variables()
def validate_permit(invitation, expectation, phone):
    current = WebsiteMessageExpectation.objects.select_for_update().get(pk=expectation.pk)
    row = current.relay
    facts = source(row.event_id, row.scenario)
    stay = authority.SOURCES[row.scenario][0].objects.get(pk=row.stay_id)
    stored = JoinInvitation.objects.select_for_update().get(pk=invitation.pk)
    if (
        current != expectation
        or current.identity != expectation.identity
        or current.attempts_claimed != 0
        or current.state != "prepared"
        or current.provider_message_id
        or current.wamid
        or current.source_binding
        or row.state != "forwarding"
        or not row.financial
        or row.message_type != KIND
        or facts.get("domain_authorized") is not True
        or not row_matches(stored, stay, row.scenario)
        or not row_matches(invitation, stay, row.scenario)
        or binding(stored, current) != binding(invitation, current)
        or stored.state != "claimed"
        or stored.dispatched_at
        or stored.phone != phone
        or stored.generation != invitation.generation
        or facts.get("loyalty_identity", {}).get("invitation_id") != str(stored.pk)
        or not current.identity.get("content_digest")
        or any(
            current.identity.get(k) != facts.get(k)
            for k in (
                "reservation_key",
                "reservation_revision",
                "recipient_ref",
                "purpose",
                "source_event_key",
                "source_message_type",
                "job_sha256",
            )
        )
        or current.identity.get("source_family") != f"lsa{row.scenario}_payment"
    ):
        raise ValidationError("website_invitation_not_verified")


def consume_ack(expectation):
    row = expectation.relay
    if row.message_type != KIND:
        return
    stay = authority.SOURCES[row.scenario][0].objects.get(pk=row.stay_id)
    invitation = JoinInvitation.objects.select_for_update().get(
        reservation_key=booking_key(stay, row.scenario)
    )
    if (
        registry.consume(
            invitation,
            phone=registry.normalize_phone(stay.guest_phone),
            website_message_expectation=expectation,
        )
        != "eligible"
    ):
        raise ValidationError("website_invitation_not_eligible")
    expectation.source_binding = binding(invitation, expectation)
    expectation.save(update_fields=["source_binding"])


def project_receipt(expectation):
    row = expectation.relay
    if row.message_type != KIND:
        return False
    stored = expectation.source_binding
    invitation = (
        JoinInvitation.objects.select_for_update().filter(pk=stored.get("invitation_id")).first()
    )
    if (
        not invitation
        or invitation.channel != "ycloud"
        or invitation.origin != "website"
        or stored != binding(invitation, expectation)
        or row.scenario not in authority.SOURCES
        or not row.financial
        or expectation.attempts_claimed != 1
        or not invitation.dispatched_at
        or not expectation.provider_message_id
        or len(expectation.provider_message_id) > 512
        or expectation.state not in {"accepted", "delivered", "read"}
        or expectation.state in {"delivered", "read"}
        and not expectation.receipt_evidence_ref
    ):
        return False
    registry.record(invitation, expectation.state, expectation.provider_message_id)
    return True
