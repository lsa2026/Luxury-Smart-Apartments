"""Read-only loopback preview using synthetic, in-memory booking data only."""

from preview_booking_summary import (
    ROOT,
    Client,
    HTTPServer,
    PreviewHandler,
    call_command,
    get_user_model,
    override_settings,
    patch,
    reverse,
    timedelta,
    timezone,
    unquote,
    urlsplit,
)

from apps.notifications.models import GuestInvoice
from tests.test_booking_modifications_phase6 import confirmed_reservation, create_extension
from tests.test_operations_booking_summary import payment

OUTPUT = ROOT / "outputs/booking-detail-20261016"


def render_preview():
    call_command("migrate", verbosity=0)
    owner = get_user_model().objects.create_superuser(username="معاينة", password=None)
    booking = confirmed_reservation()
    booking.property.name_ar = "شقة الملقا العائلية — ثلاث غرف نوم"
    booking.property.save()
    booking.booking_intent.guest_first_name = "ضيف"
    booking.booking_intent.guest_last_name = "المعاينة"
    booking.booking_intent.guest_email = "preview@example.invalid"
    booking.booking_intent.guest_phone = "+966500000000"
    booking.booking_intent.save()
    client = Client()
    client.force_login(owner)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (
        override_settings(
            HOSTAWAY_WEBSITE_RESERVATION_SYNC_ENABLED=False,
            RESERVATION_PAYMENT_LINKS_ENABLED=True,
            MANUAL_PAYMENT_RECEIPTS_ENABLED=True,
        ),
        patch("httpx.Client.send", side_effect=AssertionError("Offline preview")),
    ):
        url = reverse("notifications:booking_detail", args=[booking.pk])
        response = client.get(url)
        assert response.status_code == 200
        (OUTPUT / "pending.html").write_bytes(response.content)
        booking.payment_status = "paid"
        booking.save(update_fields=["payment_status"])
        payment(booking, amount=booking.total_price)
        response = client.get(url)
        assert response.status_code == 200
        (OUTPUT / "paid.html").write_bytes(response.content)
        GuestInvoice.objects.create(
            hostaway_reservation_id=booking.hostaway_reservation_id,
            created_by=owner,
            issued_at=timezone.now(),
            number="PREVIEW-ONLY",
            snapshot={
                "guest_name": "ضيف المعاينة",
                "guest_email": "preview@example.invalid",
                "guest_phone": "+966500000000",
                "platform_reference": "PREVIEW-ONLY",
                "currency": "SAR",
                "total": str(booking.total_price),
            },
        )
        response = client.get(url, {"section": "invoice"})
        assert response.status_code == 200
        (OUTPUT / "invoice.html").write_bytes(response.content)
        create_extension(booking)
        response = client.get(url)
        assert response.status_code == 200
        (OUTPUT / "edit.html").write_bytes(response.content)
        booking.check_out = timezone.localdate() - timedelta(days=1)
        booking.check_in = booking.check_out - timedelta(days=booking.nights)
        booking.save()
        response = client.get(url)
        assert response.status_code == 200
        (OUTPUT / "ended.html").write_bytes(response.content)


class DetailHandler(PreviewHandler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if (
            "\\" in path
            or ".." in path.split("/")
            or not path.startswith(("/static/", "/outputs/booking-detail-20261016/"))
        ):
            self.send_error(404)
            return
        super(PreviewHandler, self).do_GET()


if __name__ == "__main__":
    render_preview()
    print("Synthetic read-only preview ready on http://127.0.0.1:8770", flush=True)
    HTTPServer(("127.0.0.1", 8770), DetailHandler).serve_forever()
