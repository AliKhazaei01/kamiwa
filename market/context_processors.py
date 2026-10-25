from django.conf import settings

from .site_images import random_image


def _nav_categories():
    from .models import Category
    return Category.objects.filter(parent=None).order_by("pk")


def common(request):
    selected = request.session.get("compare_ids", [])
    if not isinstance(selected, list):
        selected = []
    selected = list(dict.fromkeys(value for value in selected if type(value) is int and 0 < value <= 9223372036854775807))[:3]
    return {"demo_mode": settings.KAMIWA_TEST_PROVIDERS, "compare_count": len(selected[:3]), "compare_ids": selected[:3],
            "site_image": random_image, "nav_categories": _nav_categories}
