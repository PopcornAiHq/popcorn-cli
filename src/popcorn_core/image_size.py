"""Pixel dimensions of an image, read from its header with the stdlib.

The apps send `media_metadata: {width, height}` on every image part and lay
the image out from it before it loads; a part without them renders cropped or
at the wrong aspect. Covers what the apps attach — PNG, JPEG, GIF, WebP,
HEIC — and reports the size as displayed, so a phone photo stored sideways
with a rotation tag comes out portrait.
"""

from __future__ import annotations

import struct

_SWAPPING_EXIF_ORIENTATIONS = {5, 6, 7, 8}


def image_size(data: bytes) -> tuple[int, int] | None:
    """`(width, height)`, or None for a format this doesn't read or a bad header."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp(data)
        if data[:2] == b"\xff\xd8":
            return _jpeg(data)
        if data[4:8] == b"ftyp":
            return _heif(data)
    except (struct.error, IndexError):
        pass
    return None


def _webp(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 ":
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None


def _jpeg(data: bytes) -> tuple[int, int] | None:
    i, swap = 2, False
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker == 0xE1 and data[i + 4 : i + 10] == b"Exif\x00\x00":
            swap = _exif_orientation(data[i + 10 : i + 2 + length]) in _SWAPPING_EXIF_ORIENTATIONS
        # SOF0 to SOF15, less DHT (C4), JPG (C8) and DAC (CC), carry the size.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return (h, w) if swap else (w, h)
        i += 2 + length
    return None


def _exif_orientation(tiff: bytes) -> int:
    endian = "<" if tiff[:2] == b"II" else ">"
    ifd = struct.unpack(endian + "I", tiff[4:8])[0]
    for n in range(struct.unpack(endian + "H", tiff[ifd : ifd + 2])[0]):
        entry = ifd + 2 + 12 * n
        tag, _type, _count, value = struct.unpack(endian + "HHIH", tiff[entry : entry + 10])
        if tag == 0x0112:
            return int(value)
    return 1


def _boxes(data: bytes, start: int, end: int) -> dict[bytes, tuple[int, int]]:
    """The first box of each type in `data[start:end]`, as (payload start, end)."""
    found: dict[bytes, tuple[int, int]] = {}
    while start + 8 <= end:
        size, kind = struct.unpack(">I4s", data[start : start + 8])
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", data[start + 8 : start + 16])[0], 16
        elif size == 0:
            size = end - start
        if size < header:
            break
        found.setdefault(kind, (start + header, min(start + size, end)))
        start += size
    return found


def _heif(data: bytes) -> tuple[int, int] | None:
    """The primary item's `ispe`, cropped by its `clap` and swapped if its
    `irot` turns it a quarter. Only properties `ipma` associates with the item
    named by `pitm` count: a thumbnail or tile carries its own."""
    meta = _boxes(data, 0, len(data)).get(b"meta")
    if meta is None:
        return None
    children = _boxes(data, meta[0] + 4, meta[1])  # past the version and flags
    iprp = children.get(b"iprp")
    if b"pitm" not in children or iprp is None:
        return None
    pitm = children[b"pitm"][0]
    primary = int.from_bytes(data[pitm + 4 : pitm + (6 if data[pitm] == 0 else 8)], "big")
    boxes = _boxes(data, *iprp)
    if b"ipco" not in boxes or b"ipma" not in boxes:
        return None

    # ipco's properties, addressed 1-based by ipma.
    properties: list[tuple[bytes, int]] = []
    at, end = boxes[b"ipco"]
    while at + 8 <= end:
        size, kind = struct.unpack(">I4s", data[at : at + 8])
        if size < 8:
            break
        properties.append((kind, at + 8))
        at += size

    at = boxes[b"ipma"][0]
    id_size = 2 if data[at] == 0 else 4  # by version
    index_size = 2 if data[at + 3] & 1 else 1  # by flags
    entries = struct.unpack(">I", data[at + 4 : at + 8])[0]
    at += 8
    mine: dict[bytes, int] = {}
    for _ in range(entries):
        item = int.from_bytes(data[at : at + id_size], "big")
        count = data[at + id_size]
        at += id_size + 1
        for _ in range(count):
            # The top bit marks the property essential; the rest is its index.
            index = int.from_bytes(data[at : at + index_size], "big") & ~(
                0x80 << 8 * (index_size - 1)
            )
            at += index_size
            if item == primary and 0 < index <= len(properties):
                kind, payload = properties[index - 1]
                mine.setdefault(kind, payload)

    if b"ispe" not in mine:
        return None
    w, h = struct.unpack(">II", data[mine[b"ispe"] + 4 : mine[b"ispe"] + 12])
    if b"clap" in mine:
        # The coded size is padded to the encoder's block size; `clap` crops it.
        wn, wd, hn, hd = struct.unpack(">IIII", data[mine[b"clap"] : mine[b"clap"] + 16])
        if wd and hd:
            w, h = wn // wd, hn // hd
    if b"irot" in mine and data[mine[b"irot"]] & 0x3 in (1, 3):
        return h, w
    return w, h
