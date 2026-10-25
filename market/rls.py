"""PostgreSQL row-level security context.

Every database connection carries three settings that the RLS policies read:
``kamiwa.mode`` (``system`` or ``user``), ``kamiwa.uid`` and ``kamiwa.perms``.
Web requests run as the signed-in user (or as an anonymous user with no id);
management commands, jobs, migrations and the pre-login flows run as ``system``.
A connection with no mode set sees no protected rows, so a forgotten context
fails closed instead of exposing data.

These policies protect against application bugs such as a missing owner
filter. They do not stop SQL injection, because injected SQL could change the
settings itself; the ORM's parameterised queries remain the defence there.
"""
import threading
from contextlib import contextmanager
from functools import wraps

from django.db import DatabaseError, connection
from django.db.backends.signals import connection_created

SYSTEM = ("system", "", "")
ANONYMOUS = ("user", "", "")

_state = threading.local()


def _wanted():
    return getattr(_state, "context", None) or SYSTEM


def _push(conn, context):
    if conn.vendor != "postgresql":
        return
    mode, uid, perms = context
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('kamiwa.mode', %s, false), set_config('kamiwa.uid', %s, false), "
            "set_config('kamiwa.perms', %s, false)",
            [mode, str(uid), perms],
        )


def _on_connection_created(sender, connection, **kwargs):
    # A reconnect in the middle of a request keeps the request's context.
    _push(connection, _wanted())


connection_created.connect(_on_connection_created, dispatch_uid="kamiwa_rls_context")


def context_for(user):
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return ANONYMOUS
    perms = {name.split(".", 1)[1] for name in user.get_all_permissions() if name.startswith("market.")}
    if user.is_staff:
        perms.add("staff")
    if user.is_superuser:
        perms.add("*")
    return ("user", str(user.pk), ",".join(sorted(perms)))


def set_context(context):
    _state.context = context
    _push(connection, context)


def current_context():
    return _wanted()


@contextmanager
def system_context():
    """Run trusted framework code (sign-in, registration, outbox) without row filters."""
    previous = getattr(_state, "context", None)
    set_context(SYSTEM)
    try:
        yield
    finally:
        _state.context = previous
        _push(connection, _wanted())


def as_system(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with system_context():
            return func(*args, **kwargs)
    return wrapper


def apply_user(user):
    set_context(context_for(user))


class RowSecurityMiddleware:
    """Must sit after AuthenticationMiddleware and AccountSessionMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        previous = getattr(_state, "context", None)
        try:
            apply_user(getattr(request, "user", None))
            return self.get_response(request)
        finally:
            _state.context = previous
            try:
                _push(connection, _wanted())
            except DatabaseError:
                # A broken connection is discarded by Django; a new one gets the context on creation.
                pass
