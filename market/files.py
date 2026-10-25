"""Validated storage and authorization for files. Never expose private storage URLs."""
import io
import os
import uuid
import warnings
from pathlib import Path
from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db.models.fields.files import FieldFile
from django.utils.deconstruct import deconstructible
from PIL import Image, ImageOps

MAX_FILE_SIZE = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 24_000_000


@deconstructible
class PrivateStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(location=settings.PRIVATE_MEDIA_ROOT, file_permissions_mode=0o600, directory_permissions_mode=0o700)

    @property
    def base_location(self):
        return settings.PRIVATE_MEDIA_ROOT

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def _save(self, name, content):
        validate_private_file(content)
        content.seek(0)
        is_pdf = content.read(8).startswith(b'%PDF-')
        content.seek(0)
        if not is_pdf:
            content = sanitize_image(content)
            name = str(Path(name).with_suffix('.jpg'))
        return super()._save(name, content)

    def url(self, name):
        raise ValueError('Private files require authorized download.')

    def get_valid_name(self, name):
        return f'{uuid.uuid4().hex}{Path(name).suffix.lower()}'


def _validate_size(upload):
    if not upload or upload.size == 0 or upload.size > MAX_FILE_SIZE:
        raise ValidationError('حجم فایل باید بیشتر از صفر و حداکثر ۸ مگابایت باشد.')


def preserve_file_lifetime(function):
    @wraps(function)
    def wrapped(upload, *args, **kwargs):
        opened_here = isinstance(upload, FieldFile) and upload.closed
        try:
            return function(upload, *args, **kwargs)
        finally:
            if opened_here:
                upload.close()
    return wrapped


@preserve_file_lifetime
def validate_public_image(upload):
    _validate_size(upload)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            upload.seek(0)
            with Image.open(upload) as image:
                if image.format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise ValidationError('تصویر باید JPEG، PNG یا WebP باشد.')
                image.verify()
    except ValidationError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError('فایل تصویر معتبر نیست یا ابعاد بسیار بزرگی دارد.')
    finally:
        upload.seek(0)
    return upload


@preserve_file_lifetime
def validate_private_file(upload):
    _validate_size(upload)
    upload.seek(0)
    header = upload.read(8)
    upload.seek(0)
    if header.startswith(b'%PDF-') and Path(upload.name).suffix.lower() == '.pdf':
        return upload
    validate_public_image(upload)
    return upload


@preserve_file_lifetime
def sanitize_image(upload):
    validate_public_image(upload)
    with Image.open(upload) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((1920, 1440), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=86, optimize=True)
    return ContentFile(output.getvalue(), name=f'{uuid.uuid4().hex}.jpg')


def authorized_private_file(actor, kind, pk):
    from .models import Account, SellerPage, InspectionReport
    if not actor.is_authenticated:
        raise PermissionDenied
    try:
        actor = Account.objects.get(pk=actor.pk)
    except Account.DoesNotExist:
        raise PermissionDenied
    if not actor.is_active or actor.is_restricted:
        raise PermissionDenied
    can_review_identity = actor.is_staff and actor.has_perm('market.review_identity')
    can_review_reports = actor.is_staff and actor.has_perm('market.finalize_report')
    if kind in {'identity', 'birth_certificate', 'identity_pending', 'birth_certificate_pending'}:
        obj = Account.objects.get(pk=pk)
        allowed = actor.pk == obj.pk or can_review_identity
        name = 'birth_certificate' if kind.startswith('birth_certificate') else 'identity_document'
        field = getattr(obj, name)
        if kind.endswith('_pending'):
            model_field = obj._meta.get_field(name)
            field = model_field.attr_class(obj, model_field, (obj.pending_profile or {}).get(name, ''))
    elif kind in {'license', 'license_pending'}:
        obj = SellerPage.objects.select_related('user').get(pk=pk)
        allowed = actor.pk == obj.user_id or can_review_identity
        field = obj.license_document
        if kind == 'license_pending':
            model_field = obj._meta.get_field('license_document')
            field = model_field.attr_class(obj, model_field, obj.pending_data.get('license_document', ''))
    elif kind == 'report':
        obj = InspectionReport.objects.select_related('inspection__listing', 'expert').get(pk=pk)
        inspection = obj.inspection
        allowed = can_review_reports or (actor.role == 'expert' and actor.pk == obj.expert.user_id and obj.expert.active)
        if actor.is_entitled and obj.status == 'final':
            allowed = allowed or actor.pk in {inspection.requester_id, inspection.listing.owner_id}
        field = obj.private_document
    else:
        raise PermissionDenied
    if not allowed or not field:
        raise PermissionDenied
    return field


MAX_VIDEO_SIZE = 60 * 1024 * 1024
MAX_VIDEO_SECONDS = 61  # one minute, with a second of encoder slack
MAX_VIDEO_EDGE = (1920, 1080)
VIDEO_SUFFIXES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime'}
_CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts'}


def _boxes(stream, start, end):
    """Yield (type, payload_start, payload_end) for ISO-BMFF boxes between two offsets."""
    position = start
    while position + 8 <= end:
        stream.seek(position)
        header = stream.read(8)
        if len(header) < 8:
            return
        size, kind = int.from_bytes(header[:4], 'big'), header[4:]
        offset = 8
        if size == 1:
            size, offset = int.from_bytes(stream.read(8), 'big'), 16
        elif size == 0:
            size = end - position
        if size < offset or position + size > end:
            raise ValidationError('ساختار فایل ویدیو معتبر نیست.')
        yield kind, position + offset, position + size
        position += size


def inspect_video(upload):
    """Read duration and frame size from MP4/MOV headers without decoding the video."""
    upload.seek(0, os.SEEK_END)
    end = upload.tell()
    duration = width = height = None
    stack = [(0, end)]
    seen_ftyp = False
    while stack:
        start, stop = stack.pop()
        for kind, payload, box_end in _boxes(upload, start, stop):
            if kind == b'ftyp':
                seen_ftyp = True
            elif kind in _CONTAINERS:
                stack.append((payload, box_end))
            elif kind == b'mvhd':
                upload.seek(payload)
                version = upload.read(1)[0]
                upload.seek(payload + (20 if version == 1 else 12))
                timescale = int.from_bytes(upload.read(4), 'big')
                length = int.from_bytes(upload.read(8 if version == 1 else 4), 'big')
                duration = length / timescale if timescale else None
            elif kind == b'tkhd' and box_end - payload >= 84:
                upload.seek(box_end - 8)
                w, h = int.from_bytes(upload.read(4), 'big') >> 16, int.from_bytes(upload.read(4), 'big') >> 16
                if w and h:
                    width, height = max(width or 0, w), max(height or 0, h)
    upload.seek(0)
    if not seen_ftyp or duration is None or not width:
        raise ValidationError('فایل ویدیو باید MP4 یا MOV سالم با تصویر باشد.')
    return {'seconds': duration, 'width': width, 'height': height}


def validate_video(upload):
    suffix = Path(upload.name).suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        raise ValidationError('فقط ویدیوی MP4 یا MOV پذیرفته می‌شود.')
    if not upload.size or upload.size > MAX_VIDEO_SIZE:
        raise ValidationError('حجم هر ویدیو باید حداکثر ۶۰ مگابایت باشد.')
    info = inspect_video(upload)
    if info['seconds'] > MAX_VIDEO_SECONDS:
        raise ValidationError('مدت هر ویدیو حداکثر یک دقیقه است.')
    long_edge, short_edge = max(info['width'], info['height']), min(info['width'], info['height'])
    if long_edge > MAX_VIDEO_EDGE[0] or short_edge > MAX_VIDEO_EDGE[1]:
        raise ValidationError('کیفیت ویدیو حداکثر ۱۰۸۰p (۱۹۲۰×۱۰۸۰) است.')
    return info
