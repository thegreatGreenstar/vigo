#!/usr/bin/env python3
"""Shift Aogo's warm petals to DeepSeek blue, and keep everything else.

    python3 art/recolor.py art/aogo-source.png art/vigo-icon.png [--hue 227]

Standard library only.  The orange is turned to the target hue while its
saturation and lightness stay where they are, so every shade of the petals
survives; skin, cheeks, glasses and the background are untouched.
"""

from __future__ import annotations

import argparse
import colorsys
import struct
import zlib

CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(path: str) -> tuple[int, int, bytearray]:
    raw = open(path, "rb").read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path}: not a PNG")
    offset = 8
    width = height = depth = colour = 0
    palette = b""
    data = b""
    while offset < len(raw):
        length, kind = struct.unpack("!I4s", raw[offset : offset + 8])
        body = raw[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack("!IIBBBBB", body)
            if depth != 8 or interlace:
                raise SystemExit("only 8-bit, non-interlaced PNGs")
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            data += body
        elif kind == b"IEND":
            break
    channels = CHANNELS[colour]
    stride = width * channels
    stream = zlib.decompress(data)
    pixels = bytearray(width * height * 4)
    previous = bytearray(stride)
    position = 0
    for row in range(height):
        filter_kind = stream[position]
        position += 1
        line = bytearray(stream[position : position + stride])
        position += stride
        for index in range(stride):
            left = line[index - channels] if index >= channels else 0
            up = previous[index]
            corner = previous[index - channels] if index >= channels else 0
            if filter_kind == 1:
                line[index] = (line[index] + left) & 0xFF
            elif filter_kind == 2:
                line[index] = (line[index] + up) & 0xFF
            elif filter_kind == 3:
                line[index] = (line[index] + (left + up) // 2) & 0xFF
            elif filter_kind == 4:
                line[index] = (line[index] + _paeth(left, up, corner)) & 0xFF
        for column in range(width):
            start = column * channels
            target = (row * width + column) * 4
            if colour == 6:
                pixels[target : target + 4] = line[start : start + 4]
            elif colour == 2:
                pixels[target : target + 3] = line[start : start + 3]
                pixels[target + 3] = 255
            elif colour == 0:
                value = line[start]
                pixels[target : target + 3] = bytes([value, value, value])
                pixels[target + 3] = 255
            elif colour == 3:
                index = line[start]
                pixels[target : target + 3] = palette[index * 3 : index * 3 + 3]
                pixels[target + 3] = 255
            else:
                raise SystemExit(f"colour type {colour} is not handled")
        previous = line
    return width, height, pixels


def encode(path: str, width: int, height: int, pixels: bytearray) -> None:
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw += pixels[row * stride : (row + 1) * stride]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack("!I", len(body))
            + kind
            + body
            + struct.pack("!I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0)))
        handle.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        handle.write(chunk(b"IEND", b""))


def recolor(pixels: bytearray, hue: float, saturation: float = 0.50,
            cool: float = 0.18) -> int:
    """Turn warm, saturated pixels to `hue`; give the greys a cool cast."""
    changed = 0
    for index in range(0, len(pixels), 4):
        red, green, blue, alpha = pixels[index : index + 4]
        if alpha == 0:
            continue
        h, s, v = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        if s >= saturation and (h * 360) <= 60:  # the orange petals and the mark
            new = colorsys.hsv_to_rgb(hue / 360, s, v)
            pixels[index : index + 3] = bytes(round(part * 255) for part in new)
            changed += 1
        elif s < 0.10:  # the leaves: a cold grey, keeping their lightness
            shade = v
            tint = (shade * (1 - cool * 0.35), shade * (1 - cool * 0.15), shade)
            if v < 0.98:
                pixels[index : index + 3] = bytes(min(255, round(part * 255)) for part in tint)
    return changed


def drop_badge(pixels: bytearray, width: int, height: int) -> int:
    """Erase the tiny '55' on the pale leaf before the colour moves.

    Only small warm marks inside the leaf's corner count as a badge; the
    petal's own edge is one long component and is left alone.  The digits are
    replaced with the flat colour of the leaf around them.
    """
    def warm(x: int, y: int) -> bool:
        index = (y * width + x) * 4
        if pixels[index + 3] == 0:
            return False
        h, s, _ = colorsys.rgb_to_hsv(
            pixels[index] / 255, pixels[index + 1] / 255, pixels[index + 2] / 255
        )
        return s > 0.5 and h * 360 < 50

    seen: set[tuple[int, int]] = set()
    marks: list[tuple[int, int, int, int]] = []
    for y in range(0, min(height, 96)):
        for x in range(width // 4, min(width, width * 3 // 5)):
            if (x, y) in seen or not warm(x, y):
                continue
            stack = [(x, y)]
            seen.add((x, y))
            points: list[tuple[int, int]] = []
            while stack:
                cx, cy = stack.pop()
                points.append((cx, cy))
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1),
                               (cx + 1, cy + 1), (cx - 1, cy + 1),
                               (cx + 1, cy - 1), (cx - 1, cy - 1)):
                    if 0 <= nx < width and 0 <= ny < 96 and (nx, ny) not in seen and warm(nx, ny):
                        seen.add((nx, ny))
                        stack.append((nx, ny))
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            if not 8 <= len(points) <= 60 or max(xs) - min(xs) > 20:
                continue  # the petal's edge, or a speck
            marks.append((min(xs), min(ys), max(xs), max(ys)))

    if not marks:
        return 0
    # One fill over the whole badge, so no stray pixel of it survives.
    left = min(mark[0] for mark in marks) - 3
    top = min(mark[1] for mark in marks) - 3
    right = max(mark[2] for mark in marks) + 3
    bottom = max(mark[3] for mark in marks) + 3
    ring = [
        (nx, ny)
        for ny in range(max(0, top - 5), min(height, bottom + 6))
        for nx in range(max(0, left - 5), min(width, right + 6))
        if not (left <= nx <= right and top <= ny <= bottom)
        and pixels[(ny * width + nx) * 4 + 3] > 0
        and not warm(nx, ny)
    ]
    if not ring:
        return 0
    fill = [
        sum(pixels[(ny * width + nx) * 4 + channel] for nx, ny in ring) // len(ring)
        for channel in range(3)
    ]
    for ny in range(max(0, top), min(height, bottom + 1)):
        for nx in range(max(0, left), min(width, right + 1)):
            if pixels[(ny * width + nx) * 4 + 3] > 0:
                pixels[(ny * width + nx) * 4 : (ny * width + nx) * 4 + 3] = bytes(fill)
    return len(marks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--hue", type=float, default=227.0,
                        help="DeepSeek blue is about 227 degrees")
    parser.add_argument("--saturation", type=float, default=0.50)
    parser.add_argument("--cool", type=float, default=0.18)
    parser.add_argument("--drop-badge", action="store_true",
                        help="erase the small '55' mark the original carried")
    args = parser.parse_args()
    width, height, pixels = decode(args.source)
    removed = drop_badge(pixels, width, height) if args.drop_badge else 0
    changed = recolor(pixels, args.hue, args.saturation, args.cool)
    encode(args.target, width, height, pixels)
    print(
        f"{args.target}: {width}x{height}, {changed} pixels moved to hue {args.hue:.0f}"
        + (f", {removed} badge marks cleared" if args.drop_badge else "")
    )


if __name__ == "__main__":
    main()
