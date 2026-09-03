"""Map HyperPay result codes once, away from views and booking logic."""

import re
from enum import StrEnum


class HyperPayStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REVIEW = "review"
    UNKNOWN = "unknown"


_SUCCESS = re.compile(r"^(?:000\.000\.|000\.100\.1|000\.[36]|000\.400\.1[12]0)")
_REVIEW = re.compile(r"^(?:000\.400\.0[^3]|000\.400\.100)")
_PENDING = re.compile(r"^(?:000\.200\.|800\.400\.5|100\.400\.500)")
_CANCELLED = {"100.396.101", "100.396.102", "100.396.103", "100.396.104"}
_VALID_CODE = re.compile(r"^\d{3}\.\d{3}\.\d{3}$")


def map_result_code(code: str) -> HyperPayStatus:
    if not isinstance(code, str) or not _VALID_CODE.fullmatch(code):
        return HyperPayStatus.UNKNOWN
    if code in _CANCELLED:
        return HyperPayStatus.CANCELLED
    if _REVIEW.match(code):
        return HyperPayStatus.REVIEW
    if _SUCCESS.match(code):
        return HyperPayStatus.SUCCESS
    if _PENDING.match(code):
        return HyperPayStatus.PENDING
    return HyperPayStatus.FAILED
