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
                   help="Also extract frame PNGs (video) / copy source images.")
    p.add_argument("--no-clamp", dest="clamp", action="store_false",
                   help="Disable clamping shapes to the image/frame bounds. By "
                        "default every point is clipped into [0, width] × "
                        "[0, height] since IDAH normalised points can drift "
                        "outside [0, 1]; pass this to keep the raw coordinates.")
    p.add_argument("--dataset-id", dest="dataset_id", default=None,
                   help="Export only this dataset, at CVAT task level: one "
                        "annotations.xml per entry (video) or per dataset "
                        "(image). Without any filter the export is at project "
                        "level instead — one annotations.xml per dataset, with "
                        "every entry as a <task> and project-wide frame "
                        "numbering.")
    p.add_argument("--entry-id", dest="entry_id", default=None,
                   help="Export only this entry, at CVAT job level: one "
                        "annotations.xml with a <job> meta block.")
    p.add_argument("--split", type=_positive_int, default=None, metavar="N",
                   help="Split a project-level export into packages of at most "
                        "N entries each, named project_<dataset>_partNNofMM, so "
                        "a large dataset can be uploaded to CVAT in several "
                        "smaller batches instead of one huge one. Ignored at "
                        "task/job level, which already write one package per "
                        "entry.")
    return p


def _positive_int(text: str) -> int:
    """``--split`` accepts entry counts only: a whole number of entries ≥ 1."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run(args.upd, args.output,
        with_images=args.with_images, dataset_id=args.dataset_id,
        entry_id=args.entry_id, clamp=args.clamp, split=args.split)


if __name__ == "__main__":
    main()
