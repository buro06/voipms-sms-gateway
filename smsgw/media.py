import io

from PIL import Image, ImageOps

_SIDES = (2048, 1600, 1280, 1024, 800, 640, 480)
_QUALITIES = (85, 75, 65, 50)


def compress_image(blob: bytes, max_bytes: int) -> bytes:
    """Re-encode an image as JPEG no larger than max_bytes (best effort), shrinking as needed."""
    with Image.open(io.BytesIO(blob)) as src:
        img = ImageOps.exif_transpose(src)
        if img.mode != "RGB":
            img = img.convert("RGB")
    out = b""
    for side in _SIDES:
        candidate = img.copy()
        candidate.thumbnail((side, side))
        for quality in _QUALITIES:
            buf = io.BytesIO()
            candidate.save(buf, "JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            if len(out) <= max_bytes:
                return out
    return out


def chunk_text(text: str, size: int) -> list[str]:
    """Split text into pieces of at most `size` chars, preferring whitespace boundaries."""
    chunks: list[str] = []
    rest = text.strip()
    while len(rest) > size:
        cut = rest.rfind(" ", 0, size + 1)
        if cut <= size // 2:
            cut = rest.rfind("\n", 0, size + 1)
        if cut <= size // 2:
            cut = size
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks
