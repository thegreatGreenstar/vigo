#!/bin/sh
# Make the app icon from Aogo's: DeepSeek blue, "41" on the wing (V4.1), "VI" on
# the hat.  Needs python3 and swift (macOS Command Line Tools), nothing else.
#
#   ./art/make-icon.sh
#
set -eu
cd "$(dirname "$0")/.."

python3 art/recolor.py art/aogo-source.png art/base-clean.png --drop-badge
sips -z 512 512 art/base-clean.png --out art/base-512.png >/dev/null

# The version, where Aogo had 55 for Opus 5.5.
swift art/badge.swift art/base-512.png art/step.png "41" 196 99 0.306 0.439 0.902 26 -2

# The name, on the tag: clear just the letters, never the tag's own shape.
python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from art.recolor import decode, encode
import colorsys

width, height, pixels = decode("art/step.png")


def value(x, y):
    index = (y * width + x) * 4
    if pixels[index + 3] == 0:
        return 1.0
    return colorsys.rgb_to_hsv(
        pixels[index] / 255, pixels[index + 1] / 255, pixels[index + 2] / 255
    )[2]


# Flood the pale outside inwards from a box around the tag: what stays pale is
# enclosed by the tag, so it is the old lettering.
left, top, right, bottom = 306, 206, 360, 258
outside = set()
stack = [(x, y) for x in range(left, right) for y in (top, bottom - 1)]
stack += [(x, y) for y in range(top, bottom) for x in (left, right - 1)]
stack = [spot for spot in stack if value(*spot) > 0.45]
outside.update(stack)
while stack:
    x, y = stack.pop()
    for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
        if left <= nx < right and top <= ny < bottom and (nx, ny) not in outside and value(nx, ny) > 0.45:
            outside.add((nx, ny))
            stack.append((nx, ny))

letters = [
    (x, y)
    for x in range(left, right)
    for y in range(top, bottom)
    if value(x, y) > 0.45 and (x, y) not in outside
]
# Grow by one pixel so the soft edge around each stroke goes too.
mask = set(letters)
for x, y in letters:
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            mask.add((x + dx, y + dy))

tag = (28, 28, 32)
for x, y in mask:
    if not (left <= x < right and top <= y < bottom):
        continue
    index = (y * width + x) * 4
    if value(x, y) > 0.22:                    # the tag stays black
        pixels[index : index + 3] = bytes(tag)

encode("art/tag-clean.png", width, height, pixels)
print(f"cleared {len(letters)} letter pixels")
EOF

swift art/badge.swift art/tag-clean.png art/vigo-icon-41.png "VI" 330 231 1 1 1 15 48

rm -f art/base-clean.png art/base-512.png art/step.png art/tag-clean.png
echo "art/vigo-icon-41.png"
