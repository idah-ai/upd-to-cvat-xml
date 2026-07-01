"""Command-line interface for the UPD → CVAT exporter."""

from __future__ import annotations

import argparse

from .converter import run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="upd-to-cvat",
        description="Export IDAH datasets from a UPD file to CVAT 1.1 packages.",
    )
    p.add_argument("--upd", required=True, help="Input UPD file path.")
    p.add_argument("--output", default="cvat-export", help="Output root directory.")
    p.add_argument("--with-images", action="store_true",
                   help="Also extract frame PNGs via ffmpeg (requires ffmpeg).")
    p.add_argument("--keyframes-only", action="store_true",
                   help="Video: emit only the IDAH keyframes and let CVAT "
                        "interpolate between them, instead of materialising "
                        "every frame. Smaller files; bboxes identical, but "
                        "polygon interpolation will differ from the frontend.")
    p.add_argument("--dataset", default=None, help="Optional dataset-id filter.")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run(args.upd, args.output,
        with_images=args.with_images, dataset_filter=args.dataset,
        keyframes_only=args.keyframes_only)


if __name__ == "__main__":
    main()
