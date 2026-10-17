"""Owner-only tree using fictional members; no real awards or provider calls."""

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import VerifiedPhone, profile_for
from apps.loyalty.models import Ledger, Member, Referral, ReferralRevision
from apps.loyalty.referrals import graph, link_members
from apps.loyalty.tree import DIAGRAM_NODES, diagram, presentation
from tests.test_loyalty_admin_referrals import programme as programme  # noqa: F401
from tests.test_loyalty_stage8 import join, user

pytestmark = pytest.mark.django_db


@pytest.fixture
def family():
    owner = user("tree-owner")
    owner.is_staff = owner.is_superuser = True
    owner.save()
    members = [join(user(f"tree-{i}")) for i in range(5)]
    for m in members:
        m.first_completed_at = timezone.now() - timedelta(days=1)
        m.save(update_fields=["first_completed_at"])
    a, b, c, d, _ = members
    for child, parent in [(b, a), (c, b), (d, a)]:
        link_members(
            owner,
            child.pk,
            parent.pk,
            reason="علاقات افتراضية لاختبار الرسم",
            operation=uuid.uuid4(),
        )
    client = Client()
    client.force_login(owner)
    return client, owner, members


def endpoint():
    return reverse("loyalty:tree")


def test_tree_roots_focus_and_nested_fragments_follow_current_links(family):
    client, _, (a, b, c, d, e) = family
    response = client.get(endpoint())
    assert response.status_code == 200
    assert response.context["tree_total"] == 5
    assert response.context["tree_roots"] == 2
    assert response.context["tree_linked"] == 3
    assert {r.member_id for r in response.context["tree_children"]} == {a.pk, e.pk}
    response = client.get(endpoint(), {"parent": a.pk})
    assert {r.member_id for r in response.context["tree_children"]} == {b.pk, d.pk}
    assert response.context["focus"].child_count == 2
    for parent, child in [(a, b), (b, c)]:
        response = client.get(endpoint(), {"parent": parent.pk, "fragment": "children"})
        content = response.content.decode()
        assert response["X-Loyalty-Tree-Fragment"] == "children"
        assert child.number in content
        assert "<!DOCTYPE" not in content
        assert "no-store" in response["Cache-Control"]
        assert response["Content-Language"] == "ar"
        assert "@example.invalid" not in content
    response = client.get(endpoint(), {"parent": c.pk})
    assert [r.member_id for r in response.context["tree_ancestors"]] == [a.pk, b.pk]
    assert response.context["tree_children"] == []
    assert not Ledger.objects.exists()  # Zero-balance members still appear.


@pytest.mark.parametrize("format", ["number", "path", "name", "phone", "arabic_phone"])
def test_search_supports_primary_verified_mobile_and_member_identifiers(family, format):
    client, _, (_, b, _, _, _) = family
    b.customer.first_name = "خالد"
    b.customer.last_name = "ضيف تجريبي"
    b.customer.save()
    VerifiedPhone.objects.create(user=b.customer, phone="+966551234567")
    value = {
        "number": b.number.lower(),
        "path": graph()[b.pk].tree_number,
        "name": "خالد",
        "phone": "055 123 4567",
        "arabic_phone": "٠٥٥١٢٣٤٥٦٧",
    }[format]
    response = client.get(endpoint(), {"q": value})
    assert response.status_code == 200
    assert response.context["focus"].member_id == b.pk


def test_unverified_contact_phone_is_not_a_search_identity(family):
    client, _, (a, *_) = family
    profile = profile_for(a.customer)
    profile.phone = "+966551234567"
    profile.save()
    response = client.get(endpoint(), {"q": "0551234567"})
    assert response.status_code == 200
    assert response.context["focus"] is None
    assert "لا يوجد عضو مطابق" in response.content.decode()


def test_ambiguous_name_requires_selection_and_inactive_members_remain_visible(family):
    client, _, (a, b, *_) = family
    for m in [a, b]:
        m.customer.first_name = "أحمد"
        m.customer.save()
    b.status = "withdrawn"
    b.save()
    response = client.get(endpoint(), {"q": "أحمد"})
    assert response.context["focus"] is None
    assert {r.member_id for r in response.context["tree_results"]} == {a.pk, b.pk}
    response = client.get(endpoint(), {"parent": b.pk})
    assert "عضوية غير نشطة" in response.content.decode()


def test_bounded_pagination_for_roots_and_direct_invitees(family):
    client, owner, (a, *_) = family
    for i in range(22):
        m = join(user(f"extra-{i}"))
        link_members(
            owner, m.pk, a.pk, reason="علاقات افتراضية لاختبار الصفحات", operation=uuid.uuid4()
        )
    first = client.get(endpoint(), {"parent": a.pk})
    cursor = first.context["tree_cursor"]
    assert len(first.context["tree_children"]) == 20
    assert cursor is not None
    more = client.get(endpoint(), {"parent": a.pk, "after": cursor, "fragment": "children"})
    assert len(more.context["tree_children"]) == 4
    assert more.context["tree_cursor"] is None
    assert not {r.pk for r in first.context["tree_children"]} & {
        r.pk for r in more.context["tree_children"]
    }
    for i in range(22):
        join(user(f"root-{i}"))
    first = client.get(endpoint())
    more = client.get(endpoint(), {"after": first.context["tree_cursor"], "fragment": "children"})
    assert len(first.context["tree_children"]) == 20
    assert len(more.context["tree_children"]) == 4


@pytest.mark.parametrize("suffix", ["", "?fragment=children", "?parent=1&fragment=children"])
def test_every_tree_response_is_owner_only(suffix, family):
    _, _, (a, *_) = family
    client = Client()
    assert client.get(endpoint() + suffix).status_code == 302
    client.force_login(a.customer)
    assert client.get(endpoint() + suffix).status_code == 302
    a.customer.is_staff = True
    a.customer.save()
    assert client.get(endpoint() + suffix).status_code == 403


def test_read_only_no_provider_calls_no_identity_leak_and_html_escaping(family):
    client, _, (a, *_) = family
    a.customer.first_name = '<script>alert("x")</script>'
    a.customer.save()
    before = [
        list(model.objects.values()) for model in [Member, Referral, ReferralRevision, Ledger]
    ]
    with (
        patch("httpx.Client.send", side_effect=AssertionError("No provider calls")),
        CaptureQueriesContext(connection) as captured,
    ):
        response = client.get(endpoint(), {"parent": a.pk})
    assert response.status_code == 200
    assert not any(
        q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in captured
    )
    assert before == [
        list(model.objects.values()) for model in [Member, Referral, ReferralRevision, Ledger]
    ]
    assert "<script>alert" not in response.content.decode()
    assert "&lt;script&gt;" in response.content.decode()
    assert len([q for q in captured if 'FROM "loyalty_referral"' in q["sql"]]) == 1
    assert client.post(endpoint()).status_code == 405


def test_empty_programme_and_graph_limit_fail_gracefully(family):
    client, *_ = family
    with patch("apps.loyalty.referrals.graph", return_value={}):
        response = client.get(endpoint())
        assert response.status_code == 200
        assert "لا توجد عضويات لعرضها" in response.content.decode()
    with patch("apps.loyalty.referrals.MAX_TREE", 1):
        response = client.get(endpoint())
        assert response.status_code == 200
        assert response.context["tree_error"]
        assert client.get(endpoint(), {"fragment": "children"}).status_code == 400


def test_breadcrumbs_do_not_loop_on_invalid_cycles(family):
    _, _, (a, b, *_) = family
    links = graph()
    links[a.pk].inviter_id = b.pk
    assert len(presentation(links, links[b.pk], 0)["tree_ancestors"]) <= 2


def test_admin_entry_and_locale_are_arabic_without_changing_guest_cookie(family, settings):
    client, *_ = family
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
    response = client.get(endpoint(), HTTP_ACCEPT_LANGUAGE="fr")
    assert response["Content-Language"] == "ar"
    assert 'dir="rtl"' in response.content.decode()
    assert "شجرة الأعضاء" in response.content.decode()
    assert settings.LANGUAGE_COOKIE_NAME not in response.cookies
    assert "فتح شجرة الأعضاء" in client.get(reverse("loyalty:admin")).content.decode()


@pytest.mark.parametrize("value", ["invalid", "²", "9" * 90, "-1"])
def test_invalid_member_reference_is_a_normal_not_found(family, value):
    client, *_ = family
    assert client.get(endpoint(), {"parent": value}).status_code == 404


def flatten_diagram(nodes):
    return [node for root in nodes for node in [root, *flatten_diagram(root["children"])]]


def test_people_diagram_preloads_real_descendants_and_safe_profile_details(family):
    client, _, (a, b, c, d, e) = family
    response = client.get(endpoint())
    nodes = flatten_diagram(response.context["tree_diagram_children"])
    assert {node["link"].member_id for node in nodes} == {a.pk, b.pk, c.pk, d.pk, e.pk}
    html = response.content.decode()
    assert html.count("data-tree-person href=") == 5
    assert html.count("<template data-tree-profile>") == 5
    assert 'id="tree-member-profile"' in html
    assert 'role="dialog"' in html
    assert "مرّر الماوس على الشخص" in html
    assert "عرض معلومات" in html
    assert "تاريخ الانضمام" in html
    assert graph()[c.pk].tree_number in html
    assert "loyalty-person-tree.js" in html
    assert response.context["tree_diagram_children"][0]["loaded"]
    for row in response.context["tree_children"]:
        assert row.inviter_name == "بدون داعٍ"
    focused = client.get(endpoint(), {"parent": b.pk})
    assert focused.context["focus"].inviter_name == a.customer.get_full_name()


def test_people_diagram_depth_and_node_limits_do_not_discard_unloaded_branches():
    links = {
        i: SimpleNamespace(member_id=i, inviter_id=i - 1 if i > 1 else None, branch_order=1)
        for i in range(1, 100)
    }
    nodes = flatten_diagram(diagram(links, [links[1]]))
    assert len(nodes) == 5
    assert nodes[-1]["link"].member_id == 5
    assert nodes[-1]["loaded"] is False  # Its button still loads generation six.
    links = {
        i: SimpleNamespace(member_id=i, inviter_id=(i // 5 or None), branch_order=i)
        for i in range(1, 1000)
    }
    roots = [row for row in links.values() if row.inviter_id is None]
    nodes = flatten_diagram(diagram(links, roots))
    assert len(nodes) == DIAGRAM_NODES
    assert len({node["link"].member_id for node in nodes}) == len(nodes)
    assert any(node.get("cursor") for node in nodes)


def test_people_diagram_never_invents_a_binary_split_or_repeats_cycles():
    links = {
        i: SimpleNamespace(member_id=i, inviter_id=1 if i > 1 else None, branch_order=i)
        for i in range(1, 9)
    }
    result = diagram(links, [links[1]])
    assert len(result[0]["children"]) == 7
    links[1].inviter_id = 2
    assert len(flatten_diagram(diagram(links, [links[1]]))) == 8
