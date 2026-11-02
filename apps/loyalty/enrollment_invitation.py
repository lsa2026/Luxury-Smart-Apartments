"""Enrollment invitation copy/artwork matching the published n8n renderer."""

import json
import unicodedata
from base64 import b64decode
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files

from django.core.exceptions import ValidationError

DEMO_NAMES = {"ar": "نورة الحربي", "en": "Emma Wilson", "fr": "Nora Martin"}
QUALIFIED_COPY = {
    "ar": (
        "لتستمتع بمكافآت إقامتك الحالية وإقاماتك القادمة",
        "لتستمتع بمكافآت إقاماتك القادمة المؤهلة المحجوزة عبر موقعنا",
        "الحجز عبر منصة خارجية لا يمنح نقاط إقامة أو مكافآت بأثر رجعي.",
    ),
    "en": (
        "to enjoy rewards on your current and future stays",
        "to enjoy rewards on future eligible stays booked through our website",
        "This external-platform booking earns no stay points or retrospective rewards.",
    ),
    "fr": (
        "pour profiter de récompenses sur vos séjours actuels et futurs",
        "pour profiter de récompenses sur vos futurs séjours éligibles réservés sur notre site",
        "Ce séjour réservé sur une autre plateforme ne génère pas de points "
        "ni de droits rétroactifs.",
    ),
}


@lru_cache(maxsize=3)
def sample(language):
    if language not in DEMO_NAMES:
        raise ValidationError("Unsupported invitation language.")
    data = json.loads(
        files("apps.loyalty")
        .joinpath("invitation_assets", "enrollment-owner-review-v3.json")
        .read_text(encoding="utf-8")
    )
    return next(item for item in data["samples"] if item["language"] == language)


def invitation_image(language):
    item = sample(language)
    content = b64decode(item["image_base64"], validate=True)
    if sha256(content).hexdigest() != item["image_sha256"]:
        raise ValidationError("Invitation artwork checksum mismatch.")
    return content


def invitation_caption(language, url, guest_name=""):
    name = (
        " ".join(
            "".join(
                " " if unicodedata.category(c) in {"Cc", "Cf"} else c
                for c in unicodedata.normalize("NFC", guest_name or "")
                if c not in "<>*_`~"
            ).split()
        )
        or {"ar": "ضيفنا العزيز", "en": "our valued guest", "fr": "cher client"}[language]
    )
    if len(name) > 150:
        raise ValidationError("Invitation guest name is too long.")
    old, qualified, limitation = QUALIFIED_COPY[language]
    caption = sample(language)["caption"].replace(DEMO_NAMES[language], name, 1)
    caption = caption.replace(old, qualified, 1).rstrip() + "\n\n" + limitation + "\n\n" + url
    if len(caption.encode("utf-16-le")) // 2 > 1024:
        raise ValidationError("Invitation caption exceeds the provider limit.")
    return caption
