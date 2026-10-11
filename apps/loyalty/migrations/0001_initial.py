"""Frozen native schema; no imports from mutable application models."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def identity():
    return models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")


def fk(to, **kwargs):
    return models.ForeignKey(to, on_delete=django.db.models.deletion.PROTECT, **kwargs)


def money(**kwargs):
    return models.DecimalField(decimal_places=2, max_digits=16, **kwargs)


class Migration(migrations.Migration):
    initial = True
    dependencies = [("properties", "0022_property_price_calendar"),
        ("reservations", "0021_reservation_loyalty_state"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="Member", fields=[
            ("id", identity()),
            ("number", models.CharField(max_length=32, unique=True)),
            ("status", models.CharField(choices=[("active", "نشطة"), ("withdrawn", "ملغاة")],
                default="active", max_length=16)),
            ("joined_at", models.DateTimeField(default=django.utils.timezone.now)),
            ("consent_at", models.DateTimeField()),
            ("terms_version", models.CharField(max_length=80)),
            ("terms_reference", models.CharField(max_length=500)),
            ("language", models.CharField(max_length=2)),
            ("consent_history", models.JSONField(default=list)),
            ("first_completed_at", models.DateTimeField(blank=True, null=True)),
            ("invitation_token", models.CharField(blank=True, max_length=100, null=True, unique=True)),
            ("invitation_issued_at", models.DateTimeField(blank=True, null=True)),
            ("deferred_corrections", models.JSONField(default=list)),
            ("command_receipts", models.JSONField(default=dict)),
            ("withdrawn_at", models.DateTimeField(blank=True, null=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("customer", models.OneToOneField(to=settings.AUTH_USER_MODEL,
                on_delete=django.db.models.deletion.PROTECT)),
            ("first_completed_reservation", fk("reservations.reservation", blank=True, null=True)),
        ]),
        migrations.CreateModel(name="Ledger", fields=[
            ("id", identity()),
            ("sequence", models.PositiveBigIntegerField(unique=True)),
            ("operation_number", models.CharField(max_length=80, unique=True)),
            ("occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
            ("membership_number", models.CharField(max_length=32)),
            ("tree_number", models.TextField()),
            ("booking_total", money(null=True)),
            ("operation_type", models.CharField(max_length=32, choices=[
                ("loyalty", "نقاط الولاء"), ("addition", "نقاط إضافة"),
                ("transfer", "نقاط تحويل أو استخدام"), ("direct_referral", "دعوة مباشرة"),
                ("indirect_referral", "دعوة غير مباشرة")])),
            ("rate_percent", models.DecimalField(decimal_places=2, max_digits=6, null=True)),
            ("credit", money(default=0)), ("debit", money(default=0)),
            ("program_balance_after", money()), ("member_balance_after", money()),
            ("account_name", models.CharField(max_length=255)),
            ("booking_guest_name", models.CharField(blank=True, max_length=255)),
            ("reservation_reference", models.CharField(blank=True, max_length=64)),
            ("property_name", models.CharField(blank=True, max_length=255)),
            ("hostaway_reservation_id", models.PositiveBigIntegerField(null=True)),
            ("check_in_at", models.DateTimeField(null=True)),
            ("check_out_at", models.DateTimeField(null=True)),
            ("actual_arrival_at", models.DateTimeField(null=True)),
            ("arrival_reference", models.CharField(blank=True, max_length=255)),
            ("booking_currency", models.CharField(blank=True, max_length=3)),
            ("original_booking_amount", models.DecimalField(decimal_places=4, max_digits=14, null=True)),
            ("eligible_lodging_sar", money(null=True)),
            ("verified_cash_sar", money(null=True)), ("refunded_cash_sar", money(null=True)),
            ("points_used", money(null=True)),
            ("settlement_revision", models.PositiveBigIntegerField(default=0)),
            ("beneficiary_distance", models.PositiveIntegerField(null=True)),
            ("older_inviter_count", models.PositiveIntegerField(default=0)),
            ("allocation_numerator", models.PositiveBigIntegerField(null=True)),
            ("allocation_denominator", models.PositiveBigIntegerField(null=True)),
            ("event_reference", models.CharField(max_length=255)),
            ("component", models.CharField(max_length=32)),
            ("event_type", models.CharField(max_length=16)),
            ("operation_group", models.CharField(max_length=80)),
            ("branch", models.PositiveIntegerField()),
            ("request_reference", models.CharField(blank=True, max_length=200)),
            ("rules_version", models.CharField(max_length=80)),
            ("executed_by", models.CharField(default="system", max_length=16)),
            ("reason", models.CharField(max_length=500)),
            ("evidence", models.JSONField(default=dict)),
            ("posted_at", models.DateTimeField(auto_now_add=True)),
            ("booking_customer", fk(settings.AUTH_USER_MODEL, null=True,
                                   related_name="loyalty_booking_movements")),
            ("customer", fk(settings.AUTH_USER_MODEL, related_name="loyalty_movements")),
            ("original", fk("loyalty.ledger", blank=True, null=True)),
            ("property", fk("properties.property", blank=True, null=True)),
            ("reservation", fk("reservations.reservation", blank=True, null=True)),
            ("counterpart", fk("loyalty.member", null=True, related_name="counterpart_movements")),
            ("direct_inviter", fk("loyalty.member", null=True, related_name="direct_reward_movements")),
            ("member", fk("loyalty.member", related_name="movements")),
        ], options={"ordering": ["sequence"]}),
        migrations.CreateModel(name="Referral", fields=[
            ("id", identity()), ("tree_number", models.TextField(unique=True)),
            ("depth", models.PositiveIntegerField()),
            ("branch_order", models.PositiveBigIntegerField()),
            ("fixed_at", models.DateTimeField(default=django.utils.timezone.now)),
            ("evidence", models.JSONField(default=dict)),
            ("inviter", fk("loyalty.member", blank=True, null=True, related_name="invitees")),
            ("member", models.OneToOneField(to="loyalty.member", related_name="referral",
                on_delete=django.db.models.deletion.PROTECT)),
            ("root", fk("loyalty.member", related_name="tree_members")),
        ]),
        migrations.AddConstraint(model_name="member", constraint=models.CheckConstraint(
            condition=models.Q(consent_at__lte=models.F("joined_at")), name="loyalty_consent_before_join")),
        migrations.AddConstraint(model_name="member", constraint=models.CheckConstraint(
            condition=models.Q(language__in=["ar", "en", "fr"]), name="loyalty_member_language")),
        migrations.AddIndex(model_name="ledger", index=models.Index(
            fields=["member", "sequence"], name="loyalty_member_sequence")),
        migrations.AddIndex(model_name="ledger", index=models.Index(
            fields=["reservation", "settlement_revision"], name="loyalty_booking_revision")),
        migrations.AddConstraint(model_name="ledger", constraint=models.CheckConstraint(
            condition=models.Q(credit__gt=0, debit=0) | models.Q(credit=0, debit__gt=0),
            name="loyalty_one_posting_side")),
        migrations.AddConstraint(model_name="ledger", constraint=models.CheckConstraint(
            condition=models.Q(member_balance_after__gte=0, program_balance_after__gte=0),
            name="loyalty_nonnegative_balances")),
        migrations.AddConstraint(model_name="ledger", constraint=models.CheckConstraint(
            condition=models.Q(executed_by="system"), name="loyalty_system_executor")),
        migrations.AddConstraint(model_name="ledger", constraint=models.UniqueConstraint(
            fields=["event_reference", "member", "component"], name="loyalty_unique_business_event")),
        migrations.AddConstraint(model_name="ledger", constraint=models.UniqueConstraint(
            fields=["operation_group", "branch"], name="loyalty_unique_operation_branch")),
        migrations.AddConstraint(model_name="referral", constraint=models.CheckConstraint(
            condition=models.Q(inviter__isnull=True) | ~models.Q(inviter=models.F("member")),
            name="loyalty_no_self_referral")),
        migrations.AddConstraint(model_name="referral", constraint=models.UniqueConstraint(
            condition=models.Q(inviter__isnull=False), fields=["inviter", "branch_order"],
            name="loyalty_unique_child_branch")),
        migrations.AddConstraint(model_name="referral", constraint=models.UniqueConstraint(
            condition=models.Q(inviter__isnull=True), fields=["branch_order"],
            name="loyalty_unique_root_branch")),
    ]
