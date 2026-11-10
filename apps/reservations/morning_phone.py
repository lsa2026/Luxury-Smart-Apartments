"""Compatibility wrapper for the shared booking phone convention."""

from apps.core.phone_numbers import normalize_booking_phone


def normalize_morning_phone(value, country="SA"):
    return normalize_booking_phone(value, default_region=country or "SA")
