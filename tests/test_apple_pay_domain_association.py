"""Apple must retrieve the exact verification bytes without a language redirect."""

import pytest
from django.test import Client, override_settings

PATHS = (
    "/.well-known/apple-developer-merchantid-domain-association",
    "/.well-known/apple-developer-merchantid-domain-association.txt",
)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("method", ["get", "head"])
def test_domain_association_exact_bytes_no_redirect(tmp_path, path, method):
    content = b"apple-domain-association\r\nunchanged-bytes\n"
    filename = tmp_path / "apple_pay_domain_association"
    filename.write_bytes(content)
    with override_settings(APPLE_PAY_DOMAIN_ASSOCIATION_FILE=str(filename)):
        response = getattr(Client(), method)(path, HTTP_ACCEPT_LANGUAGE="fr")
    assert response.status_code == 200
    assert "Location" not in response
    assert response.content == (content if method == "get" else b"")
    assert response["Content-Type"] == "text/plain"
    assert response["X-Content-Type-Options"] == "nosniff"


@pytest.mark.django_db
@pytest.mark.parametrize("content", [None, b""])
def test_missing_or_empty_domain_file_fails_closed(tmp_path, content):
    filename = tmp_path / "unconfigured"
    if content is not None:
        filename.write_bytes(content)
    with override_settings(APPLE_PAY_DOMAIN_ASSOCIATION_FILE=str(filename)):
        response = Client().get(PATHS[0])
    assert response.status_code == 404
    assert "Location" not in response


def test_domain_association_does_not_accept_writes():
    assert Client().post(PATHS[0]).status_code == 405
