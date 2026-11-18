"""Image validation, EXIF metadata stripping, and storage path utilities.

Security & Privacy guarantees:
- Enforces 5 MB maximum upload size (matches Stage 2 'hazard-photos' bucket).
- Verifies actual decoded image format via Pillow (JPEG, PNG, WebP only) —
  never trusts filenames or Content-Type headers alone.
- Strips ALL EXIF, GPS, XMP, and ICC metadata by copying raw pixel buffers into
  a clean image prior to storage.
- Builds storage paths strictly matching Stage 4 storage policy:
    <auth.uid()>/<report_id_uuid>/<filename>
"""

from dataclasses import dataclass
import io
from typing import Optional
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB (5,242,880 bytes)
ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_FORMAT_METADATA = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}
# Protect against decompression bombs
Image.MAX_IMAGE_PIXELS = 25_000_000


class ImageValidationError(Exception):
    """Base image validation error with HTTP status mapping."""

    def __init__(self, user_message: str, status_code: int = 400) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code


class ImageTooLargeError(ImageValidationError):
    """Raised when an uploaded image exceeds 5 MB."""

    def __init__(self, user_message: str = "Image exceeds the 5 MB maximum upload limit.") -> None:
        super().__init__(user_message=user_message, status_code=413)


class UnsupportedImageTypeError(ImageValidationError):
    """Raised when an uploaded image is not JPEG, PNG, or WebP."""

    def __init__(
        self,
        user_message: str = "Unsupported image format. Allowed formats: JPEG, PNG, and WebP.",
    ) -> None:
        super().__init__(user_message=user_message, status_code=415)


@dataclass(frozen=True)
class ProcessedImage:
    """Validated, EXIF-stripped image ready for private Supabase Storage upload."""

    content: bytes
    content_type: str
    extension: str
    width: int
    height: int
    size_bytes: int


def validate_and_strip_exif(
    raw_bytes: bytes,
    declared_content_type: Optional[str] = None,
) -> ProcessedImage:
    """Validate size/type/integrity of an image and return EXIF-stripped bytes."""
    if not raw_bytes:
        raise ImageValidationError("An evidence photograph is required.", status_code=400)

    if len(raw_bytes) > MAX_IMAGE_SIZE_BYTES:
        raise ImageTooLargeError()

    if declared_content_type:
        normalized_mime = declared_content_type.split(";", 1)[0].strip().lower()
        if normalized_mime and normalized_mime not in ALLOWED_MIME_TYPES:
            raise UnsupportedImageTypeError()

    try:
        with Image.open(io.BytesIO(raw_bytes)) as src_img:
            detected_format = (src_img.format or "").upper()
            if detected_format not in _FORMAT_METADATA:
                raise UnsupportedImageTypeError()

            # Preserve visual orientation before discarding EXIF tags
            oriented = ImageOps.exif_transpose(src_img)
            target_mode = "RGB" if detected_format == "JPEG" else (
                "RGBA" if oriented.mode in ("RGBA", "LA", "P") else "RGB"
            )
            converted = oriented.convert(target_mode)

            # Create brand-new image from raw pixel data to guarantee zero EXIF/GPS/XMP metadata
            clean_img = Image.new(converted.mode, converted.size)
            clean_img.paste(converted)

            output = io.BytesIO()
            if detected_format == "JPEG":
                clean_img.save(output, format="JPEG", quality=88, optimize=True)
            elif detected_format == "PNG":
                clean_img.save(output, format="PNG", optimize=True)
            else:
                clean_img.save(output, format="WEBP", quality=88)

            cleaned_bytes = output.getvalue()
            width, height = clean_img.size
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageValidationError(
            "Uploaded file is not a valid readable image.",
            status_code=400,
        ) from exc

    if len(cleaned_bytes) > MAX_IMAGE_SIZE_BYTES:
        raise ImageTooLargeError()

    mime_type, ext = _FORMAT_METADATA[detected_format]
    return ProcessedImage(
        content=cleaned_bytes,
        content_type=mime_type,
        extension=ext,
        width=width,
        height=height,
        size_bytes=len(cleaned_bytes),
    )


def build_hazard_storage_path(user_id: str, report_id: str, extension: str) -> str:
    """Build a Storage path matching Stage 4 policy: <auth.uid()>/<report_id>/<filename>."""
    # Validate UUID format strictly to prevent path traversal
    uid_str = str(uuid.UUID(str(user_id)))
    rid_str = str(uuid.UUID(str(report_id)))
    ext = extension.strip().lower().lstrip(".")
    if ext not in ("jpg", "png", "webp"):
        raise UnsupportedImageTypeError()
    filename = f"{uuid.uuid4()}.{ext}"
    return f"{uid_str}/{rid_str}/{filename}"
