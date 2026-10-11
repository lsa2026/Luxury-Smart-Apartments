"""Three financial master tables. Booking operating state stays on Reservation."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Member(models.Model):
    customer = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    number = models.CharField(max_length=32, unique=True)
    status = models.CharField(
        max_length=16,
        default="active",
        choices=[
            ("active", "نشطة"),
            ("withdrawn", "ملغاة"),
        ],
    )
    joined_at = models.DateTimeField(default=timezone.now)
    consent_at = models.DateTimeField()
    terms_version = models.CharField(max_length=80)
    terms_reference = models.CharField(max_length=500)
    language = models.CharField(max_length=2)
    consent_history = models.JSONField(default=list)
    first_completed_reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    first_completed_at = models.DateTimeField(null=True, blank=True)
    invitation_token = models.CharField(max_length=100, null=True, unique=True, blank=True)
    invitation_issued_at = models.DateTimeField(null=True, blank=True)
    deferred_corrections = models.JSONField(default=list)
    command_receipts = models.JSONField(default=dict)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(consent_at__lte=F("joined_at")), name="loyalty_consent_before_join"
            ),
            models.CheckConstraint(
                condition=Q(language__in=["ar", "en", "fr"]), name="loyalty_member_language"
            ),
        ]

    def __str__(self):
        return self.number


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("السجل التاريخي لا يعدل؛ أضف حركة تصحيح.")

    def delete(self):
        raise ValidationError("السجل التاريخي لا يحذف.")


class AppendOnly(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("السجل التاريخي لا يعدل.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("السجل التاريخي لا يحذف.")


class Referral(AppendOnly):
    member = models.OneToOneField(Member, on_delete=models.PROTECT, related_name="referral")
    inviter = models.ForeignKey(
        Member, on_delete=models.PROTECT, null=True, blank=True, related_name="invitees"
    )
    root = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="tree_members")
    tree_number = models.TextField(unique=True)
    depth = models.PositiveIntegerField()
    branch_order = models.PositiveBigIntegerField()
    fixed_at = models.DateTimeField(default=timezone.now)
    evidence = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(inviter__isnull=True) | ~Q(inviter=F("member")),
                name="loyalty_no_self_referral",
            ),
            models.UniqueConstraint(
                fields=["inviter", "branch_order"],
                condition=Q(inviter__isnull=False),
                name="loyalty_unique_child_branch",
            ),
            models.UniqueConstraint(
                fields=["branch_order"],
                condition=Q(inviter__isnull=True),
                name="loyalty_unique_root_branch",
            ),
        ]

    def __str__(self):
        return self.tree_number


class Ledger(AppendOnly):
    # Agreed commercial columns, followed by evidence and audit fields.
    sequence = models.PositiveBigIntegerField(unique=True)
    operation_number = models.CharField(max_length=80, unique=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="loyalty_movements"
    )
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="movements")
    membership_number = models.CharField(max_length=32)
    tree_number = models.TextField()
    reservation = models.ForeignKey(
        "reservations.Reservation", on_delete=models.PROTECT, null=True, blank=True
    )
    property = models.ForeignKey(
        "properties.Property", on_delete=models.PROTECT, null=True, blank=True
    )
    booking_total = models.DecimalField(max_digits=16, decimal_places=2, null=True)
    operation_type = models.CharField(
        max_length=32,
        choices=[
            ("loyalty", "نقاط الولاء"),
            ("addition", "نقاط إضافة"),
            ("transfer", "نقاط تحويل أو استخدام"),
            ("direct_referral", "دعوة مباشرة"),
            ("indirect_referral", "دعوة غير مباشرة"),
        ],
    )
    rate_percent = models.DecimalField(max_digits=6, decimal_places=2, null=True)
    credit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    debit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    program_balance_after = models.DecimalField(max_digits=16, decimal_places=2)
    member_balance_after = models.DecimalField(max_digits=16, decimal_places=2)
    account_name = models.CharField(max_length=255)
    booking_customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="loyalty_booking_movements",
    )
    booking_guest_name = models.CharField(max_length=255, blank=True)
    reservation_reference = models.CharField(max_length=64, blank=True)
    property_name = models.CharField(max_length=255, blank=True)
    hostaway_reservation_id = models.PositiveBigIntegerField(null=True)
    check_in_at = models.DateTimeField(null=True)
    check_out_at = models.DateTimeField(null=True)
    actual_arrival_at = models.DateTimeField(null=True)
    arrival_reference = models.CharField(max_length=255, blank=True)
    booking_currency = models.CharField(max_length=3, blank=True)
    original_booking_amount = models.DecimalField(max_digits=14, decimal_places=4, null=True)
    eligible_lodging_sar = models.DecimalField(max_digits=16, decimal_places=2, null=True)
    verified_cash_sar = models.DecimalField(max_digits=16, decimal_places=2, null=True)
    refunded_cash_sar = models.DecimalField(max_digits=16, decimal_places=2, null=True)
    points_used = models.DecimalField(max_digits=16, decimal_places=2, null=True)
    settlement_revision = models.PositiveBigIntegerField(default=0)
    beneficiary_distance = models.PositiveIntegerField(null=True)
    older_inviter_count = models.PositiveIntegerField(default=0)
    allocation_numerator = models.PositiveBigIntegerField(null=True)
    allocation_denominator = models.PositiveBigIntegerField(null=True)
    direct_inviter = models.ForeignKey(
        Member, on_delete=models.PROTECT, null=True, related_name="direct_reward_movements"
    )
    counterpart = models.ForeignKey(
        Member, on_delete=models.PROTECT, null=True, related_name="counterpart_movements"
    )
    original = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True)
    event_reference = models.CharField(max_length=255)
    component = models.CharField(max_length=32)
    event_type = models.CharField(max_length=16)
    operation_group = models.CharField(max_length=80)
    branch = models.PositiveIntegerField()
    request_reference = models.CharField(max_length=200, blank=True)
    rules_version = models.CharField(max_length=80)
    executed_by = models.CharField(max_length=16, default="system")
    reason = models.CharField(max_length=500)
    evidence = models.JSONField(default=dict)
    posted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        indexes = [
            models.Index(fields=["member", "sequence"], name="loyalty_member_sequence"),
            models.Index(
                fields=["reservation", "settlement_revision"], name="loyalty_booking_revision"
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(Q(credit__gt=0, debit=0) | Q(debit__gt=0, credit=0)),
                name="loyalty_one_posting_side",
            ),
            models.CheckConstraint(
                condition=Q(member_balance_after__gte=0) & Q(program_balance_after__gte=0),
                name="loyalty_nonnegative_balances",
            ),
            models.CheckConstraint(
                condition=Q(executed_by="system"), name="loyalty_system_executor"
            ),
            models.UniqueConstraint(
                fields=["event_reference", "member", "component"],
                name="loyalty_unique_business_event",
            ),
            models.UniqueConstraint(
                fields=["operation_group", "branch"], name="loyalty_unique_operation_branch"
            ),
        ]

    def __str__(self):
        return self.operation_number
