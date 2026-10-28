"""Image parts carry `media_metadata: {width, height}`, read from the file's
header, so the apps lay the image out at the right aspect before it loads."""

from __future__ import annotations

import pathlib
import struct
from unittest.mock import MagicMock, patch

import pytest

from popcorn_core import operations
from popcorn_core.image_size import image_size

_IMAGES = pathlib.Path(__file__).parent / "fixtures" / "images"


@pytest.mark.parametrize("name", ["3x2.png", "3x2.jpg", "3x2.heic"])
def test_fixture_sizes(name: str) -> None:
    # The HEIC's coded size is padded to 4x2; its `clap` crops it back to 3x2.
    assert image_size((_IMAGES / name).read_bytes()) == (3, 2)


def _exif_jpeg(orientation: int, width: int = 300, height: int = 200) -> bytes:
    """SOI, an APP1 Exif segment holding one Orientation tag, then SOF0."""
    tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1)
    tiff += struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0) + b"\x00\x00\x00\x00"
    app1 = b"Exif\x00\x00" + tiff
    sof = struct.pack(">BHHB", 8, height, width, 3) + b"\x00" * 9
    return (
        b"\xff\xd8"
        + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
        + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    )  # fmt: skip


@pytest.mark.parametrize(("orientation", "size"), [(1, (300, 200)), (6, (200, 300))])
def test_jpeg_exif_orientation_is_applied(orientation: int, size: tuple[int, int]) -> None:
    assert image_size(_exif_jpeg(orientation)) == size


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full(kind: bytes, payload: bytes) -> bytes:
    return _box(kind, b"\x00\x00\x00\x00" + payload)


def _heif(primary: int, properties: list[bytes], ipma: dict[int, list[int]]) -> bytes:
    """ftyp, then a meta box naming `primary` and mapping item ids to the
    1-based `properties` each is associated with."""
    entries = b"".join(
        struct.pack(">HB", item, len(idx)) + bytes(idx) for item, idx in ipma.items()
    )
    iprp = _box(
        b"iprp",
        _box(b"ipco", b"".join(properties))
        + _full(b"ipma", struct.pack(">I", len(ipma)) + entries),
    )
    meta = _full(b"meta", _full(b"pitm", struct.pack(">H", primary)) + iprp)
    return _box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic") + meta


def _ispe(w: int, h: int) -> bytes:
    return _full(b"ispe", struct.pack(">II", w, h))


def _irot(quarters: int) -> bytes:
    return _box(b"irot", bytes([quarters]))


def test_heif_reads_only_the_primary_items_properties() -> None:
    # Property 1 is another item's quarter turn; the primary (item 1) is
    # associated with a 3x2 ispe and a zero turn.
    data = _heif(1, [_irot(1), _ispe(3, 2), _irot(0), _ispe(320, 240)], {1: [2, 3], 2: [4, 1]})
    assert image_size(data) == (3, 2)


def test_heif_rotation_and_larger_thumbnail_items() -> None:
    assert image_size(_heif(1, [_ispe(4032, 3024), _irot(1)], {1: [1, 0x82]})) == (3024, 4032)
    assert image_size(_heif(2, [_ispe(9000, 9000), _ispe(4032, 3024)], {1: [1], 2: [2]})) == (
        4032,
        3024,
    )


def test_gif_and_webp() -> None:
    assert image_size(b"GIF89a" + struct.pack("<HH", 2400, 1350) + b"\x00" * 4) == (2400, 1350)
    vp8x = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8
    vp8x += (2399).to_bytes(3, "little") + (1349).to_bytes(3, "little")
    assert image_size(vp8x) == (2400, 1350)
    bits = (2400 - 1) | ((1350 - 1) << 14)
    vp8l = b"RIFF\x00\x00\x00\x00WEBPVP8L\x00\x00\x00\x00\x2f" + bits.to_bytes(4, "little")
    assert image_size(vp8l) == (2400, 1350)


@pytest.mark.parametrize("data", [b"", b"%PDF-1.7", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"])
def test_unreadable_is_none(data: bytes) -> None:
    assert image_size(data) is None


def _upload(tmp_path: pathlib.Path, name: str, data: bytes) -> dict:
    path = tmp_path / name
    path.write_bytes(data)
    client = MagicMock()
    client.post.return_value = {
        "file_upload": {"file_key": "k"},
        "upload_url": "https://upload.example.com",
        "upload_fields": {},
    }
    with (
        patch.object(operations, "resolve_conversation", lambda c, ref: ref),
        patch.object(operations.httpx, "post", return_value=MagicMock(status_code=204)),
    ):
        return operations.upload_file(client, "conv", str(path))


def test_upload_sends_image_dimensions(tmp_path: pathlib.Path) -> None:
    part = _upload(tmp_path, "shot.png", (_IMAGES / "3x2.png").read_bytes())
    assert part["media_metadata"] == {"width": 3, "height": 2}


def test_upload_leaves_non_images_and_unreadable_images_bare(tmp_path: pathlib.Path) -> None:
    assert _upload(tmp_path, "notes.txt", b"hello")["media_metadata"] == {}
    assert _upload(tmp_path, "broken.png", b"not a png")["media_metadata"] == {}
