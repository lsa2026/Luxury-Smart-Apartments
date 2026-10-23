"""Synthetic SDK examples used for validation only, never runtime data."""

IDENTIFIER = "07000000-0000-4000-8000-000000000001"
BOOKING = {
    "source": "morning07",
    "morning_stay_id": IDENTIFIER,
    "reservation_key": "morning07:" + IDENTIFIER,
    "source_revision": 1,
    "phone": "+16502530000",
    "guest_name": "Synthetic Guest",
    "guest_email": "synthetic@example.invalid",
    "guest_language": "en",
    "payment_verified": True,
    "status": "paid",
    "delivery_token": "synthetic-capability-only",
}
REFERENCE = {"valid": True, "event_id": IDENTIFIER, "event_token": "synthetic-capability-only"}
CONTACT = {
    "first": "Synthetic",
    "last": "Guest",
    "phone": "16502530000",
    "email": "synthetic@example.invalid",
}
CONTACT_STATE = {
    "status": "working",
    "request": {
        "method": "PATCH",
        "url": "https://people.googleapis.com/v1/people/synthetic:updateContact",
        "body": {},
    },
    "guest": CONTACT,
    "contact_error": "",
}
JOB = {
    **BOOKING,
    "action": "send",
    "job_key": "lsa07:" + IDENTIFIER + ":r1:status",
    "message_type": "status_new",
    "recipient_kind": "guest",
    "recipient_phone": "16502530000",
    "message_body": "Synthetic confirmation",
    "reference_id": "synthetic-reference",
    "provider_message_id": "",
    "wait_seconds": 15,
}
CLAIM = {
    "has_work": True,
    "booking": BOOKING,
    "previous": {},
    "apartment": {},
    "templates": [],
    "work_group_id": "synthetic-group",
    "now": "2032-01-01T00:00:00Z",
}


def sample(name):
    if name == "M00 Morning07 Website Event":
        return {"body": REFERENCE}
    if name.startswith(("M01 Validate", "M01A")):
        return REFERENCE
    if name.startswith("M02"):
        return {"statusCode": 200, "body": BOOKING}
    if name.startswith(("M03", "M04 Admission")):
        return {**BOOKING, "valid": True}
    if name.startswith("M05"):
        return {"accepted": True, "revision": 1}
    if name.startswith(("M06", "M07")):
        return {"accepted": True, "event_id": IDENTIFIER}
    if name.startswith(("B00", "B01")):
        return CLAIM
    if name.startswith("B02"):
        return {"stay_id": IDENTIFIER, "revision": 1, "jobs": []}
    if name.startswith("B03"):
        return {"prepared": True}
    if name.startswith(("D00", "D01", "D50")):
        return JOB
    if name.startswith("D10"):
        return {"statusCode": 202, "body": {"sent": "true", "id": "synthetic-provider"}}
    if name.startswith("D20"):
        return {"statusCode": 200, "body": {"messages": []}}
    if name.startswith("D30"):
        return {"job_key": JOB["job_key"], "result": {"delivered": False, "provider_id": ""}}
    if name.startswith("D40"):
        return {"recorded": True, "delivered": False}
    if name.startswith(("C00", "C01")):
        return {**BOOKING, "has_contact": True, "previous_phones": []}
    if name.startswith("GC00"):
        return CONTACT
    if name.startswith(("GC02", "GC03", "GC04", "GC06", "GC07")):
        return CONTACT_STATE
    if name.startswith("E00"):
        return {"execution": {"id": "synthetic-execution"}}
    return {"ok": True}
