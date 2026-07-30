import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory

from apps.reviews.admin import ReviewAdmin
from apps.reviews.models import Review

pytestmark = pytest.mark.django_db


def test_review_admin_permissions_and_source_fields_are_read_only() -> None:
    user_model = get_user_model()
    staff = user_model.objects.create_user(
        username="review-editor",
        password="test-password",
        is_staff=True,
    )
    staff.user_permissions.add(Permission.objects.get(codename="change_review"))
    superuser = user_model.objects.create_superuser(
        username="root-admin",
        password="test-password",
        email="admin@example.com",
    )
    review_admin = ReviewAdmin(Review, AdminSite())
    request = RequestFactory().get("/admin/reviews/review/")

    request.user = staff
    assert review_admin.has_add_permission(request) is False
    assert review_admin.has_delete_permission(request) is False
    assert "public_review" in review_admin.get_readonly_fields(request)
    assert "rating" in review_admin.get_readonly_fields(request)
    assert "is_visible" not in review_admin.get_readonly_fields(request)
    assert "is_featured" not in review_admin.get_readonly_fields(request)

    request.user = superuser
    assert review_admin.has_delete_permission(request) is True
