#!/usr/bin/env python3
"""Cutest picture entry v2 for make.fun's "Make the cutest picture" bounty.

A Python 3.13 program, standard library only. It draws a baby red panda
cub in a mint knitted scarf hugging a tiny green frog, against a warm
peach bokeh background, and writes the picture to /output/entry.png
(or $MAKEFUN_OUTPUT_DIR/entry.png when set).

How it works: every shape is a signed-distance field painted into a
512x512 float buffer (discs, ellipses, rounded triangles, line segments),
then upscaled to 1024x1024 with bilinear interpolation (which also
softens the edges) and encoded as a PNG by hand (zlib + struct + crc32,
no Pillow needed).

Determinism: no randomness, no wall clock, no network, no dict-order
dependence. Two runs write byte-identical files.
"""

import binascii
import math
import os
import struct
import zlib

RENDER = 512
OUT_SIZE = 1024
OUT_NAME = "entry.png"

# ---------------------------------------------------------------- palette
PANDA = (216, 112, 56)        # rusty orange fur
PANDA_DARK = (178, 84, 40)    # fur shading / outline
PANDA_CREAM = (255, 240, 219) # face mask / chest
EAR_INNER = (255, 214, 188)   # inner ear
EYE_BROWN = (150, 96, 54)     # iris
EYE_RING = (96, 60, 40)       # iris ring
PUPIL = (30, 18, 14)          # pupil
INK = (74, 48, 38)            # mouth / lashes
BLUSH = (248, 150, 140)       # cheek blush
NOSE = (58, 34, 28)           # nose
FROG = (118, 198, 92)         # frog green
FROG_DARK = (72, 152, 62)     # frog shading
FROG_BELLY = (222, 240, 178)  # frog belly
SCARF = (140, 210, 184)       # mint scarf
SCARF_DARK = (96, 174, 148)   # scarf shading
BG_TOP = (255, 231, 214)
BG_BOTTOM = (255, 248, 236)

# ---------------------------------------------------------------- helpers
def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def edge(d, aa):
    """Soft coverage 1.0 inside a shape (d <= -aa) fading to 0 outside."""
    t = clamp((aa - d) / (2.0 * aa))
    return t * t * (3.0 - 2.0 * t)


def blend(buf, idx, rgb, alpha):
    """Blend rgb (0..255) onto float buffer at flat index idx (3 floats)."""
    if alpha <= 0.0:
        return
    if alpha >= 1.0:
        buf[idx] = rgb[0]
        buf[idx + 1] = rgb[1]
        buf[idx + 2] = rgb[2]
        return
    inv = 1.0 - alpha
    buf[idx] = buf[idx] * inv + rgb[0] * alpha
    buf[idx + 1] = buf[idx + 1] * inv + rgb[1] * alpha
    buf[idx + 2] = buf[idx + 2] * inv + rgb[2] * alpha

def disc(buf, cx, cy, r, rgb, alpha=1.0, aa=1.5):
    """Filled circle with a soft edge."""
    x0 = max(0, int(cx - r - aa - 1))
    x1 = min(RENDER - 1, int(cx + r + aa + 1))
    y0 = max(0, int(cy - r - aa - 1))
    y1 = min(RENDER - 1, int(cy + r + aa + 1))
    for y in range(y0, y1 + 1):
        dy = y - cy
        row = y * RENDER * 3
        for x in range(x0, x1 + 1):
            dx = x - cx
            d = math.sqrt(dx * dx + dy * dy) - r
            a = edge(d, aa)
            if a > 0.0:
                blend(buf, row + x * 3, rgb, a * alpha)


def ring(buf, cx, cy, r, width, rgb, alpha=1.0, aa=1.5):
    """Thin circle outline."""
    x0 = max(0, int(cx - r - width - aa - 1))
    x1 = min(RENDER - 1, int(cx + r + width + aa + 1))
    y0 = max(0, int(cy - r - width - aa - 1))
    y1 = min(RENDER - 1, int(cy + r + width + aa + 1))
    for y in range(y0, y1 + 1):
        dy = y - cy
        row = y * RENDER * 3
        for x in range(x0, x1 + 1):
            dx = x - cx
            d = abs(math.sqrt(dx * dx + dy * dy) - r) - width / 2.0
            a = edge(d, aa)
            if a > 0.0:
                blend(buf, row + x * 3, rgb, a * alpha)

def ellipse(buf, cx, cy, rx, ry, rgb, alpha=1.0, aa=1.5, rot=0.0):
    """Filled ellipse, optionally rotated (rot in radians)."""
    co = math.cos(rot)
    si = math.sin(rot)
    x0 = max(0, int(cx - rx - ry - aa - 1))
    x1 = min(RENDER - 1, int(cx + rx + ry + aa + 1))
    y0 = max(0, int(cy - rx - ry - aa - 1))
    y1 = min(RENDER - 1, int(cy + rx + ry + aa + 1))
    for y in range(y0, y1 + 1):
        row = y * RENDER * 3
        for x in range(x0, x1 + 1):
            dx = x - cx
            dy = y - cy
            px = dx * co + dy * si
            py = -dx * si + dy * co
            d = math.sqrt((px / rx) ** 2 + (py / ry) ** 2) - 1.0
            a = edge(d * min(rx, ry), aa)
            if a > 0.0:
                blend(buf, row + x * 3, rgb, a * alpha)

def triangle(buf, ax, ay, bx, by, cx, cy, rgb, alpha=1.0, aa=1.5):
    """Filled triangle with soft edges (signed distance to the triangle)."""
    x0 = max(0, int(min(ax, bx, cx) - aa - 1))
    x1 = min(RENDER - 1, int(max(ax, bx, cx) + aa + 1))
    y0 = max(0, int(min(ay, by, cy) - aa - 1))
    y1 = min(RENDER - 1, int(max(ay, by, cy) + aa + 1))
    ex, ey = bx - ax, by - ay
    fx, fy = cx - bx, cy - by
    gx, gy = ax - cx, ay - cy
    for y in range(y0, y1 + 1):
        row = y * RENDER * 3
        for x in range(x0, x1 + 1):
            px, py = x - ax, y - ay
            qx, qy = x - bx, y - by
            rx, ry = x - cx, y - cy
            de = min(
                (px * ex + py * ey) / (ex * ex + ey * ey),
                (qx * fx + qy * fy) / (fx * fx + fy * fy),
                (rx * gx + ry * gy) / (gx * gx + gy * gy),
            )
            de = clamp(de)
            d2 = min(
                (px - ex * de) ** 2 + (py - ey * de) ** 2,
                (qx - fx * de) ** 2 + (qy - fy * de) ** 2,
                (rx - gx * de) ** 2 + (ry - gy * de) ** 2,
            )
            c1 = ex * py - ey * px
            c2 = fx * qy - fy * qx
            c3 = gx * ry - gy * rx
            inside = (c1 >= 0 and c2 >= 0 and c3 >= 0) or (
                c1 <= 0 and c2 <= 0 and c3 <= 0
            )
            d = -math.sqrt(d2) if inside else math.sqrt(d2)
            a = edge(d, aa)
            if a > 0.0:
                blend(buf, row + x * 3, rgb, a * alpha)

def segment(buf, x1, y1, x2, y2, width, rgb, alpha=1.0, aa=1.2):
    """Soft round-capped line segment."""
    x0 = max(0, int(min(x1, x2) - width - aa - 1))
    x1b = min(RENDER - 1, int(max(x1, x2) + width + aa + 1))
    y0 = max(0, int(min(y1, y2) - width - aa - 1))
    y1b = min(RENDER - 1, int(max(y1, y2) + width + aa + 1))
    dx, dy = x2 - x1, y2 - y1
    ll = dx * dx + dy * dy or 1.0
    for y in range(y0, y1b + 1):
        row = y * RENDER * 3
        for x in range(x0, x1b + 1):
            t = clamp(((x - x1) * dx + (y - y1) * dy) / ll)
            qx, qy = x - (x1 + t * dx), y - (y1 + t * dy)
            d = math.sqrt(qx * qx + qy * qy) - width / 2.0
            a = edge(d, aa)
            if a > 0.0:
                blend(buf, row + x * 3, rgb, a * alpha)


def sparkle(buf, cx, cy, r, rgb, alpha=0.8):
    """Four-point star: a thin diamond plus a bright core dot."""
    x0 = max(0, int(cx - r - 2))
    x1 = min(RENDER - 1, int(cx + r + 2))
    y0 = max(0, int(cy - r - 2))
    y1 = min(RENDER - 1, int(cy + r + 2))
    for y in range(y0, y1 + 1):
        row = y * RENDER * 3
        for x in range(x0, x1 + 1):
            dx, dy = abs(x - cx), abs(y - cy)
            d = dx + dy - r
            a = edge(d, 1.5) * 0.55
            d2 = math.sqrt(dx * dx + dy * dy) - r * 0.28
            a = max(a, edge(d2, 1.2))
            if a > 0.0:
                blend(buf, row + x * 3, rgb, a * alpha)

def flower(buf, cx, cy, r, petal, center):
    """Tiny five-petal daisy: rotated petal ellipses, yellow centre."""
    for k in range(5):
        ang = -math.pi / 2 + k * (2 * math.pi / 5)
        px = cx + math.cos(ang) * r * 0.52
        py = cy + math.sin(ang) * r * 0.52
        ellipse(buf, px, py, r * 0.30, r * 0.52, petal, 1.0, 1.5,
                rot=ang + math.pi / 2)
    disc(buf, cx, cy, r * 0.30, center, 1.0, 1.2)
    disc(buf, cx - r * 0.08, cy - r * 0.08, r * 0.10,
         (255, 232, 150), 1.0, 1.0)


def eye(buf, cx, cy, r):
    """One huge brown eye: iris, pupil, two sparkle highlights, lashes."""
    disc(buf, cx, cy, r, EYE_BROWN, 1.0, 1.5)
    disc(buf, cx, cy, r * 0.78, (110, 66, 38), 1.0, 1.5)
    disc(buf, cx, cy, r * 0.52, PUPIL, 1.0, 1.5)
    ring(buf, cx, cy, r, 2.0, EYE_RING, 0.85, 1.5)
    disc(buf, cx - r * 0.34, cy - r * 0.36, r * 0.26,
         (255, 255, 255), 1.0, 1.2)
    disc(buf, cx + r * 0.30, cy + r * 0.34, r * 0.12,
         (255, 255, 255), 0.95, 1.0)
    for ang in (-0.5, -0.15, 0.2):
        sx = cx + math.cos(ang - math.pi / 2) * r * 0.98
        sy = cy + math.sin(ang - math.pi / 2) * r * 0.98
        ex = cx + math.cos(ang - math.pi / 2) * r * 1.22
        ey = cy + math.sin(ang - math.pi / 2) * r * 1.22
        segment(buf, sx, sy, ex, ey, 2.2, INK, 0.85)

# ---------------------------------------------------------------- picture
def draw():
    """Paint the red panda cub and frog into a float buffer, back to front."""
    buf = [0.0] * (RENDER * RENDER * 3)

    # background: warm vertical gradient, peach top to cream bottom
    for y in range(RENDER):
        t = y / (RENDER - 1)
        r = BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t
        g = BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t
        b = BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t
        row = y * RENDER * 3
        for x in range(RENDER):
            buf[row + x * 3] = r
            buf[row + x * 3 + 1] = g
            buf[row + x * 3 + 2] = b

    # soft bokeh discs drifting in the background (fixed places, low alpha)
    bokeh = [
        (70, 90, 34, (255, 205, 170), 0.28),
        (440, 70, 26, (255, 220, 150), 0.30),
        (470, 250, 40, (255, 190, 190), 0.24),
        (40, 300, 30, (255, 225, 160), 0.26),
        (480, 420, 30, (255, 200, 175), 0.22),
        (90, 450, 22, (255, 235, 190), 0.28),
        (380, 480, 36, (255, 210, 180), 0.22),
        (150, 40, 18, (255, 255, 255), 0.28),
    ]
    for cx, cy, r, rgb, a in bokeh:
        disc(buf, cx, cy, r, rgb, a, aa=8.0)

    # sparkles
    for cx, cy, r in [(120, 130, 8), (420, 160, 7), (60, 210, 6),
                      (460, 330, 8), (110, 380, 6), (400, 90, 6)]:
        sparkle(buf, cx, cy, r, (255, 250, 220), 0.9)

    # small bushy banded tail nub peeking out at the bottom left
    ellipse(buf, 45, 495, 62, 34, PANDA, 1.0, 2.0, rot=0.55)
    ellipse(buf, 20, 480, 32, 8, PANDA_CREAM, 1.0, 2.0, rot=2.12)
    ellipse(buf, 55, 500, 32, 8, PANDA_CREAM, 1.0, 2.0, rot=2.12)

    # cub body: orange shape with a cream chest the frog sits against
    ellipse(buf, 256, 600, 190, 160, PANDA, 1.0, 2.0)
    ellipse(buf, 256, 580, 125, 115, PANDA_CREAM, 1.0, 2.0)

    # ears: wide rounded triangles, orange outside, pale inside
    triangle(buf, 108, 212, 142, 96, 228, 182, PANDA, 1.0, 2.0)
    triangle(buf, 404, 212, 370, 96, 284, 182, PANDA, 1.0, 2.0)
    triangle(buf, 130, 192, 150, 122, 202, 176, EAR_INNER, 1.0, 2.0)
    triangle(buf, 382, 192, 362, 122, 310, 176, EAR_INNER, 1.0, 2.0)
    # little cream tuft on top of the head between the ears
    triangle(buf, 244, 128, 268, 128, 256, 152, PANDA_CREAM, 1.0, 1.5)

    # head: big round orange disc with a soft darker rim
    disc(buf, 256, 258, 148, PANDA, 1.0, 2.0)
    ring(buf, 256, 258, 148, 2.5, PANDA_DARK, 0.55, 2.0)

    # cream face mask across the eyes and muzzle, cheek tufts at the sides
    ellipse(buf, 256, 288, 108, 86, PANDA_CREAM, 1.0, 2.0)
    triangle(buf, 118, 300, 104, 268, 128, 288, PANDA, 1.0, 2.0)
    triangle(buf, 394, 300, 408, 268, 384, 288, PANDA, 1.0, 2.0)

    # dark tear marks under the eyes, then the huge brown eyes
    ellipse(buf, 198, 308, 9, 14, PANDA_DARK, 0.30, 2.0)
    ellipse(buf, 314, 308, 9, 14, PANDA_DARK, 0.30, 2.0)
    eye(buf, 198, 268, 35)
    eye(buf, 314, 268, 35)

    # blush, nose, little mouth
    ellipse(buf, 158, 330, 26, 14, BLUSH, 0.5, 2.0)
    ellipse(buf, 354, 330, 26, 14, BLUSH, 0.5, 2.0)
    triangle(buf, 244, 316, 268, 316, 256, 332, NOSE, 1.0, 1.5)
    disc(buf, 252, 320, 3.5, (150, 110, 100), 0.9, 1.0)
    segment(buf, 244, 330, 251, 338, 2.0, INK, 0.9)
    segment(buf, 251, 338, 256, 331, 2.0, INK, 0.9)
    segment(buf, 256, 331, 261, 338, 2.0, INK, 0.9)
    segment(buf, 261, 338, 268, 330, 2.0, INK, 0.9)

    # mint knitted scarf wrapped around the neck, knot at the left
    ellipse(buf, 256, 420, 122, 30, SCARF, 1.0, 2.0)
    ellipse(buf, 256, 411, 120, 18, SCARF_DARK, 0.35, 2.0)
    segment(buf, 150, 410, 150, 430, 3.0, SCARF_DARK, 0.6)
    segment(buf, 362, 410, 362, 430, 3.0, SCARF_DARK, 0.6)
    disc(buf, 185, 420, 17, SCARF_DARK, 1.0, 2.0)
    disc(buf, 185, 420, 13, SCARF, 1.0, 2.0)
    segment(buf, 178, 414, 192, 426, 3.0, SCARF_DARK, 0.6)
    segment(buf, 192, 414, 178, 426, 3.0, SCARF_DARK, 0.6)

    # tiny frog hugged at the lower centre: body, belly, stalk eyes, smile
    disc(buf, 306, 448, 44, FROG, 1.0, 2.0)
    ellipse(buf, 306, 458, 30, 26, FROG_BELLY, 1.0, 2.0)
    disc(buf, 284, 398, 15, FROG, 1.0, 1.5)
    disc(buf, 328, 398, 15, FROG, 1.0, 1.5)
    disc(buf, 284, 398, 7.5, PUPIL, 1.0, 1.2)
    disc(buf, 328, 398, 7.5, PUPIL, 1.0, 1.2)
    disc(buf, 281, 395, 2.6, (255, 255, 255), 0.95, 1.0)
    disc(buf, 325, 395, 2.6, (255, 255, 255), 0.95, 1.0)
    segment(buf, 288, 448, 306, 456, 2.5, INK, 0.9)
    segment(buf, 306, 456, 324, 448, 2.5, INK, 0.9)
    ellipse(buf, 280, 446, 8, 5, BLUSH, 0.5, 1.5)
    ellipse(buf, 332, 446, 8, 5, BLUSH, 0.5, 1.5)
    # a tiny white daisy tucked between the frog's eyes
    flower(buf, 306, 380, 9, (255, 255, 255), (255, 201, 60))

    # cub paws hugging the frog from both sides
    ellipse(buf, 224, 452, 38, 23, PANDA_DARK, 0.6, 2.0)
    ellipse(buf, 388, 448, 38, 23, PANDA_DARK, 0.6, 2.0)
    ellipse(buf, 224, 452, 36, 21, PANDA, 1.0, 2.0)
    ellipse(buf, 388, 448, 36, 21, PANDA, 1.0, 2.0)
    segment(buf, 216, 438, 216, 452, 2.0, (150, 80, 45), 0.9)
    segment(buf, 230, 438, 230, 452, 2.0, (150, 80, 45), 0.9)
    segment(buf, 380, 434, 380, 448, 2.0, (150, 80, 45), 0.9)
    segment(buf, 394, 434, 394, 448, 2.0, (150, 80, 45), 0.9)

    # two tiny floating hearts
    for cx, cy, s in [(88, 430, 9), (438, 300, 8)]:
        disc(buf, cx - s * 0.45, cy - s * 0.25, s * 0.55,
             (242, 140, 168), 0.55, 1.5)
        disc(buf, cx + s * 0.45, cy - s * 0.25, s * 0.55,
             (242, 140, 168), 0.55, 1.5)
        triangle(buf, cx - s, cy, cx + s, cy, cx, cy + s * 1.1,
                 (242, 140, 168), 0.55, 1.5)

    # a tiny daisy filling the bottom-right corner
    flower(buf, 432, 472, 10, (255, 255, 255), (255, 201, 60))

    return buf

# ---------------------------------------------------------------- upscale
def upscale(buf):
    """Bilinear 512 -> 1024 upscale; returns flat RGB bytes."""
    out = bytearray(OUT_SIZE * OUT_SIZE * 3)
    scale = (RENDER - 1) / (OUT_SIZE - 1)
    for oy in range(OUT_SIZE):
        sy = oy * scale
        y0 = int(sy)
        y1 = min(y0 + 1, RENDER - 1)
        fy = sy - y0
        orow = oy * OUT_SIZE * 3
        r0 = y0 * RENDER * 3
        r1 = y1 * RENDER * 3
        for ox in range(OUT_SIZE):
            sx = ox * scale
            x0 = int(sx)
            x1 = min(x0 + 1, RENDER - 1)
            fx = sx - x0
            o = orow + ox * 3
            for c in range(3):
                top = buf[r0 + x0 * 3 + c] * (1 - fx) + buf[r0 + x1 * 3 + c] * fx
                bot = buf[r1 + x0 * 3 + c] * (1 - fx) + buf[r1 + x1 * 3 + c] * fx
                out[o + c] = int(top * (1 - fy) + bot * fy + 0.5)
    return bytes(out)


# ---------------------------------------------------------------- png
def png_bytes(rgb, width, height):
    """Encode flat RGB bytes as a PNG (filter 0, zlib level 9)."""
    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- main
def main():
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)
    data = png_bytes(upscale(draw()), OUT_SIZE, OUT_SIZE)
    path = os.path.join(out_dir, OUT_NAME)
    with open(path, "wb") as f:
        f.write(data)
    print("wrote %s (%d bytes)" % (path, len(data)))


if __name__ == "__main__":
    main()
