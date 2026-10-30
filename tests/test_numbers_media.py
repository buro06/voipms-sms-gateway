import io

import pytest
from PIL import Image

from smsgw.media import chunk_text, compress_image
from smsgw.numbers import InvalidNumber, normalize, normalize_lenient, pretty


@pytest.mark.parametrize("raw", ["5145551234", "+1 (514) 555-1234", "1-514-555-1234", "514.555.1234"])
def test_normalize(raw):
    assert normalize(raw) == "5145551234"


@pytest.mark.parametrize("raw", ["", "12345", "0145551234", "5141551234", "+44 20 7946 0958"])
def test_normalize_rejects(raw):
    with pytest.raises(InvalidNumber):
        normalize(raw)


def test_lenient_keeps_short_codes():
    assert normalize_lenient("12345") == "12345"
    assert normalize_lenient("15145551234") == "5145551234"


def test_pretty():
    assert pretty("5145551234") == "+1 (514) 555-1234"
    assert pretty("12345") == "12345"


def test_chunk_text_prefers_spaces():
    text = " ".join(["word"] * 100)
    chunks = chunk_text(text, 160)
    assert all(len(c) <= 160 for c in chunks)
    assert " ".join(chunks) == text


def test_chunk_text_hard_split():
    assert chunk_text("x" * 350, 160) == ["x" * 160, "x" * 160, "x" * 30]


def test_compress_image_under_limit():
    img = Image.effect_noise((3000, 2000), 100).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert len(buf.getvalue()) > 900_000
    out = compress_image(buf.getvalue(), 300_000)
    assert len(out) <= 300_000
    assert Image.open(io.BytesIO(out)).format == "JPEG"
