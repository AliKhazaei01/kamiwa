from django.urls import path, register_converter
from . import views


class DatabaseIDConverter:
    """Reject invalid bigint IDs before a database query can overflow."""
    regex = r"[0-9]{1,19}"

    def to_python(self, value):
        number = int(value)
        if not 0 < number <= 9223372036854775807:
            raise ValueError
        return number

    def to_url(self, value):
        return str(self.to_python(str(value)))


register_converter(DatabaseIDConverter, "dbid")

app_name = "market"
urlpatterns = [
    path("", views.landing, name="landing"),
    path("market/", views.home, name="home"),
    path("accounts/register/", views.register, name="register"),
    path("accounts/register/check-username/", views.username_check, name="username_check"),
    path("accounts/login/", views.login_view, name="login"),
    path("accounts/logout/", views.logout_view, name="logout"),
    path("accounts/verify/", views.verify, name="verify"),
    path("accounts/profile/", views.profile, name="profile"),
    path("accounts/password/", views.password_change, name="password_change"),
    path("accounts/password/reset/", views.password_reset, name="password_reset"),
    path("accounts/password/reset/confirm/", views.password_reset_confirm, name="password_reset_confirm"),
    path("accounts/mfa/setup/", views.mfa_setup, name="mfa_setup"),
    path("accounts/mfa/challenge/", views.mfa_challenge, name="mfa_challenge"),
    path("accounts/admin-email/", views.admin_email_change, name="admin_email_change"),
    path("accounts/outbox/", views.outbox, name="outbox"),
    path("accounts/outbox/<dbid:pk>/", views.outbox, name="outbox_detail"),
    path("private/<str:kind>/<dbid:pk>/", views.private_file, name="private_file"),
    path("panel/", views.dashboard, name="dashboard"),
    path("listings/new/", views.listing_create, name="listing_create"),
    path("listings/<dbid:pk>/", views.listing_detail, name="listing_detail"),
    path("listings/<dbid:pk>/edit/", views.listing_edit, name="listing_edit"),
    path("listings/<dbid:pk>/status/", views.listing_status, name="listing_status"),
    path("listings/<dbid:pk>/price/", views.listing_price, name="listing_price"),
    path("sellers/<str:slug>/", views.seller_page, name="seller_page"),
    path("favorites/", views.favorites, name="favorites"),
    path("favorites/<dbid:pk>/", views.favorite_toggle, name="favorite_toggle"),
    path("compare/", views.compare, name="compare"),
    path("compare/<dbid:pk>/", views.compare_toggle, name="compare_toggle"),
    path("plans/", views.subscriptions, name="subscriptions"),
    path("checkout/<dbid:pk>/", views.checkout, name="checkout"),
    path("inspections/", views.inspections, name="inspections"),
    path("listings/<dbid:pk>/inspect/", views.inspection_create, name="inspection_create"),
    path("inspections/<dbid:pk>/", views.inspection_detail, name="inspection_detail"),
    path("inspections/<dbid:pk>/report/", views.report_create, name="report_create"),
    path("tickets/", views.tickets, name="tickets"),
    path("tickets/new/", views.ticket_create, name="ticket_create"),
    path("tickets/<dbid:pk>/", views.ticket_detail, name="ticket_detail"),
    path("staff/", views.staff, name="staff"),
    path("staff/users/<dbid:pk>/", views.staff_user, name="staff_user"),
    path("staff/admins/", views.staff_admins, name="staff_admins"),
    path("staff/inspections/", views.staff_inspections, name="staff_inspections"),
    path("staff/wallets/", views.staff_wallets, name="staff_wallets"),
    path("wallet/", views.wallet, name="wallet"),
    path("staff/reports/<dbid:pk>/edit/", views.staff_report_edit, name="staff_report_edit"),
]
