"""Arabic-only owner workspace, independent of guest language preferences."""

from functools import wraps

from django.utils.translation import override


def arabic_admin(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        # Local to this request: do not change the guest's cookie or profile.
        with override("ar"):
            request.LANGUAGE_CODE = "ar"
            response = view(request, *args, **kwargs)
            response["Content-Language"] = "ar"
            return response

    return wrapped
