"""Provider-backed contact selection and field provenance for new EXT drafts.

No writer, provider create, source20 RPC, or recipient-generation inference.
"""

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal
from time import monotonic

from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from apps.core.phone_numbers import InvalidPhoneNumber, normalize_booking_phone
from apps.integrations.hostaway.reservation_validators import validate_reservation_response

from .models import StayExtension, StayExtensionChain
from .stay_extensions import billing_initial, guest_initial, read_source, source_snapshot

PROVENANCE_CONTRACT = "site01-extension-contact-provenance-1"

GUEST_FIELDS = (
    "guest_first_name",
    "guest_last_name",
    "guest_email",
    "guest_phone",
    "guest_language",
)
BILLING_FIELDS = (
    "billing_street1",
    "billing_city",
    "billing_state",
    "billing_postcode",
    "billing_country",
)
FIELDS = GUEST_FIELDS + BILLING_FIELDS
BILLING_PROVIDER_FIELDS = dict(
    zip(
        BILLING_FIELDS,
        ("guestAddress", "guestCity", "guestState", "guestZipCode", "guestCountry"),
        strict=True,
    )
)


def root_contact_source(selected_raw):
    """Reuse the selected root GET; a selected child requires a root GET.

    Membership comes from the stored chain, never a caller-supplied root ID.
    Closed children cannot make an older draft the source of current contact.
    """
    selected = source_snapshot(selected_raw)
    member = (
        StayExtension.objects.select_related("chain")
        .filter(reservation__hostaway_reservation_id=selected["id"])
        .first()
    )
    chain = (
        member.chain
        if member
        else StayExtensionChain.objects.filter(root_hostaway_reservation_id=selected["id"]).first()
    )
    if chain is None:
        return selected_raw
    raw = (
        selected_raw
        if selected["id"] == chain.root_hostaway_reservation_id
        else read_source(chain.root_hostaway_reservation_id)
    )
    if source_snapshot(raw) != chain.root_snapshot:
        raise ValueError("contact_root_changed")
    if selected["listing_map_id"] != chain.root_snapshot["listing_map_id"]:
        raise ValueError("contact_property_changed")
    return raw


def contact_values(raw):
    values = {**guest_initial(raw), **billing_initial(raw)}
    # Present provider fields, including explicit clears, outrank an older local
    # billing intent. Missing fields can still use the existing saved address.
    for field, provider in BILLING_PROVIDER_FIELDS.items():
        if provider in raw:
            values[field] = str(raw[provider] or "").strip()
    return {field: str(values.get(field) or "") for field in FIELDS}


def _canonical(field, value):
    value = str(value or "").strip()
    if field == "guest_phone" and value:
        try:
            return normalize_booking_phone(value)
        except InvalidPhoneNumber:
            return value
    if field in {"guest_email", "guest_language"}:
        return value.casefold()
    if field == "billing_country":
        return value.upper()
    return value


def field_provenance(values, *, user_id, root_id):
    """Only keyed hashes enter the signed form ticket, never guest/address text."""
    return {
        field: salted_hmac(
            "extension-contact-fields-1",
            f"{user_id}:{root_id}:{field}:{_canonical(field, values.get(field))}",
            algorithm="sha256",
        ).hexdigest()
        for field in FIELDS
    }


def merge_contact_fields(submitted, fresh_raw, *, baseline, user_id, root_id):
    if type(baseline) is not dict or set(baseline) != set(FIELDS):
        raise ValueError("contact_provenance_required")
    if fresh_raw.get("id") != root_id:
        raise ValueError("contact_root_mismatch")
    posted = {**submitted, **submitted.get("billing_address", {})}
    hashes = field_provenance(posted, user_id=user_id, root_id=root_id)
    fresh = contact_values(fresh_raw)
    merged = {
        field: fresh[field] if hashes[field] == baseline[field] else posted.get(field, "")
        for field in FIELDS
    }
    result = dict(submitted)
    result.update({field: merged[field] for field in GUEST_FIELDS})
    result["billing_address"] = {field: merged[field] for field in BILLING_FIELDS}
    return result


def provider_context(raw):
    """Allowlisted evidence from an already performed GET; no clock fallback."""
    item = validate_reservation_response({"status": "success", "result": raw})
    snapshot = source_snapshot(raw)
    guest = guest_initial(raw)
    timestamp_field = "updatedOn" if raw.get("updatedOn") else "latestActivityOn"
    timestamp_raw = raw.get(timestamp_field)
    timestamp_raw = timestamp_raw if isinstance(timestamp_raw, str) else None
    # The existing validator applies the server timezone to naive values. Do
    # not turn that assumption into provider chronology in the new wire.
    parsed = parse_datetime(timestamp_raw) if timestamp_raw else None
    timestamp = parsed.isoformat() if parsed and timezone.is_aware(parsed) else None
    return {
        "hostaway_reservation_id": item.reservation_id,
        "listing_map_id": item.listing_map_id,
        "status": snapshot["status"],
        "provider_status": item.status,
        "provider_updated_at": timestamp,
        "provider_updated_at_raw": timestamp_raw,
        "provider_updated_at_field": timestamp_field if timestamp_raw else None,
        "contact": guest,
    }


def draft_contact_values(draft):
    return {
        **{key: getattr(draft, key) for key in GUEST_FIELDS},
        **{key: (draft.billing_address or {}).get(key, "") for key in BILLING_FIELDS},
    }


def _draft_hashes(draft, values, root_id):
    return {
        key: salted_hmac(
            "extension-draft-contact-1",
            f"{draft.pk}:{root_id}:{key}:{_canonical(key, values.get(key))}",
            algorithm="sha256",
        ).hexdigest()
        for key in FIELDS
    }


def source_field_origins(raw, root_id):
    kind = "hostaway_root" if raw["id"] == root_id else "selected_hostaway_pending_root"
    values = contact_values(raw)
    origins = {key: kind for key in GUEST_FIELDS}
    if not raw.get("guestLocale"):
        origins["guest_language"] = "default_language"
    for key, provider in BILLING_PROVIDER_FIELDS.items():
        origins[key] = (
            kind if provider in raw else ("local_saved_address" if values[key] else "unavailable")
        )
    return origins


def make_draft_provenance(extension, *, explicit_fields, source_id, revision=1, source_fields=None):
    draft, chain = extension.draft, extension.chain
    explicit = sorted(set(explicit_fields))
    if set(explicit) - set(FIELDS):
        raise ValueError("contact_fields_invalid")
    source_fields = source_fields or (draft.extension_contact_provenance or {}).get("field_sources")
    if type(source_fields) is not dict or set(source_fields) != set(FIELDS):
        raise ValueError("contact_field_origins_required")
    return {
        "contract": PROVENANCE_CONTRACT,
        "draft_id": str(draft.pk),
        "root_hostaway_reservation_id": chain.root_hostaway_reservation_id,
        "stay_chain_id": str(chain.pk),
        "chain_revision": chain.revision,
        "revision": revision,
        "source_hostaway_reservation_id": source_id,
        "baseline_hmac": _draft_hashes(
            draft, draft_contact_values(draft), chain.root_hostaway_reservation_id
        ),
        "explicit_fields": explicit,
        "field_sources": {
            key: "administrator" if key in explicit else source_fields[key] for key in FIELDS
        },
    }


def validate_draft_provenance(extension):
    draft, chain = extension.draft, extension.chain
    proof = draft.extension_contact_provenance
    keys = {
        "contract",
        "draft_id",
        "root_hostaway_reservation_id",
        "stay_chain_id",
        "chain_revision",
        "revision",
        "source_hostaway_reservation_id",
        "baseline_hmac",
        "explicit_fields",
        "field_sources",
    }
    if (
        type(proof) is not dict
        or set(proof) != keys
        or type(proof.get("explicit_fields")) is not list
        or any(type(key) is not str for key in proof["explicit_fields"])
        or type(proof.get("source_hostaway_reservation_id")) is not int
        or proof["source_hostaway_reservation_id"]
        not in {chain.root_hostaway_reservation_id, extension.previous_hostaway_reservation_id}
    ):
        raise ValueError("extension_contact_provenance_required")
    sources = proof.get("field_sources")
    if type(sources) is not dict or set(sources) != set(FIELDS):
        raise ValueError("extension_contact_provenance_required")
    for key in FIELDS:
        if key in proof["explicit_fields"]:
            allowed = {"administrator"}
        else:
            allowed = {
                "hostaway_root"
                if proof["source_hostaway_reservation_id"] == chain.root_hostaway_reservation_id
                else "selected_hostaway_pending_root"
            }
            if key in BILLING_FIELDS:
                allowed |= {"local_saved_address", "unavailable"}
            if key == "guest_language":
                allowed.add("default_language")
        if sources[key] not in allowed:
            raise ValueError("extension_contact_provenance_required")
    if (
        type(proof) is not dict
        or proof.get("contract") != PROVENANCE_CONTRACT
        or proof.get("draft_id") != str(draft.pk)
        or proof.get("root_hostaway_reservation_id") != chain.root_hostaway_reservation_id
        or proof.get("stay_chain_id") != str(chain.pk)
        or type(proof.get("revision")) is not int
        or proof["revision"] < 1
        or type(proof.get("chain_revision")) is not int
        or proof["chain_revision"] < 0
        or proof["chain_revision"] > chain.revision
        or type(proof.get("explicit_fields")) is not list
        or set(proof["explicit_fields"]) - set(FIELDS)
        or proof["explicit_fields"] != sorted(set(proof["explicit_fields"]))
        or proof.get("baseline_hmac")
        != _draft_hashes(draft, draft_contact_values(draft), chain.root_hostaway_reservation_id)
    ):
        raise ValueError("extension_contact_provenance_required")
    return proof


def changed_fields(before, after):
    return {
        key for key in FIELDS if _canonical(key, before.get(key)) != _canonical(key, after.get(key))
    }


def provenance_for_save(extension, submitted):
    proof = validate_draft_provenance(extension)
    if (
        type(submitted.get("extension_contact_revision")) is not int
        or submitted["extension_contact_revision"] != proof["revision"]
    ):
        raise ValueError("extension_contact_revision_changed")
    after = {**submitted, **submitted.get("billing_address", {})}
    return set(proof["explicit_fields"]) | changed_fields(
        draft_contact_values(extension.draft), after
    )


@dataclass(frozen=True)
class ExtensionRootRead:
    draft_id: str
    root_id: int
    chain_revision: int
    raw: dict = dataclass_field(repr=False)
    checked_at: float = dataclass_field(default_factory=monotonic)

    def reusable(self, extension):
        return (
            self.draft_id == str(extension.draft_id)
            and self.root_id == extension.previous_hostaway_reservation_id
            and self.root_id == extension.chain.root_hostaway_reservation_id
            and self.chain_revision == extension.chain.revision
            and 0 <= monotonic() - self.checked_at <= 10
        )


def refresh_before_new_intent(extension, *, client=None):
    """Called under the draft lock, only while there is no existing intent."""
    from .stay_extension_views import ExtensionGuestForm

    draft, chain = extension.draft, extension.chain
    proof = validate_draft_provenance(extension)
    raw = read_source(chain.root_hostaway_reservation_id, client=client)
    if source_snapshot(raw) != chain.root_snapshot or chain.review_required:
        raise ValueError("extension_contact_root_changed")
    current = StayExtensionChain.objects.values_list("revision", "fingerprint").get(pk=chain.pk)
    if current != (chain.revision, chain.fingerprint):
        raise ValueError("extension_contact_chain_changed")
    fresh = contact_values(raw)
    saved = draft_contact_values(draft)
    values = {key: saved[key] if key in proof["explicit_fields"] else fresh[key] for key in FIELDS}
    check = ExtensionGuestForm(
        {
            **values,
            "guest_phone_country": "SA",
            "final_total_price": draft.final_total_price.quantize(Decimal("0.01")),
            "extension_contact_revision": proof["revision"],
        },
        draft=draft,
        require_address=True,
    )
    if not check.is_valid():
        raise ValueError("extension_current_contact_invalid")
    for key in GUEST_FIELDS:
        setattr(draft, key, check.cleaned_data[key])
    draft.billing_address = dict(check.cleaned_data["billing_address"])
    draft.extension_contact_provenance = make_draft_provenance(
        extension,
        explicit_fields=proof["explicit_fields"],
        source_id=raw["id"],
        revision=proof["revision"] + 1,
        source_fields=source_field_origins(raw, chain.root_hostaway_reservation_id),
    )
    draft.full_clean()
    draft.save(
        update_fields=[
            *GUEST_FIELDS,
            "billing_address",
            "extension_contact_provenance",
            "updated_at",
        ]
    )
    return ExtensionRootRead(str(draft.pk), raw["id"], chain.revision, raw)
