"""Codes and signed links used to prove a guest controls an email address.

Password reset keeps Django's own generator, which invalidates a link as soon as
the password changes. Verification needs different properties, so it is signed
separately here:

* the address is inside the payload, so a link issued for one address cannot
  confirm a different one after the customer edits it;
* the signature is namespaced, so a verification token can never be replayed
  against another feature that also signs values;
* nothing is stored server-side, so an abandoned link simply expires.
"""

from django.conf import settings
from django.core import signing
from django.utils.crypto import constant_time_compare, salted_hmac

VERIFICATION_SALT = "accounts.email-verification.v1"
VERIFICATION_MAX_AGE_SECONDS = 3 * 24 * 60 * 60


def make_verification_code(user_pk: int, email: str, issued_at: object) -> str:
    """Derive a six-digit code without storing a reusable secret in the database."""
    timestamp = int(issued_at.timestamp())  # type: ignore[union-attr]
    payload = f"{int(user_pk)}:{(email or '').strip().casefold()}:{timestamp}"
    digest = salted_hmac("accounts.email-verification-code.v1", payload).hexdigest()
    return f"{int(digest[:12], 16) % 1_000_000:06d}"


def verification_code_is_valid(
    *,
    user_pk: int,
    email: str,
    issued_at: object | None,
    submitted_code: str,
) -> bool:
    """Accept only the current, short-lived code sent to this exact address."""
    if issued_at is None:
        return False
    from django.utils import timezone

    if (
        timezone.now() - issued_at
    ).total_seconds() > settings.ACCOUNT_EMAIL_VERIFICATION_CODE_MAX_AGE_SECONDS:
        return False
    expected = make_verification_code(user_pk, email, issued_at)
    return constant_time_compare(expected, submitted_code.strip())


def make_verification_token(user_pk: int, email: str) -> str:
    return signing.dumps(
        {"uid": int(user_pk), "email": (email or "").strip().casefold()},
        salt=VERIFICATION_SALT,
    )


def read_verification_token(token: str) -> tuple[int, str] | None:
    """Return ``(user_pk, email)`` for a valid token, or ``None``.

    Every failure mode collapses to ``None`` on purpose: a caller must not be
    able to tell a forged link from an expired one.
    """
    try:
        payload = signing.loads(
            token,
            salt=VERIFICATION_SALT,
            max_age=getattr(
                settings,
                "ACCOUNT_VERIFICATION_MAX_AGE_SECONDS",
                VERIFICATION_MAX_AGE_SECONDS,
            ),
        )
    except (signing.BadSignature, signing.SignatureExpired, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    email = payload.get("email")
    if not isinstance(uid, int) or not isinstance(email, str) or not email:
        return None
    return uid, email
