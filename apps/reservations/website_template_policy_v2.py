"""Rebuild prepared v2 content from independent private source, never its body."""

from django.views.decorators.debug import sensitive_variables

from . import message_source_authorization as source
from . import website_financial_source_v2 as financial
from . import website_template_selector_v2 as selector
from .website_access_source_v2 import access_source_values


@sensitive_variables()
def policy_from_source(event, facts, context):
    scenario = facts.get("scenario")
    if facts.get("source_message_type") in {
        "review_followup_1",
        "review_followup_2",
        "review_followup_3",
    }:
        from .website_review_source_v2 import policy_from_source as review_policy

        return review_policy(event, facts, context)
    if scenario == "02" and facts.get("source_family") == "lsa_ext_checkout":
        from .extension_checkout_source_v2 import policy_from_source as extension_policy

        return extension_policy(event, facts, context)
    if scenario not in source.SOURCES or not isinstance(context, dict):
        return None
    kind = facts.get("source_message_type")
    try:
        current = None
        if kind == "payment_link":
            resource = facts.get("financial_identity", {}).get("delivery_id")
            current = financial.payment_link_source(resource, scenario)
            family = f"lsa{scenario}_payment_link"
        elif kind == "payment_success":
            current = financial.payment_confirmation_source(event.pk, scenario)
            family = f"lsa{scenario}_payment"
        elif kind == "loyalty_invitation":
            from .website_loyalty_source_v2 import source as loyalty_source

            current = loyalty_source(event.pk, scenario)
            if current.get("domain_authorized") is not True:
                return None
            family = f"lsa{scenario}_payment"
        elif kind in source.PURPOSES and kind not in {"loyalty_invitation"}:
            if not isinstance(event, source.SOURCES[scenario][1]):
                return None
            event = type(event).objects.select_related("stay__property").get(pk=event.pk)
            if not source.current_core(event.stay, event, source.SOURCES[scenario][4]):
                return None
            key = source.core_keys(event.stay.pk, event.payload, scenario).get(kind)
            if not key or source.sha(key) != facts.get("job_sha256"):
                return None
            current = source.core_facts(event.stay, event, scenario, facts["job_sha256"])
            family = f"lsa{scenario}"
        else:
            return None
        if not current or current.get("available") is False:
            return None
        for key in (
            "reservation_key",
            "reservation_revision",
            "recipient_ref",
            "recipient_e164",
            "source_event_key",
            "job_sha256",
            "source_message_type",
            "purpose",
            "language",
        ):
            if type(current.get(key)) is not type(facts.get(key)) or current[key] != facts[key]:
                return None
        canonical_facts = {**current, "source_family": family, "source_owner_family": "website"}
        bound = selector.match_context(canonical_facts, context)
        if kind in {"payment_link", "payment_success", "loyalty_invitation"}:
            # Financial variables and URLs are re-read from source01 under its locks.
            # A backend render-source view cannot replace those values.
            if bound.get("values") != current.get("variables"):
                return None
            if kind == "loyalty_invitation" and bound.get("loyalty_identity") != current.get(
                "loyalty_identity"
            ):
                return None
        else:
            local = financial.core_variables(event, scenario)
            values = bound.get("values")
            if not isinstance(values, dict):
                return None
            if any(k in values and values[k] != v for k, v in local.items()):
                return None
            if (
                kind == "checkin_guide"
                and access_source_values(event, current, bound).get("available") is not True
            ):
                return None
        selected = selector.select_source_message(canonical_facts, bound)
        return {
            "language": bound["language"],
            "message": selected.message,
            "render_policy_ref": selected.selection_ref,
        }
    except Exception:
        # Neither source values, PDF bytes, capability nor transport errors are logged.
        return None
