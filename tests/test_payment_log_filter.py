import logging

import pytest
from django.conf import settings

from apps.core.payment_log_filter import PaymentCapabilityFilter


@pytest.mark.parametrize(
    "message,args",
    [
        ("GET /payments/pay/secret:signature/ HTTP/1.1 200", ()),
        ("GET %s HTTP/1.1 200", ("/payments/pay/secret:signature/",)),
        ("%(r)s %(s)s", {"r": "GET /payments/pay/secret:signature/ HTTP/1.1", "s": 200}),
        (
            "GET /payments/hyperpay/result/uuid/?return_token=secret:signature&resourcePath=ok 200",
            (),
        ),
        ("Referer: https://booking.example/payments/pay/secret:signature/?source=message 200", ()),
    ],
)
def test_redacts_capabilities_without_hiding_http_evidence(message, args):
    record = logging.LogRecord("access", logging.INFO, __file__, 1, message, args, None)
    assert PaymentCapabilityFilter().filter(record)
    result = record.getMessage()
    assert "secret:signature" not in result
    assert "[redacted]" in result and "200" in result
    assert PaymentCapabilityFilter().filter(record)
    assert record.getMessage() == result


def test_ordinary_logs_are_preserved_and_access_loggers_are_wired():
    record = logging.LogRecord(
        "app", logging.INFO, __file__, 1, "Receipt status %s", ("pending",), None
    )
    PaymentCapabilityFilter().filter(record)
    assert record.getMessage() == "Receipt status pending"
    config = settings.LOGGING
    assert config["loggers"]["gunicorn.access"]["handlers"] == ["console"]
    assert "payment_capabilities" in config["handlers"]["console"]["filters"]
    assert "payment_capabilities" in config["loggers"]["django.server"]["filters"]
