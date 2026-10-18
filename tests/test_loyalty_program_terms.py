"""Owner-approved terms publication; no financial or retrospective consent writes."""
# Complete approved legal wording is pinned verbatim rather than reflowed.
# ruff: noqa: E501

from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import translation

from apps.accounts.models import PhoneChallenge, VerifiedPhone
from apps.loyalty.accrual import (
    IDENTITY_TERMS_REFERENCE,
    IDENTITY_TERMS_VERSION,
    PROGRAM_TERMS_REFERENCE,
    PROGRAM_TERMS_VERSION,
    SIGNED_TERMS_REFERENCE,
    SIGNED_TERMS_VERSION,
    policy_enabled,
)
from apps.loyalty.models import GuestEvent, Ledger, Member
from apps.loyalty.public_terms import (
    PROGRAM_CONTENT,
    SIGNED_CONTENT,
    consent_metadata,
    current_terms_version,
    document,
    terms_reference,
)
from tests.test_loyalty_stage8 import join, user

pytestmark = pytest.mark.django_db

HISTORIC_HASHES = {
    "loyalty-public-20261016-v1": {
        "ar": "6147ef2c8e2eef4078b311a55cc40249536f9e649153f638436fea23f77e1217",
        "en": "5c014d8d657463f68025b05b573daf719defded48ba2a7b4d4219bbe4c550de3",
        "fr": "f790a0a2718a5ab652db9aa2f7a3650e53d72837a216bbd742fd98d6d517dd0a",
    },
    "loyalty-public-20261016-v2": {
        "ar": "43e98d965fcae36620a077fa2ec0092574ae154c84eff958f50e4edb7cad6c51",
        "en": "03fdbff222a7edc6a7d5beb272a1613f10cf24a9aa85ee2f73875742854ddc0e",
        "fr": "1b079730d0a5ccbdf34e640fe7f82cabe9a1e62d032f58685bd87562f7eab24d",
    },
    "loyalty-public-20261017-v3": {
        "ar": "ac92b200a7f25f75014939b0146b4ada5f7926cbe922959fbd27abed8d4d08b4",
        "en": "ba9c85735e08f482bcd40ff08e6572f31ba914a1a9462855f8e2bad69899b28c",
        "fr": "54da1dc398c4b3006ae11db5b79c534a9471ad19943f8804b50095d41c3290de",
    },
    "loyalty-public-20261017-v4": {
        "ar": "84daac7e6f0fd58073e7065376af757842d17c13bf44700f6ec83c8087aaa62a",
        "en": "b3e9f11ccae46307a3c26aefc18b1bcd87f7d63cab4f2ad0f4d9a19a0a94098c",
        "fr": "de4b2121c099e92f69a86df7bd82b10f96ad1ac765c663dfb2d8b740aaf47336",
    },
}

APPROVED_AR = "تحتفظ المؤسسة بحق تعديل برنامج الولاء أو تعليقه أو إنهائه كليًا أو جزئيًا لأسباب تجارية أو تشغيلية مشروعة، ومنها الخسارة أو تعذر استمرار توفير الإقامة أو خروج العقارات المشاركة من التشغيل، دون اشتراط موافقة لاحقة من العضو، مع إعلان القرار وإبلاغ الأعضاء بموعد نفاذه وآثاره على النقاط.\n\nويوافق العضو مسبقًا، في الحدود الجائزة نظامًا، على إعفاء المؤسسة من التعويض عن الأضرار الناشئة عن تعديل البرنامج أو تعليقه أو إنهائه، وعدم مطالبتها بالتعويض عن تلك الأضرار ضمن هذه الحدود.\n\nإذا ثبت أن قوة قاهرة أو سببًا خارجًا عن إرادة المؤسسة أدى إلى استحالة نهائية لتنفيذ المكافآت غير المستخدمة ضمن نطاق البرنامج، ينقضي الالتزام المتأثر بقدر الاستحالة ومن وقت تحققها، وفق الأحكام النظامية وما يترتب عليها من آثار. ولا يُعد قرار الإنهاء وحده إثباتًا للاستحالة، ولا ينقضي الالتزام فيما بقي تنفيذه ممكنًا وفق شروطه.\n\nتبقى الحجوزات المؤكدة نافذة وتُنفذ وفق شروطها، ولا يترتب على إنهاء البرنامج إلغاؤها تلقائيًا. ولا يُفترض سريان أي تعديل على حقوق سابقة بالمخالفة للشروط التي نشأت بموجبها أو الأحكام النظامية."


@pytest.fixture(autouse=True)
def publication(settings):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = True
    settings.LOYALTY_PILOT_ONLY = False
    settings.LOYALTY_UI_PREVIEW = False
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_VERSION = SIGNED_TERMS_VERSION
    settings.LOYALTY_TERMS_REFERENCE = SIGNED_TERMS_REFERENCE
    settings.LOYALTY_LAUNCH_AT = "2026-10-01T00:00:00+03:00"
    with translation.override("ar"):
        yield


@pytest.mark.parametrize("version", HISTORIC_HASHES)
@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_historical_terms_content_hashes_are_unchanged(version, language):
    assert document(language, version=version)["sha256"] == HISTORIC_HASHES[version][language]


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_new_terms_are_readable_without_login_and_create_nothing(language, settings):
    settings.LOYALTY_ENABLED = settings.LOYALTY_UI_ENABLED = False
    settings.LOYALTY_TERMS_APPROVED = False
    client = Client()
    url = f"/{language}/legal/rewards/2026-10-18-v5/"
    response = client.get(url)
    issued = document(language, version=PROGRAM_TERMS_VERSION)
    assert response.status_code == 200
    assert response["X-Document-Version"] == PROGRAM_TERMS_VERSION
    assert response["X-Document-SHA256"] == issued["sha256"]
    assert len(issued["sections"]) == 17
    assert issued["sections"][:12] == SIGNED_CONTENT[language]["sections"][:12]
    assert issued["sections"][14][1] == SIGNED_CONTENT[language]["sections"][13][1]
    assert issued["sections"][13][0] in response.content.decode()
    assert client.head(url).status_code == 200
    assert client.post(url, {}).status_code == 405
    for model in (Member, Ledger, GuestEvent, VerifiedPhone, PhoneChallenge):
        assert not model.objects.exists()


def test_approved_arabic_clause_is_verbatim_and_has_no_enumerated_exceptions():
    assert PROGRAM_CONTENT["ar"]["sections"][13][1] == APPROVED_AR
    assert "الغش" not in APPROVED_AR and "الخطأ الجسيم" not in APPROVED_AR
    assert "الفعل الضار" not in APPROVED_AR
    assert "في الحدود الجائزة نظامًا" in APPROVED_AR
    assert "بقدر الاستحالة" in APPROVED_AR
    assert "تبقى الحجوزات المؤكدة نافذة" in APPROVED_AR


@pytest.mark.parametrize(
    ("version", "reference"),
    [
        (IDENTITY_TERMS_VERSION, IDENTITY_TERMS_REFERENCE),
        (SIGNED_TERMS_VERSION, SIGNED_TERMS_REFERENCE),
        (PROGRAM_TERMS_VERSION, PROGRAM_TERMS_REFERENCE),
    ],
)
def test_only_approved_matching_release_pairs_resolve_to_new_terms(settings, version, reference):
    settings.LOYALTY_TERMS_VERSION, settings.LOYALTY_TERMS_REFERENCE = version, reference
    assert current_terms_version() == PROGRAM_TERMS_VERSION
    assert terms_reference() == PROGRAM_TERMS_REFERENCE
    assert policy_enabled()


def test_disabled_or_custom_terms_are_not_silently_promoted(settings):
    settings.LOYALTY_TERMS_APPROVED = False
    assert current_terms_version() == SIGNED_TERMS_VERSION
    assert terms_reference() == SIGNED_TERMS_REFERENCE
    assert not policy_enabled()
    settings.LOYALTY_TERMS_APPROVED = True
    settings.LOYALTY_TERMS_REFERENCE = "/custom-terms/"
    assert current_terms_version() == SIGNED_TERMS_VERSION
    assert terms_reference() == "/custom-terms/"


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_full_project_copies_match_the_published_content(language, settings):
    issued = document(language, version=PROGRAM_TERMS_VERSION)
    expected = (
        "\n\n".join(
            [issued["title"], issued["issued"], issued["operator"], issued["intro"]]
            + [heading + "\n\n" + paragraph for heading, paragraph in issued["sections"]]
        )
        + "\n"
    )
    path = Path(settings.BASE_DIR) / "docs" / "loyalty-terms" / f"2026-10-18-v5.{language}.txt"
    assert path.read_text(encoding="utf-8").rstrip("\n") == expected.rstrip("\n")


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_existing_members_are_not_reenrolled_or_reconsented_when_viewing_terms(client, language):
    guest = user("existing-program-terms-" + language)
    member = join(guest)
    before = list(Member.objects.values())
    events = list(GuestEvent.objects.values())
    client.force_login(guest)
    assert client.get(f"/{language}/legal/rewards/2026-10-18-v5/").status_code == 200
    assert list(Member.objects.values()) == before
    assert list(GuestEvent.objects.values()) == events
    member.refresh_from_db()
    assert member.terms_version != PROGRAM_TERMS_VERSION
    assert not Ledger.objects.exists()


@pytest.mark.parametrize("language", ["ar", "en", "fr"])
def test_new_terms_consent_cannot_use_a_historical_reference(language):
    with pytest.raises(ValidationError):
        consent_metadata(
            PROGRAM_TERMS_VERSION, f"/{language}/legal/rewards/2026-10-17-v4/", language
        )
    metadata = consent_metadata(
        PROGRAM_TERMS_VERSION, f"/{language}/legal/rewards/2026-10-18-v5/", language
    )
    assert (
        metadata["terms_content_sha256"]
        == document(language, version=PROGRAM_TERMS_VERSION)["sha256"]
    )
