"""Read-only classic MPQ hash-table overlap inspection; never edits archives.

Format reference: StormLib SBaseCommon.cpp (Storm hashing and table decoding).
https://github.com/ladislav-zezula/StormLib/blob/master/src/SBaseCommon.cpp
No file extraction or executable code from MPQs is performed.
"""

from pathlib import Path
import struct

MASK = 0xFFFFFFFF
CRYPT = [0] * 0x500
seed = 0x100001
for index in range(256):
    for section in range(5):
        seed = (seed * 125 + 3) % 0x2AAAAB
        first = (seed & 0xFFFF) << 16
        seed = (seed * 125 + 3) % 0x2AAAAB
        CRYPT[index + section * 256] = first | (seed & 0xFFFF)


def name_hash(name, kind):
    a, b = 0x7FED7FED, 0xEEEEEEEE
    for char in name.upper().encode("ascii"):
        a = CRYPT[kind * 256 + char] ^ ((a + b) & MASK)
        b = (char + a + b + (b << 5) + 3) & MASK
    return a


def decode_table(raw):
    key, seed = name_hash("(hash table)", 3), 0xEEEEEEEE
    out = bytearray()
    for (encoded,) in struct.iter_unpack("<I", raw):
        seed = (seed + CRYPT[0x400 + (key & 255)]) & MASK
        plain = encoded ^ ((key + seed) & MASK)
        key = (((~key << 21) + 0x11111111) | (key >> 11)) & MASK
        seed = (plain + seed + (seed << 5) + 3) & MASK
        out.extend(struct.pack("<I", plain))
    return out


SPECIAL = {
    (name_hash(n, 1), name_hash(n, 2)) for n in ("(listfile)", "(attributes)", "(signature)")
}
CACHE = {}


def fingerprints(path):
    path = Path(path)
    stat = path.stat()
    cache_key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if cache_key in CACHE:
        return CACHE[cache_key]
    with open(path, "rb") as handle:
        prefix = handle.read(min(stat.st_size, 1024 * 1024))
        start = next(
            (n for n in range(0, len(prefix) - 31, 512) if prefix[n : n + 4] == b"MPQ\x1a"), None
        )
        if start is None:
            raise ValueError("Classic MPQ header not found")
        _, header_size, archive_size, version, sector_shift, offset, block_offset, count, blocks = (
            struct.unpack("<4sIIHHIIII", prefix[start : start + 32])
        )
        if version > 1 or header_size < 32 or not 0 < count <= 1048576:
            raise ValueError("Unsupported MPQ table format")
        if version == 1:
            handle.seek(start + 40)
            high = handle.read(2)
            if len(high) != 2:
                raise ValueError("Truncated extended header")
            offset |= struct.unpack("<H", high)[0] << 32
        if start + offset + count * 16 > stat.st_size:
            raise ValueError("Truncated MPQ hash table")
        handle.seek(start + offset)
        raw = handle.read(count * 16)
    entries = set()
    for a, b, locale, platform, block in struct.iter_unpack("<IIHHI", decode_table(raw)):
        if block not in (0xFFFFFFFF, 0xFFFFFFFE) and block < blocks:
            entries.add((a, b))
    entries -= SPECIAL
    CACHE[cache_key] = entries
    return entries


def overlaps(inventory):
    files, unknown = [], []
    for item in inventory:
        if (
            item["kind"] not in ("managed", "manual")
            or not item["enabled"]
            or item["location"] != "Data"
        ):
            continue
        try:
            files.append((item, fingerprints(item["path"])))
        except (ValueError, OSError) as exc:
            unknown.append(item["name"] + ": " + str(exc))
    hits = []
    for index, (left, a) in enumerate(files):
        for right, b in files[index + 1 :]:
            if left["owner"] and left["owner"] == right["owner"]:
                continue
            shared = len(a & b)
            if shared:
                hits.append(
                    "%s / %s: %d shared indexed assets" % (left["name"], right["name"], shared)
                )
    return hits, unknown
