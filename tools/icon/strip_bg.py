#!/usr/bin/env python3
"""Strip a flat background off a generated glyph into a transparent PNG.

Nano Banana Pro can't emit transparency, so we generate the glyph as a solid
white shape on a flat solid-black field (see generate_icon.py) and key it out
here: the background colour is made transparent within a fuzz tolerance, leaving
the anti-aliased glyph with clean soft edges. The result is then trimmed,
re-padded, and downscaled to a small square PNG.

Optionally recolours the glyph (alpha preserved) — handy if you don't want to
tint it purely via CSS.

Wraps ImageMagick (`convert`); no Python image libs required.

Usage:
    python tools/icon/strip_bg.py in.jpg -o out.png             # 512px, native white
    python tools/icon/strip_bg.py in.jpg -o out.png --size 256 --pad 0.14
    python tools/icon/strip_bg.py in.jpg -o out.png --fill '#6d8bff'   # recolour glyph
    python tools/icon/strip_bg.py in.jpg -o out.png --bg white --fuzz 25
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _magick() -> list[str]:
    for cand in ("magick", "convert"):
        if shutil.which(cand):
            # `magick convert ...` on IM7, bare `convert` on IM6.
            return [cand, "convert"] if cand == "magick" else [cand]
    sys.exit("error: ImageMagick not found (need `magick` or `convert` on PATH)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Key a flat background off a glyph -> transparent PNG.")
    ap.add_argument("input", type=Path, help="source image (solid glyph on a flat background)")
    ap.add_argument("-o", "--out", type=Path, required=True, help="output PNG path")
    ap.add_argument("--size", type=int, default=512, help="output square size in px (default 512)")
    ap.add_argument("--pad", type=float, default=0.12, help="padding as a fraction of size (default 0.12)")
    ap.add_argument("--bg", default="black", help="flat background colour to key out (default black)")
    ap.add_argument("--fuzz", type=float, default=30.0,
                    help="colour tolerance %% when keying the background (default 30)")
    ap.add_argument("--fill", default="keep",
                    help="recolour the glyph to this colour, or 'keep' to preserve (default keep)")
    args = ap.parse_args(argv)

    if not args.input.is_file():
        sys.exit(f"error: input not found: {args.input}")
    if not 0 <= args.pad < 0.5:
        sys.exit("error: --pad must be in [0, 0.5)")

    inner = max(1, round(args.size * (1 - 2 * args.pad)))
    args.out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        *_magick(), str(args.input),
        # Key the flat field to transparent; anti-aliased glyph edges survive.
        "-fuzz", f"{args.fuzz}%", "-transparent", args.bg,
    ]
    if args.fill != "keep":
        # Recolour RGB only (+channel restores default) so alpha is untouched.
        cmd += ["-channel", "RGB", "-fill", args.fill, "-colorize", "100%", "+channel"]
    cmd += [
        "-trim", "+repage",
        "-resize", f"{inner}x{inner}",
        "-background", "none", "-gravity", "center", "-extent", f"{args.size}x{args.size}",
        f"PNG32:{args.out}",
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"error: ImageMagick failed:\n{proc.stderr.strip()}")

    print(f"wrote {args.out} ({args.out.stat().st_size:,} bytes, "
          f"{args.size}x{args.size}, bg={args.bg}, fill={args.fill})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
