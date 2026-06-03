#!/usr/bin/env python3
"""
social-compose — overlay branded HTML over background media.

Usage:
  compose.py image --background BG.png --brand-id ID --brief TEXT [--output OUT.png]
  compose.py video --background BG.mp4 --brand-id ID --brief TEXT [--audio AUDIO.mp3] [--output OUT.mp4]

Branding config lives at <instance_dir>/branding/<brand-id>/:
  brand.yaml    — colors, fonts, name, logo_position
  logo.svg      — SVG logo (fallback: logo.png)
  overlay.html  — Jinja2 template (receives brand, logo_svg, logo_b64, brief, width, height)
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing pyyaml — pip install pyyaml")

try:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
except ImportError:
    sys.exit("Missing jinja2 — pip install jinja2")

try:
    from PIL import Image
except ImportError:
    sys.exit("Missing Pillow — pip install Pillow")


CHROME_CANDIDATES = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium-browser",
    "/snap/bin/chromium",
]
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def _find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    out = subprocess.run(["which", "google-chrome"], capture_output=True, text=True)
    if out.returncode == 0:
        return out.stdout.strip()
    raise RuntimeError("No Chromium/Chrome binary found. Tried: " + ", ".join(CHROME_CANDIDATES))


def _load_brand(brand_dir: Path) -> dict:
    yaml_path = brand_dir / "brand.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"brand.yaml not found at {yaml_path}")
    with yaml_path.open() as f:
        return yaml.safe_load(f) or {}


def _load_logo(brand_dir: Path) -> tuple[str, str, str]:
    """Return (logo_svg_content, logo_b64, logo_ext)."""
    for name, ext in [("logo.svg", "svg"), ("logo.png", "png"), ("logo.jpg", "jpg")]:
        p = brand_dir / name
        if p.exists():
            raw = p.read_bytes()
            b64 = base64.b64encode(raw).decode()
            svg_content = raw.decode("utf-8", errors="replace") if ext == "svg" else ""
            return svg_content, b64, ext
    return "", "", ""


def _render_overlay_html(brand_dir: Path, brand: dict, logo_svg: str, logo_b64: str,
                          logo_ext: str, brief: str, width: int, height: int) -> str:
    """Render overlay.html Jinja2 template, return absolute path to rendered file."""
    template_path = brand_dir / "overlay.html"
    if not template_path.exists():
        raise FileNotFoundError(f"overlay.html not found at {template_path}")

    env = Environment(
        loader=FileSystemLoader(str(brand_dir)),
        undefined=StrictUndefined,
        autoescape=False,
    )
    tmpl = env.get_template("overlay.html")
    rendered = tmpl.render(
        brand=brand,
        logo_svg=logo_svg,
        logo_b64=logo_b64,
        logo_ext=logo_ext,
        brief=brief,
        width=width,
        height=height,
    )

    tmp = tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write(rendered)
    tmp.close()
    return tmp.name


def _chromium_screenshot(html_path: str, width: int, height: int, out_png: str) -> None:
    chrome = _find_chrome()
    cmd = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-software-rasterizer",
        f"--window-size={width},{height}",
        "--hide-scrollbars",
        "--default-background-color=00000000",
        f"--screenshot={out_png}",
        f"file://{html_path}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not Path(out_png).exists() or Path(out_png).stat().st_size == 0:
        raise RuntimeError(
            f"Chromium screenshot failed (rc={result.returncode}):\n{result.stderr[:500]}"
        )


def _composite_image(background_path: str, overlay_png: str, output_path: str) -> None:
    bg = Image.open(background_path).convert("RGBA")
    ov = Image.open(overlay_png).convert("RGBA")
    if ov.size != bg.size:
        ov = ov.resize(bg.size, Image.LANCZOS)
    bg.paste(ov, (0, 0), ov)
    bg.convert("RGB").save(output_path, "PNG", optimize=True)


def _probe_video(video_path: str) -> tuple[int, int]:
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        video_path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    parts = out.stdout.strip().split(",")
    return int(parts[0]), int(parts[1])


def _composite_video(background_path: str, overlay_png: str, audio_path: str | None,
                      output_path: str) -> None:
    cmd = [FFMPEG, "-y",
           "-i", background_path,
           "-i", overlay_png,
           "-filter_complex", "[0:v][1:v]overlay=0:0[v]",
           "-map", "[v]",
    ]
    if audio_path:
        cmd += ["-i", audio_path, "-map", "2:a", "-c:a", "aac", "-shortest"]
    cmd += [
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-1000:]}")


def _default_output(instance_dir: Path, brand_id: str, ext: str) -> str:
    out_dir = instance_dir / "state" / "generated" / "social"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    return str(out_dir / f"{brand_id}-{ts}.{ext}")


def cmd_image(args: argparse.Namespace, instance_dir: Path) -> None:
    bg_path = Path(args.background).resolve()
    if not bg_path.exists():
        sys.exit(f"Background not found: {bg_path}")

    brand_dir = instance_dir / "branding" / args.brand_id
    if not brand_dir.is_dir():
        sys.exit(f"Brand dir not found: {brand_dir}")

    brand = _load_brand(brand_dir)
    logo_svg, logo_b64, logo_ext = _load_logo(brand_dir)

    with Image.open(bg_path) as im:
        width, height = im.size

    output = args.output or _default_output(instance_dir, args.brand_id, "png")

    with tempfile.TemporaryDirectory() as tmpdir:
        html_path = _render_overlay_html(
            brand_dir, brand, logo_svg, logo_b64, logo_ext,
            args.brief, width, height
        )
        overlay_png = str(Path(tmpdir) / "overlay.png")
        try:
            print(f"Rendering overlay ({width}x{height})...", file=sys.stderr)
            _chromium_screenshot(html_path, width, height, overlay_png)
            print("Compositing...", file=sys.stderr)
            _composite_image(str(bg_path), overlay_png, output)
        finally:
            Path(html_path).unlink(missing_ok=True)

    print(output)


def cmd_video(args: argparse.Namespace, instance_dir: Path) -> None:
    bg_path = Path(args.background).resolve()
    if not bg_path.exists():
        sys.exit(f"Background not found: {bg_path}")

    brand_dir = instance_dir / "branding" / args.brand_id
    if not brand_dir.is_dir():
        sys.exit(f"Brand dir not found: {brand_dir}")

    brand = _load_brand(brand_dir)
    logo_svg, logo_b64, logo_ext = _load_logo(brand_dir)

    print("Probing video dimensions...", file=sys.stderr)
    width, height = _probe_video(str(bg_path))

    output = args.output or _default_output(instance_dir, args.brand_id, "mp4")
    audio_path = args.audio

    with tempfile.TemporaryDirectory() as tmpdir:
        html_path = _render_overlay_html(
            brand_dir, brand, logo_svg, logo_b64, logo_ext,
            args.brief, width, height
        )
        overlay_png = str(Path(tmpdir) / "overlay.png")
        try:
            print(f"Rendering overlay ({width}x{height})...", file=sys.stderr)
            _chromium_screenshot(html_path, width, height, overlay_png)
            print("Compositing video...", file=sys.stderr)
            _composite_video(str(bg_path), overlay_png, audio_path, output)
        finally:
            Path(html_path).unlink(missing_ok=True)

    print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Social content composer")
    parser.add_argument(
        "--instance-dir", default=os.environ.get("JC_INSTANCE_DIR", "."),
        help="JC instance directory (default: $JC_INSTANCE_DIR or .)"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    img = sub.add_parser("image", help="Composite overlay over background PNG")
    img.add_argument("--instance-dir", dest="instance_dir_sub",
                     default=None, help="Override global --instance-dir")
    img.add_argument("--background", required=True)
    img.add_argument("--brand-id", required=True)
    img.add_argument("--brief", required=True)
    img.add_argument("--output")

    vid = sub.add_parser("video", help="Composite overlay over background MP4")
    vid.add_argument("--instance-dir", dest="instance_dir_sub",
                     default=None, help="Override global --instance-dir")
    vid.add_argument("--background", required=True)
    vid.add_argument("--brand-id", required=True)
    vid.add_argument("--brief", required=True)
    vid.add_argument("--audio", default=None, help="Optional audio MP3 (default: no audio)")
    vid.add_argument("--output")

    args = parser.parse_args()
    # Sub-command --instance-dir overrides global one
    effective_dir = getattr(args, "instance_dir_sub", None) or args.instance_dir
    instance_dir = Path(effective_dir).resolve()

    if args.mode == "image":
        cmd_image(args, instance_dir)
    elif args.mode == "video":
        cmd_video(args, instance_dir)


if __name__ == "__main__":
    main()
