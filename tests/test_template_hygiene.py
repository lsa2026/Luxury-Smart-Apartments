"""Structural guards for the template sources themselves."""

import re
from pathlib import Path

import pytest
from django.conf import settings

TEMPLATE_ROOT = Path(settings.BASE_DIR) / "templates"


def template_files() -> list[Path]:
    return sorted(TEMPLATE_ROOT.rglob("*.html"))


def test_templates_exist_to_be_checked() -> None:
    # Guards the guard: a bad glob would otherwise make the scan below vacuous.
    assert len(template_files()) > 20


@pytest.mark.parametrize("template", template_files(), ids=lambda p: p.name)
def test_short_comments_never_span_lines(template: Path) -> None:
    """``{# #}`` is single-line only; spanning lines renders it to the visitor.

    Django's short comment is closed by the end of the line, not by ``#}``, so a
    wrapped one silently becomes page copy. Multi-line notes belong in
    ``{% comment %}``.
    """
    source = template.read_text(encoding="utf-8")

    offenders = []
    for match in re.finditer(r"\{#", source):
        tail = source[match.start() :]
        closing = tail.find("#}")
        if closing != -1 and "\n" in tail[:closing]:
            offenders.append(source[: match.start()].count("\n") + 1)

    assert not offenders, (
        f"{template.relative_to(TEMPLATE_ROOT)} has a multi-line {{# #}} comment on "
        f"line(s) {offenders}; use {{% comment %}} so it is not rendered."
    )
