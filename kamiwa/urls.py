from django.contrib import admin
from django.urls import include, path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic.base import RedirectView

urlpatterns = [
    # The stock admin form must not bypass the shared CAPTCHA and login limits.
    path('administration/login/', RedirectView.as_view(pattern_name='market:login', permanent=False)),
    path('administration/', admin.site.urls),
    path('captcha/', include('captcha.urls')),
    path('', include(('market.urls', 'market'), namespace='market')),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
