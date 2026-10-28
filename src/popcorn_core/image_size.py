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


def _heif(data: bytes) -> tuple[int, int] | None:
    """The largest `ispe` (the primary image, not a thumbnail or grid tile),
    cropped by its `clap` and swapped if an `irot` turns it a quarter. Read from the `meta` box only, so
    the image data can't pass for a box."""
    at = 0
    while at + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[at : at + 8])
        if kind == b"meta":
            break
        if size < 8:
            return None
        at += size
    else:
        return None
    data = data[at : at + size]
    sizes = []
    at = data.find(b"ispe")
    while at != -1:
        sizes.append(struct.unpack(">II", data[at + 8 : at + 16]))
        at = data.find(b"ispe", at + 4)
    if not sizes:
        return None
    w, h = max(sizes, key=lambda s: s[0] * s[1])
    # The coded size is padded to the encoder's block size and a `clap` crops
    # it back; one that trims only a block's worth is the primary image's.
    at = data.find(b"clap")
    while at != -1:
        wn, wd, hn, hd = struct.unpack(">IIII", data[at + 4 : at + 20])
        cw, ch = (wn // wd, hn // hd) if wd and hd else (0, 0)
        if 0 < cw <= w < cw + 64 and 0 < ch <= h < ch + 64:
            w, h = cw, ch
            break
        at = data.find(b"clap", at + 4)
    rot = data.find(b"irot")
    if rot != -1 and data[rot + 4] & 0x3 in (1, 3):
        return h, w
    return w, h
