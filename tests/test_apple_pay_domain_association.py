import pytest
from django.http import Http404
from django.test import RequestFactory, override_settings
from django.urls import resolve

APPLE_PAY_ASSOCIATION_PATH = (
    "/.well-known/apple-developer-merchantid-domain-association.txt"
)


def test_apple_pay_domain_association_serves_exact_configured_file(tmp_path) -> None:
    expected = b"apple-domain-verification-token\n"
    association_file = tmp_path / "apple_pay_domain_association"
    association_file.write_bytes(expected)

    with override_settings(APPLE_PAY_DOMAIN_ASSOCIATION_FILE=str(association_file)):
        view = resolve(APPLE_PAY_ASSOCIATION_PATH).func
        response = view(RequestFactory().get(APPLE_PAY_ASSOCIATION_PATH))

    assert response.status_code == 200
    assert response.content == expected
    assert response["Content-Type"] == "text/plain"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_apple_pay_domain_association_is_404_until_secret_file_is_configured(
    tmp_path,
) -> None:
    missing_file = tmp_path / "not-configured"

    with override_settings(APPLE_PAY_DOMAIN_ASSOCIATION_FILE=str(missing_file)):
        view = resolve(APPLE_PAY_ASSOCIATION_PATH).func
        with pytest.raises(Http404):
            view(RequestFactory().get(APPLE_PAY_ASSOCIATION_PATH))
