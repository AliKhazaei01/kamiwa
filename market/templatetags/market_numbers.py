from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()
PERSIAN_DIGITS = str.maketrans('0123456789,', '۰۱۲۳۴۵۶۷۸۹٬')


@register.filter
def intcomma(value):
    """Readable Persian whole-number prices, independent of locale grouping defaults."""
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return '—'
        return format(number, ',.0f').translate(PERSIAN_DIGITS)
    except (InvalidOperation, TypeError, ValueError):
        return value


@register.filter
def fa_digits(value):
    return str(value).translate(PERSIAN_DIGITS)


@register.filter
def ago(value):
    """Single-unit relative time ("۳ روز") for compact card metadata."""
    from django.utils.timesince import timesince
    try:
        return timesince(value, depth=1).translate(PERSIAN_DIGITS)
    except (TypeError, ValueError):
        return ''


def _jalali_parts(value):
    from datetime import datetime
    from django.utils import timezone
    from ..jalali import to_jalali
    if isinstance(value, datetime) and timezone.is_aware(value):
        value = timezone.localtime(value)
    return value, to_jalali(value)


@register.filter
def jdate(value):
    """Solar Hijri date in Persian digits, e.g. ۱۴۰۵/۰۷/۲۸."""
    if not value:
        return ''
    try:
        _, (jy, jm, jd) = _jalali_parts(value)
    except (AttributeError, TypeError, ValueError):
        return ''
    return f'{jy}/{jm:02d}/{jd:02d}'.translate(PERSIAN_DIGITS)


@register.filter
def jdatetime(value):
    """Solar Hijri date and local time in Persian digits, e.g. ۱۴۰۵/۰۷/۲۸ ۱۳:۴۵."""
    if not value:
        return ''
    try:
        local, (jy, jm, jd) = _jalali_parts(value)
        clock = f' {local.hour:02d}:{local.minute:02d}' if hasattr(local, 'hour') else ''
    except (AttributeError, TypeError, ValueError):
        return ''
    return f'{jy}/{jm:02d}/{jd:02d}{clock}'.translate(PERSIAN_DIGITS)


# Former 3D emoji names -> line icons in includes/icon.html.
GLYPHS = {
    'money-bag': 'cash', 'money-wings': 'alert', 'hourglass': 'clock', 'chat': 'message', 'balance': 'compare',
    'sparkles': 'star', 'glowing-star': 'star', 'memo': 'edit', 'bust': 'user', 'trophy': 'award', 'office': 'building',
    'bar-chart': 'chart', 'ticket': 'card', 'party': 'check', 'thumbs-up': 'check', 'eyes': 'eye', 'envelope': 'mail',
    'lorry': 'tractor', 'package': 'trailer', 'pickup': 'tow', 'rocket': 'bolt', 'gear': 'settings', 'construction': 'wrench',
}


@register.filter
def glyph(name):
    return GLYPHS.get(name, name)


@register.filter
def glyph_size(size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        return 'md'
    return 'sm' if size <= 30 else 'md' if size <= 48 else 'lg' if size <= 80 else 'xl'
