#!/usr/bin/env python3
"""Cutest picture entry v3 for make.fun's "Make the cutest picture" bounty.

A Python 3.13 program, standard library only. It draws a fluffy white
kitten in a strawberry hat hugging a tiny ladybug, against a warm peach
bokeh background, and writes the picture to /output/entry.png
(or $MAKEFUN_OUTPUT_DIR/entry.png when set).

How it works: every shape is a signed-distance field painted into a
512x512 float buffer (discs, ellipses, triangles, line segments), then
upscaled to 1024x1024 with bilinear interpolation (which also softens
the edges) and encoded as a PNG by hand (zlib + struct + crc32).

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
FUR = (255, 255, 255)        # white kitten fur
FUR_SHADE = (232, 226, 220)   # soft grey shading / outline
FUR_PINK = (255, 214, 205)    # inner ear / belly tint
EYE_AMBER = (178, 110, 48)    # iris
EYE_RING = (120, 76, 40)      # iris ring
PUPIL = (32, 20, 16)          # pupil
INK = (80, 55, 50)            # mouth / lashes / whiskers
BLUSH = (250, 150, 150)       # cheek blush
NOSE = (235, 120, 135)        # pink nose
HAT = (232, 72, 74)           # strawberry red
HAT_DARK = (196, 52, 58)      # hat shading
SEED = (255, 232, 150)        # strawberry seeds
LEAF = (96, 172, 96)          # hat leaves
BUG = (226, 60, 52)           # ladybug red
BUG_DARK = (40, 30, 30)       # ladybug head / dots
BG_TOP = (255, 224, 210)
BG_BOTTOM = (255, 244, 230)

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

def heart(buf, cx, cy, s, rgb, alpha=0.6):
    """Small heart from two discs and a triangle."""
    disc(buf, cx - s * 0.45, cy - s * 0.25, s * 0.55, rgb, alpha, 1.5)
    disc(buf, cx + s * 0.45, cy - s * 0.25, s * 0.55, rgb, alpha, 1.5)
    triangle(buf, cx - s, cy, cx + s, cy, cx, cy + s * 1.1,
             rgb, alpha, 1.5)


def eye(buf, cx, cy, r):
    """One huge amber eye: iris, pupil, two sparkle highlights, lashes."""
    disc(buf, cx, cy, r, EYE_AMBER, 1.0, 1.5)
    disc(buf, cx, cy, r * 0.78, (140, 84, 36), 1.0, 1.5)
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
    """Paint the white kitten and ladybug into a buffer, back to front."""
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

    # soft ground shadow under the sitting kitten
    ellipse(buf, 256, 502, 150, 14, (235, 190, 175), 0.30, 3.0)

    # long banded tail curling up on the left, fully visible
    disc(buf, 152, 470, 26, FUR, 1.0, 2.0)
    disc(buf, 126, 456, 25, FUR, 1.0, 2.0)
    disc(buf, 104, 434, 23, FUR, 1.0, 2.0)
    disc(buf, 94, 406, 21, FUR, 1.0, 2.0)
    disc(buf, 98, 378, 17, FUR, 1.0, 2.0)
    disc(buf, 108, 358, 13, FUR, 1.0, 2.0)
    # pale grey bands across the tail so it reads as striped, not a nub
    ellipse(buf, 140, 463, 13, 6, FUR_SHADE, 0.7, 2.0, rot=0.9)
    ellipse(buf, 112, 442, 12, 6, FUR_SHADE, 0.7, 2.0, rot=0.9)
    ellipse(buf, 96, 414, 11, 6, FUR_SHADE, 0.7, 2.0, rot=0.9)

    # kitten body: white sitting shape with a pale pink belly patch
    ellipse(buf, 256, 415, 118, 90, FUR, 1.0, 2.0)
    ring(buf, 256, 415, 118, 2.0, FUR_SHADE, 0.45, 2.0)
    ellipse(buf, 256, 432, 78, 62, FUR_PINK, 0.55, 2.0)

    # hind feet poking out at the bottom, with toe lines
    ellipse(buf, 195, 490, 34, 18, FUR, 1.0, 2.0)
    ellipse(buf, 317, 490, 34, 18, FUR, 1.0, 2.0)
    segment(buf, 188, 478, 188, 492, 2.0, FUR_SHADE, 0.9)
    segment(buf, 202, 478, 202, 492, 2.0, FUR_SHADE, 0.9)
    segment(buf, 310, 478, 310, 492, 2.0, FUR_SHADE, 0.9)
    segment(buf, 324, 478, 324, 492, 2.0, FUR_SHADE, 0.9)

    # ears: wide rounded triangles, white outside, pink inside, peeking
    # out from behind the hat so the pink shows at the outer edge
    triangle(buf, 122, 158, 146, 84, 214, 140, FUR, 1.0, 2.0)
    triangle(buf, 390, 158, 366, 84, 298, 140, FUR, 1.0, 2.0)
    # round off the sharp ear tips so they look soft, not paper-cut
    disc(buf, 146, 84, 8, FUR, 1.0, 2.0)
    disc(buf, 366, 84, 8, FUR, 1.0, 2.0)
    triangle(buf, 140, 142, 152, 98, 194, 132, FUR_PINK, 1.0, 2.0)
    triangle(buf, 372, 142, 360, 98, 318, 132, FUR_PINK, 1.0, 2.0)
    # little cheek fluff tufts at the sides of the face
    triangle(buf, 132, 300, 118, 268, 142, 284, FUR, 1.0, 2.0)
    triangle(buf, 380, 300, 394, 268, 370, 284, FUR, 1.0, 2.0)

    # head: big round white disc with a soft grey rim
    disc(buf, 256, 225, 128, FUR, 1.0, 2.0)
    ring(buf, 256, 225, 128, 2.5, FUR_SHADE, 0.5, 2.0)
    # faint pink muzzle patch so the face has a soft centre
    ellipse(buf, 256, 306, 56, 30, FUR_PINK, 0.35, 2.0)

    # strawberry hat: red dome tilted playfully over the top of the head
    ellipse(buf, 256, 100, 90, 46, HAT, 1.0, 2.0, rot=-0.10)
    ellipse(buf, 256, 142, 92, 11, HAT_DARK, 0.55, 2.0)
    # pale seeds scattered across the dome
    for sx, sy in [(216, 88), (240, 76), (266, 72), (290, 86),
                   (226, 108), (252, 102), (278, 106), (298, 104)]:
        ellipse(buf, sx, sy, 4.5, 3.0, SEED, 0.95, 1.2, rot=0.4)
    # green leaves and a little stem on top of the hat
    ellipse(buf, 240, 54, 9, 17, LEAF, 1.0, 1.5, rot=-0.5)
    ellipse(buf, 256, 46, 9, 18, LEAF, 1.0, 1.5, rot=0.0)
    ellipse(buf, 272, 54, 9, 17, LEAF, 1.0, 1.5, rot=0.5)
    segment(buf, 256, 46, 256, 32, 3.0, (70, 130, 70), 1.0)

    # the huge amber eyes, then wide soft blush (blush only, no drips)
    eye(buf, 200, 235, 33)
    eye(buf, 312, 235, 33)
    ellipse(buf, 162, 300, 25, 13, BLUSH, 0.55, 2.0)
    ellipse(buf, 350, 300, 25, 13, BLUSH, 0.55, 2.0)

    # pink nose, little w mouth, light whiskers
    triangle(buf, 244, 286, 268, 286, 256, 300, NOSE, 1.0, 1.5)
    disc(buf, 252, 290, 3.5, (150, 110, 100), 0.9, 1.0)
    segment(buf, 244, 300, 251, 308, 2.0, INK, 0.9)
    segment(buf, 251, 308, 256, 301, 2.0, INK, 0.9)
    segment(buf, 256, 301, 261, 308, 2.0, INK, 0.9)
    segment(buf, 261, 308, 268, 300, 2.0, INK, 0.9)
    for x1, y1, x2, y2 in [(172, 288, 128, 276), (172, 298, 130, 296),
                          (172, 308, 132, 316), (340, 288, 384, 276),
                          (340, 298, 382, 296), (340, 308, 380, 316)]:
        segment(buf, x1, y1, x2, y2, 1.8, (190, 180, 175), 0.75)

    # tiny ladybug hugged at the chest: red dome, black head, dots
    disc(buf, 256, 395, 30, BUG, 1.0, 2.0)
    segment(buf, 256, 367, 256, 423, 3.0, BUG_DARK, 1.0)
    disc(buf, 256, 364, 11, BUG_DARK, 1.0, 1.5)
    for dx, dy, r in [(-18, -12, 5), (18, -12, 5), (-24, 8, 4.5),
                      (24, 8, 4.5), (-10, 20, 4), (10, 20, 4)]:
        disc(buf, 256 + dx, 395 + dy, r, BUG_DARK, 1.0, 1.2)
    disc(buf, 246, 382, 5, (255, 255, 255), 0.9, 1.0)
    # ladybug face: two white eyes with pupils, tiny smile
    disc(buf, 251, 360, 3.2, (255, 255, 255), 1.0, 1.0)
    disc(buf, 261, 360, 3.2, (255, 255, 255), 1.0, 1.0)
    disc(buf, 251, 361, 1.6, BUG_DARK, 1.0, 1.0)
    disc(buf, 261, 361, 1.6, BUG_DARK, 1.0, 1.0)
    segment(buf, 250, 370, 256, 373, 1.8, (255, 255, 255), 0.95)
    segment(buf, 256, 373, 262, 370, 1.8, (255, 255, 255), 0.95)

    # kitten paws wrapping the ladybug from both sides, toe lines on top
    ellipse(buf, 208, 400, 30, 18, FUR_SHADE, 0.6, 2.0, rot=-0.35)
    ellipse(buf, 304, 400, 30, 18, FUR_SHADE, 0.6, 2.0, rot=0.35)
    ellipse(buf, 208, 400, 28, 16, FUR, 1.0, 2.0, rot=-0.35)
    ellipse(buf, 304, 400, 28, 16, FUR, 1.0, 2.0, rot=0.35)
    segment(buf, 200, 388, 200, 402, 2.0, FUR_SHADE, 0.9)
    segment(buf, 214, 388, 214, 402, 2.0, FUR_SHADE, 0.9)
    segment(buf, 298, 388, 298, 402, 2.0, FUR_SHADE, 0.9)
    segment(buf, 312, 388, 312, 402, 2.0, FUR_SHADE, 0.9)

    # two tiny floating hearts and a daisy in the corner
    heart(buf, 88, 300, 9, (242, 140, 168), 0.6)
    heart(buf, 436, 180, 8, (242, 140, 168), 0.6)
    flower(buf, 440, 470, 10, (255, 255, 255), (255, 201, 60))
    flower(buf, 66, 130, 9, (255, 255, 255), (255, 201, 60))

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
