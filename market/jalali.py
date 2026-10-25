"""Solar Hijri (Jalali) <-> Gregorian conversion without third-party dependencies."""
from datetime import date

MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
          "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
ASCII_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def to_jalali(value):
    """Return (year, month, day) for a Gregorian date."""
    gy, gm, gd = value.year, value.month, value.day
    offsets = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
            + gd + offsets[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def to_gregorian(jy, jm, jd):
    """Return a Gregorian date, or raise ValueError when the Jalali date does not exist."""
    if not (1 <= jm <= 12 and 1 <= jd <= 31):
        raise ValueError("invalid Jalali date")
    year = jy + 1595
    days = (-355668 + 365 * year + (year // 33) * 8 + ((year % 33) + 3) // 4 + jd
            + ((jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186))
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    for gm, length in enumerate((31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31), start=1):
        if gd <= length:
            break
        gd -= length
    result = date(gy, gm, gd)
    # Day 31 of a 30-day month (or Esfand 30 in a common year) rolls over; reject it.
    if to_jalali(result) != (jy, jm, jd):
        raise ValueError("invalid Jalali date")
    return result


def format_jalali(value):
    if not value:
        return ""
    jy, jm, jd = to_jalali(value)
    return f"{jd} {MONTHS[jm - 1]} {jy}".translate(PERSIAN_DIGITS)
