#!/usr/bin/env python3
"""Write a solid-colour banner, so the app's profile matches its icon.

    python3 art/make-banner.py art/vigo-banner.png 1920 1080 4d6bfe
"""

from __future__ import annotations

import sys

from recolor import encode

DEEPSEEK_BLUE = "4d6bfe"   # the brand blue
PETAL_BLUE = "4e70e6"      # what the icon's petals actually are


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "art/vigo-banner.png"
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 1920
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 1080
    colour = sys.argv[4] if len(sys.argv) > 4 else DEEPSEEK_BLUE
    red, green, blue = (int(colour[index : index + 2], 16) for index in (0, 2, 4))
    pixels = bytearray()
    for _ in range(width * height):
        pixels += bytes((red, green, blue, 255))
    encode(target, width, height, pixels)
    print(f"{target}: {width}x{height} #{colour.upper()}")


if __name__ == "__main__":
    main()
