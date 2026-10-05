from urllib.parse import parse_qs, urlsplit

import pytest

EXPECTED_CAMPAIGN = {
    "utm_source": ["whatsapp"],
    "utm_medium": ["messaging"],
    "utm_campaign": ["missed_call"],
    "utm_content": ["scenario_05"],
}


@pytest.mark.parametrize("path", ["/hello", "/hello/"])
@pytest.mark.parametrize("language", ["ar", "en"])
@pytest.mark.parametrize("method", ["get", "head"])
def test_short_link_redirects_to_localized_home_with_fixed_utms(client, path, language, method):
    response = getattr(client, method)(path, HTTP_ACCEPT_LANGUAGE=language)
    target = urlsplit(response["Location"])
    assert response.status_code == 302
    assert target.path == f"/{language}/"
    assert target.netloc == ""
    assert parse_qs(target.query) == EXPECTED_CAMPAIGN
    assert "no-store" in response["Cache-Control"]
    assert response["X-Robots-Tag"] == "noindex"


def test_incoming_parameters_cannot_override_campaign_or_leak_personal_data(client):
    response = client.get(
        "/hello",
        {
            "utm_source": "other",
            "next": "https://example.org/",
            "phone": "test-only-not-a-real-number",
            "email": "test@example.invalid",
        },
    )
    target = urlsplit(response["Location"])
    assert target.netloc == ""
    assert parse_qs(target.query) == EXPECTED_CAMPAIGN


def test_short_link_does_not_accept_post(client):
    assert client.post("/hello").status_code == 405
