"""Write web/icon-192.png and web/icon-512.png without any imaging library.

Android needs PNG icons for "Add to Home screen"; this draws the same gold disc
and arrow as icon.svg.
"""
import struct
import zlib
from pathlib import Path

BG, GOLD, GOLD2, DARK = (15, 17, 21), (228, 182, 74), (184, 144, 47), (15, 17, 21)


def pixel(x: float, y: float, n: int) -> tuple[int, int, int]:
    cx = cy = n / 2
    r = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    # arrow: triangle head + stem, in a 512-space scaled to n
    s = n / 512
    X, Y = x / s, y / s
    in_head = 150 <= Y <= 260 and abs(X - 256) <= (Y - 150) * (84 / 110)
    in_stem = 260 <= Y <= 360 and abs(X - 256) <= 34
    if r <= n * 130 / 512:
        return DARK if (in_head or in_stem) else GOLD2
    if r <= n * 170 / 512:
        return GOLD
    return BG


def png(n: int) -> bytes:
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x + .5, y + .5, n)) for x in range(n)) for y in range(n))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    web = Path(__file__).resolve().parent.parent / "web"
    for n in (192, 512):
        (web / f"icon-{n}.png").write_bytes(png(n))
        print("wrote", web / f"icon-{n}.png")
