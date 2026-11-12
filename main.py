#!/usr/bin/env python3
"""Cutest picture entry for make.fun's "Make the cutest picture" bounty.

A Python 3.13 program, standard library only. It draws a fluffy cream
kitten with a flower crown and huge amber eyes, hugging a tiny plush
bunny, against a warm peach bokeh background, and writes the picture to
/output/entry.png (or $MAKEFUN_OUTPUT_DIR/entry.png when set).

How it works: every shape is a signed-distance field painted into a
512x512 float buffer (discs, ellipses, rounded triangles, line segments,
flowers made of rotated petals). The buffer is then upscaled to
1024x1024 with bilinear interpolation, which also softens the edges,
and encoded as a PNG by hand (zlib + struct + crc32, no Pillow needed).

Determinism: no randomness, no wall clock, no network, no dict-order
dependence. Two runs write byte-identical files.
"""

import binascii
import math
import os
import struct
import zlib

RENDER = 512        # internal render size (square)
OUT_SIZE = 1024     # final image size (square)
OUT_NAME = "entry.png"


# ---------------------------------------------------------------- palette
CREAM = (255, 246, 230)      # kitten fur
CREAM_DARK = (245, 223, 192)  # fur shading / outline
EAR_PINK = (247, 184, 196)    # inner ear
NOSE_PINK = (244, 140, 160)   # nose
BLUSH = (248, 170, 185)       # cheek blush
EYE_AMBER = (232, 163, 61)    # iris
EYE_BROWN = (90, 58, 46)      # iris ring / pupil outline
PUPIL = (28, 18, 14)          # pupil
INK = (74, 48, 38)            # mouth / lashes / whiskers
BUNNY = (237, 232, 226)       # plush bunny fur
BUNNY_DARK = (214, 206, 198)  # bunny shading
BOW_PINK = (242, 140, 168)    # bunny bow
PETAL_PINK = (249, 168, 192)
PETAL_WHITE = (255, 255, 255)
FLOWER_YELLOW = (255, 201, 60)
LEAF = (156, 203, 122)
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
            # inside test: same winding on all three edges
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
    """Five-petal daisy: rotated petal ellipses, yellow centre, two leaves."""
    ellipse(buf, cx - r * 0.85, cy + r * 0.45, r * 0.42, r * 0.22,
            LEAF, 0.95, 1.5, rot=0.7)
    ellipse(buf, cx + r * 0.85, cy + r * 0.45, r * 0.42, r * 0.22,
            LEAF, 0.95, 1.5, rot=-0.7)
    for k in range(5):
        ang = -math.pi / 2 + k * (2 * math.pi / 5)
        px = cx + math.cos(ang) * r * 0.52
        py = cy + math.sin(ang) * r * 0.52
        ellipse(buf, px, py, r * 0.30, r * 0.52, petal, 1.0, 1.5,
                rot=ang + math.pi / 2)
    disc(buf, cx, cy, r * 0.30, center, 1.0, 1.2)
    disc(buf, cx - r * 0.08, cy - r * 0.08, r * 0.10, (255, 232, 150), 1.0, 1.0)


def eye(buf, cx, cy, r):
    """One huge amber eye: ring, iris, pupil, two sparkle highlights, lashes."""
    disc(buf, cx, cy, r, EYE_AMBER, 1.0, 1.5)
    disc(buf, cx, cy, r * 0.78, (176, 110, 38), 1.0, 1.5)
    disc(buf, cx, cy, r * 0.52, PUPIL, 1.0, 1.5)
    ring(buf, cx, cy, r, 2.0, EYE_BROWN, 0.85, 1.5)
    disc(buf, cx - r * 0.34, cy - r * 0.36, r * 0.26, (255, 255, 255), 1.0, 1.2)
    disc(buf, cx + r * 0.30, cy + r * 0.34, r * 0.12, (255, 255, 255), 0.95, 1.0)
    # three little lashes on the upper outer edge
    for k, ang in enumerate((-0.5, -0.15, 0.2)):
        sx = cx + math.cos(ang - math.pi / 2) * r * 0.98
        sy = cy + math.sin(ang - math.pi / 2) * r * 0.98
        ex = cx + math.cos(ang - math.pi / 2) * r * 1.22
        ey = cy + math.sin(ang - math.pi / 2) * r * 1.22
        segment(buf, sx, sy, ex, ey, 2.2, INK, 0.85)


# ---------------------------------------------------------------- picture
def draw():
    """Paint the kitten into a RENDER x RENDER float buffer, back to front."""
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

    # kitten body: big soft shape the face and paws sit on
    ellipse(buf, 256, 590, 195, 165, (251, 238, 220), 1.0, 2.0)

    # ears: wide rounded triangles, fur outside, pink inside
    triangle(buf, 108, 212, 142, 96, 228, 182, CREAM, 1.0, 2.0)
    triangle(buf, 404, 212, 370, 96, 284, 182, CREAM, 1.0, 2.0)
    triangle(buf, 130, 192, 150, 122, 202, 176, EAR_PINK, 1.0, 2.0)
    triangle(buf, 382, 192, 362, 122, 310, 176, EAR_PINK, 1.0, 2.0)

    # face: big round cream disc with a soft darker rim
    disc(buf, 256, 265, 148, CREAM, 1.0, 2.0)
    ring(buf, 256, 265, 148, 2.5, CREAM_DARK, 0.55, 2.0)

    # flower crown arcing across the forehead between the ears
    crown = [
        (158, 168, 15, PETAL_PINK),
        (203, 148, 17, PETAL_WHITE),
        (256, 140, 18, PETAL_PINK),
        (309, 148, 17, PETAL_WHITE),
        (354, 168, 15, PETAL_PINK),
    ]
    for cx, cy, r, petal in crown:
        flower(buf, cx, cy, r, petal, FLOWER_YELLOW)

    # soft tabby stripes on the forehead, and fur tufts on the cheeks
    for sx, sy, ex, ey in [(228, 196, 232, 222), (256, 190, 256, 218),
                           (284, 196, 280, 222)]:
        segment(buf, sx, sy, ex, ey, 5.0, CREAM_DARK, 0.35)
    triangle(buf, 118, 300, 104, 268, 128, 288, CREAM, 1.0, 2.0)
    triangle(buf, 394, 300, 408, 268, 384, 288, CREAM, 1.0, 2.0)

    # the huge amber eyes
    eye(buf, 198, 270, 36)
    eye(buf, 314, 270, 36)

    # blush, nose, mouth, whiskers
    ellipse(buf, 158, 332, 26, 14, BLUSH, 0.45, 2.0)
    ellipse(buf, 354, 332, 26, 14, BLUSH, 0.45, 2.0)
    triangle(buf, 244, 318, 268, 318, 256, 334, NOSE_PINK, 1.0, 1.5)
    disc(buf, 252, 322, 3.5, (255, 190, 200), 0.9, 1.0)
    segment(buf, 244, 332, 251, 340, 2.0, INK, 0.9)
    segment(buf, 251, 340, 256, 333, 2.0, INK, 0.9)
    segment(buf, 256, 333, 261, 340, 2.0, INK, 0.9)
    segment(buf, 261, 340, 268, 332, 2.0, INK, 0.9)
    for sx, sy, ex, ey in [(182, 312, 118, 296), (180, 326, 114, 322),
                           (182, 340, 120, 350)]:
        segment(buf, sx, sy, ex, ey, 1.6, (217, 193, 163), 0.75)
    for sx, sy, ex, ey in [(330, 312, 394, 296), (332, 326, 398, 322),
                           (330, 340, 392, 350)]:
        segment(buf, sx, sy, ex, ey, 1.6, (217, 193, 163), 0.75)

    # plush bunny held at the lower right: ears, head, bow, body
    ellipse(buf, 322, 336, 13, 34, BUNNY, 1.0, 2.0, rot=-0.45)
    ellipse(buf, 348, 344, 13, 34, BUNNY, 1.0, 2.0, rot=-0.85)
    ellipse(buf, 322, 338, 6, 22, EAR_PINK, 0.9, 1.5, rot=-0.45)
    ellipse(buf, 348, 346, 6, 22, EAR_PINK, 0.9, 1.5, rot=-0.85)
    disc(buf, 318, 394, 42, BUNNY, 1.0, 2.0)
    disc(buf, 302, 388, 4.5, PUPIL, 1.0, 1.2)
    disc(buf, 332, 388, 4.5, PUPIL, 1.0, 1.2)
    disc(buf, 300, 386, 1.6, (255, 255, 255), 0.9, 1.0)
    disc(buf, 330, 386, 1.6, (255, 255, 255), 0.9, 1.0)
    triangle(buf, 312, 400, 322, 400, 317, 407, NOSE_PINK, 1.0, 1.2)
    segment(buf, 310, 408, 315, 413, 1.6, INK, 0.85)
    segment(buf, 315, 413, 322, 408, 1.6, INK, 0.85)
    ellipse(buf, 296, 406, 8, 5, BLUSH, 0.5, 1.5)
    ellipse(buf, 338, 406, 8, 5, BLUSH, 0.5, 1.5)
    # little pink bow under the bunny's chin
    triangle(buf, 300, 424, 314, 418, 314, 430, BOW_PINK, 1.0, 1.5)
    triangle(buf, 332, 424, 318, 418, 318, 430, BOW_PINK, 1.0, 1.5)
    disc(buf, 316, 424, 4.5, BOW_PINK, 1.0, 1.2)
    ellipse(buf, 316, 464, 34, 28, BUNNY, 1.0, 2.0)
    ellipse(buf, 316, 464, 30, 24, BUNNY_DARK, 0.35, 2.0)

    # kitten paws hugging the bunny from both sides
    ellipse(buf, 218, 452, 38, 23, CREAM_DARK, 0.6, 2.0)
    ellipse(buf, 372, 448, 38, 23, CREAM_DARK, 0.6, 2.0)
    ellipse(buf, 218, 452, 36, 21, CREAM, 1.0, 2.0)
    ellipse(buf, 372, 448, 36, 21, CREAM, 1.0, 2.0)
    segment(buf, 210, 438, 210, 452, 2.0, (190, 160, 120), 0.9)
    segment(buf, 224, 438, 224, 452, 2.0, (190, 160, 120), 0.9)
    segment(buf, 364, 434, 364, 448, 2.0, (190, 160, 120), 0.9)
    segment(buf, 378, 434, 378, 448, 2.0, (190, 160, 120), 0.9)

    # two tiny floating hearts
    for cx, cy, s in [(88, 430, 9), (438, 300, 8)]:
        disc(buf, cx - s * 0.45, cy - s * 0.25, s * 0.55, BOW_PINK, 0.55, 1.5)
        disc(buf, cx + s * 0.45, cy - s * 0.25, s * 0.55, BOW_PINK, 0.55, 1.5)
        triangle(buf, cx - s, cy, cx + s, cy, cx, cy + s * 1.1,
                 BOW_PINK, 0.55, 1.5)

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
