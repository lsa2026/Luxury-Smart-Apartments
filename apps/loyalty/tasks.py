"""Daily fixed mirror, OFF by default. Explicit local rehearsal or approved cloud."""

from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .backup import LocalMirror, _local_source_only


@shared_task(ignore_result=True, soft_time_limit=45, time_limit=60)
def guest_invitations_task():
    from .notifications import dispatch_due

    return dispatch_due()


@shared_task(ignore_result=True, soft_time_limit=240, time_limit=300)
def daily_backup_task():
    if not settings.LOYALTY_BACKUP_ENABLED:
        return {"status": "disabled"}
    if settings.LOYALTY_BACKUP_LOCAL_REHEARSAL:
        if not settings.DEBUG:
            raise ImproperlyConfigured("لا يجوز استخدام النسخة المحلية كتحديث خارجي.")
        _local_source_only()
        path = Path(settings.LOYALTY_BACKUP_LOCAL_PATH)
        if not path.is_absolute() or not path.parent.is_dir():
            raise ImproperlyConfigured("يلزم مسار محلي صريح ومجلد اختبار موجود.")
        result = LocalMirror(path).run(source_id=settings.LOYALTY_BACKUP_SOURCE_ID)
    else:
        from .backup import BackupError
        from .cloud_backup import CloudMirror, mark_failure, mark_success

        if not settings.LOYALTY_BACKUP_DATABASE_URL:
            raise ImproperlyConfigured("يلزم إعداد الاتصال السري بالوجهة المعتمدة.")
        try:
            result = CloudMirror.configured().run()
        except BackupError as exc:
            mark_failure(exc.code)
            raise
        mark_success()
    # Counts and total only. Never return member data through the task broker.
    return {
        "status": "succeeded",
        "counts": result["counts"],
        "program_balance": result["program_balance"],
    }
