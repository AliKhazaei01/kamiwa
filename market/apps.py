from django.apps import AppConfig


class MarketConfig(AppConfig):
    name = "market"

    def ready(self):
        from . import rls  # noqa: F401  registers the connection hook
