#!/usr/bin/env python3
"""Generate the subcast app icon with Google's Nano Banana Pro (Gemini 3 Pro Image).

Calls the Gemini image-generation REST API (``:generateContent``) with a text
prompt and writes the returned image to disk. Stdlib only — no SDK, no extra
deps (same approach as ``src/subcast/artwork.py``).

The API key is read from an environment variable (``GEMINI_API_KEY`` by default,
falling back to ``GOOGLE_API_KEY``) — it is never passed on the command line so
it can't leak into shell history or ``ps``.

Usage:
    export GEMINI_API_KEY="..."          # https://aistudio.google.com/apikey
    python tools/icon/generate_icon.py                       # default subcast icon
    python tools/icon/generate_icon.py --out icon.png --size 4K
    python tools/icon/generate_icon.py --prompt "a red bicycle" --aspect 16:9
    python tools/icon/generate_icon.py --ref logo.png        # edit a reference image

Exit codes: 0 ok, 1 usage/config error, 2 API/network error, 3 no image in reply.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3-pro-image"  # "Nano Banana Pro"
API_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

# A self-contained default so `generate_icon.py` with no args produces the app
# icon. Override with --prompt for anything else.
DEFAULT_PROMPT = (
    "A modern, minimal app icon for a tool called 'subcast' that casts video "
    "with subtitles to a TV. The icon is a single bold glyph: the left half is "
    "a sleek TV/monitor screen showing a play symbol, and the right half is "
    "subtitle text lines — visually splitting the mark into 'half a TV, half "
    "text'. Flat vector style, rounded squircle tile with a soft purple-to-blue "
    "gradient background, crisp white foreground shapes, generous padding, "
    "high contrast, no lettering, no words. Centered, symmetrical, suitable for "
    "a small favicon and a large app icon. Clean, premium, friendly."
)


def _resolve_api_key() -> str:
    for name in API_KEY_ENVS:
        val = os.environ.get(name)
        if val:
            return val.strip()
    joined = " or ".join(API_KEY_ENVS)
    sys.exit(
        f"error: no API key found. Set one of: {joined}\n"
        "       get a key at https://aistudio.google.com/apikey, then e.g.:\n"
        '       export GEMINI_API_KEY="..."'
    )


def _build_request_body(prompt: str, aspect: str, size: str, ref: Path | None) -> dict:
    """Assemble the generateContent payload (optionally with a reference image)."""
    parts: list[dict] = [{"text": prompt}]
    if ref is not None:
        mime = mimetypes.guess_type(ref.name)[0] or "image/png"
        data = base64.b64encode(ref.read_bytes()).decode("ascii")
        # Image part first so the model treats the text as an edit instruction.
        parts.insert(0, {"inlineData": {"mimeType": mime, "data": data}})
    return {
        "contents": [{"parts": parts}],
        "generationConfig": {
            # Image models still emit a "thinking" text part; ask for both so the
            # request is never rejected for omitting TEXT, then we pick the image.
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": aspect, "imageSize": size},
        },
    }


def _call_api(model: str, body: dict, api_key: str) -> dict:
    url = f"{API_BASE}/{model}:generateContent"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,  # key in header, not the URL
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:  # surface the API's own error message when present
            detail = json.dumps(json.loads(detail)["error"], indent=2)
        except Exception:
            pass
        sys.exit(f"error: API returned HTTP {exc.code}\n{detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"error: could not reach the API: {exc.reason}")


def _extract_image(payload: dict) -> tuple[bytes, str]:
    """Pull the first inline image (base64) out of the response. Defensive about
    camelCase vs snake_case, since the API uses both across surfaces."""
    candidates = payload.get("candidates") or []
    for cand in candidates:
        parts = (cand.get("content") or {}).get("parts") or []
        for part in parts:
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                mime = blob.get("mimeType") or blob.get("mime_type") or "image/png"
                return base64.b64decode(blob["data"]), mime
    # No image — explain why if the model said something or the prompt was blocked.
    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        sys.exit(
            f"error: prompt blocked ({feedback['blockReason']}); no image returned.",
        )
    for cand in candidates:
        for part in (cand.get("content") or {}).get("parts") or []:
            if part.get("text"):
                sys.exit(
                    "error: model returned text but no image:\n"
                    + part["text"].strip()[:500]
                )
    sys.exit(f"error: no image in response: {json.dumps(payload)[:500]}")


def _ext_for(mime: str) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
        mime, mimetypes.guess_extension(mime) or ".png"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Generate an image with Gemini 3 Pro Image (Nano Banana Pro).",
    )
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="text prompt (default: subcast icon)")
    ap.add_argument("--out", type=Path, default=Path("tools/icon/out/subcast-icon.png"),
                    help="output image path (extension adjusted to the returned format)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--aspect", default="1:1",
                    help="aspect ratio, e.g. 1:1, 16:9, 4:3 (default: 1:1)")
    ap.add_argument("--size", default="2K", choices=["1K", "2K", "4K"],
                    help="image resolution (default: 2K)")
    ap.add_argument("--ref", type=Path, default=None,
                    help="reference image to edit/restyle instead of generating from scratch")
    args = ap.parse_args(argv)

    if args.ref is not None and not args.ref.is_file():
        sys.exit(f"error: --ref file not found: {args.ref}")

    api_key = _resolve_api_key()
    body = _build_request_body(args.prompt, args.aspect, args.size, args.ref)

    print(f"requesting {args.model} ({args.aspect}, {args.size})…", file=sys.stderr)
    payload = _call_api(args.model, body, api_key)
    image, mime = _extract_image(payload)

    out = args.out.with_suffix(_ext_for(mime))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(image)
    print(f"wrote {out} ({len(image):,} bytes, {mime})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
