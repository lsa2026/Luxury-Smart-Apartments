"""Signed, expiring tokens for email verification.

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

VERIFICATION_SALT = "accounts.email-verification.v1"
VERIFICATION_MAX_AGE_SECONDS = 3 * 24 * 60 * 60


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
