"""Redact payment capabilities, not operational HTTP status or timing evidence."""

import logging
import re

_PATH = re.compile(r"(/payments/pay/)[^/?#\s\"'<>]+", re.IGNORECASE)
_RETURN = re.compile(r"([?&]return_token=)[^&\s\"'<>]+", re.IGNORECASE)


class PaymentCapabilityFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = _PATH.sub(r"\1[redacted]", message)
        record.msg = _RETURN.sub(r"\1[redacted]", message)
        record.args = ()
        return True
