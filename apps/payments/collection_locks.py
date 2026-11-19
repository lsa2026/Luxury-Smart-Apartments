"""One booking lock, acquired before row locks in channel and manual collection."""

from django.db import connection


def lock_channel_booking(hostaway_id):
    if hostaway_id is None or connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ["channel-booking-collection:" + str(hostaway_id)],
        )
