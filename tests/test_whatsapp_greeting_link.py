from urllib.parse import parse_qs, urlsplit

import pytest

EXPECTED_QUERY = (
    "utm_source=whatsapp&utm_medium=messaging"
    "&utm_campaign=whatsapp_greeting&utm_content=business_app_greeting"
)


@pytest.mark.parametrize("path", ["/welcome", "/welcome/"])
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
@pytest.mark.parametrize("method", ["get", "head"])
def test_greeting_has_fixed_localized_destination(client, path, language, method):
    response = getattr(client, method)(path, HTTP_ACCEPT_LANGUAGE=language)

    assert response.status_code == 302
    assert response["Location"] == f"/{language}/?{EXPECTED_QUERY}"
    assert response["Cache-Control"] == "no-store, max-age=0"
    assert response["X-Robots-Tag"] == "noindex"
    if method == "head":
        assert response.content == b""


@pytest.mark.parametrize("path", ["/welcome", "/welcome/"])
def test_greeting_drops_untrusted_query_and_personal_identifiers(client, path):
    response = client.get(
        path,
        {
            "utm_source": "untrusted",
            "utm_medium": "email",
            "utm_campaign": "missed_call",
            "utm_content": "scenario_05",
            "next": "https://example.invalid/",
            "url": "//example.invalid/",
            "phone": "synthetic-only",
            "email": "test@example.invalid",
            "recipient": "synthetic-only",
            "message_id": "synthetic-only",
        },
        HTTP_ACCEPT_LANGUAGE="ar",
    )

    assert response.status_code == 302
    assert response["Location"] == f"/ar/?{EXPECTED_QUERY}"
    target = urlsplit(response["Location"])
    assert not target.scheme
    assert not target.netloc
    assert len(parse_qs(target.query)) == 4


@pytest.mark.parametrize("path", ["/welcome", "/welcome/"])
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "options", "trace"])
def test_greeting_only_accepts_get_and_head(client, path, method):
    response = getattr(client, method)(path)

    assert response.status_code == 405
    assert response["Allow"] == "GET, HEAD"
    assert "Location" not in response
