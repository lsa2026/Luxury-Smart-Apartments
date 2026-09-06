from html.parser import HTMLParser

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


class MainImageAltParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.main_depth = 0
        self.image_alts: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "main":
            self.main_depth += 1
        elif tag == "img" and self.main_depth:
            self.image_alts.append(attributes.get("alt"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "main" and self.main_depth:
            self.main_depth -= 1


def test_every_homepage_content_image_has_meaningful_alt_text() -> None:
    parser = MainImageAltParser()
    parser.feed(Client().get("/").content.decode())

    assert parser.image_alts
    assert all(alt and alt.strip() for alt in parser.image_alts)


def test_homepage_support_ratio_is_isolated_in_rtl() -> None:
    content = Client().get("/").content.decode()

    assert '<bdi dir="ltr">٢٤/٧</bdi>' in content
