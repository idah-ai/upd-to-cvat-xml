"""
UPD → CVAT 1.1 conversion.

Reads IDAH datasets out of a UPD file (via the ``upd`` SDK) and writes CVAT 1.1
annotation packages.

CVAT ships two on-disk variants — *CVAT for images 1.1* and *CVAT for video
1.1*. They share an identical ``<meta>`` block and differ only in the body:
images group shapes under ``<image>`` elements, video groups them under
``<track>`` elements that carry an object identity across frames. IDAH mirrors
this split via the dataset ``modality`` column (``idah-image`` / ``idah-video``).

Both modalities are supported:

- ``idah-video`` → *CVAT for video 1.1*. Each entry is one video, so one entry
  → one task folder, with ``<track>``s::

      <output>/<dataset>/<entry>_<media-id>/annotations.xml
                                            images/frame_000000.PNG   (--with-images)

- ``idah-image`` → *CVAT for images 1.1*. The whole dataset is one task, one
  ``<image>`` per entry::

      <output>/<dataset>/annotations.xml
                         images/<name>.jpg                            (--with-images)

CVAT stores absolute pixel coordinates while IDAH stores normalised [0, 1]
points, so each video/image is probed with ``ffprobe`` for its dimensions (and
frame count, for video). ``ffprobe`` is therefore required even in
annotations-only mode; ``ffmpeg`` frame decoding is only needed with
``with_images`` for video (both ship in the same package).
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from upd import UPD

from . import interpolation as interp


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def check_ffmpeg(*, need_ffmpeg: bool) -> None:
    """Ensure ffprobe (always) and ffmpeg (when extracting frames) are on PATH."""
    if shutil.which("ffprobe") is None:
        raise RuntimeError(
            "ffprobe not found on PATH. CVAT export needs video dimensions; "
            "please install ffmpeg (e.g. `brew install ffmpeg` or "
            "`apt-get install ffmpeg`)."
        )
    if need_ffmpeg and shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. --with-images extracts frame images; "
            "please install ffmpeg (e.g. `brew install ffmpeg` or "
            "`apt-get install ffmpeg`)."
        )


def probe_video(path: str) -> tuple[int, int, int]:
    """Return (width, height, n_frames) for a video file via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,nb_frames",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    width = int(stream["width"])
    height = int(stream["height"])
    nb = stream.get("nb_frames")
    # nb_frames can be "N/A" for some containers; fall back to a frame count.
    if nb in (None, "N/A"):
        n_frames = _count_frames(path)
    else:
        n_frames = int(nb)
    return width, height, n_frames


def _count_frames(path: str) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "default=nokey=1:noprint_wrappers=1", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return int(out) if out.isdigit() else 0


def extract_frames(video_path: str, images_dir: Path) -> int:
    """Extract every frame to images/frame_%06d.PNG. Returns the count."""
    images_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", video_path,
         "-start_number", "0", str(images_dir / "frame_%06d.PNG")],
        check=True,
    )
    return len(list(images_dir.glob("frame_*.PNG")))


def probe_image(path: str) -> tuple[int, int]:
    """Return (width, height) for an image file via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


# ---------------------------------------------------------------------------
# CVAT XML builders
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """Filesystem-safe folder name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "unnamed"


def build_labels(labeling_config: dict) -> str:
    """Build the CVAT <labels> block from a dataset Labeling-Configuration.

    Uses the value ``id`` (the IDAH tree-path key, e.g. ``"vehicles/truck"``)
    as the CVAT label ``<name>``, *not* the human ``label`` ("Truck"). IDAH's
    labels are a tree, so the same ``label`` can appear under different branches
    — only the ``id`` is unique. CVAT label names must be unique within a task
    and shapes reference labels by name, so keying on the ``id`` keeps every
    category distinct and matches the ``label=`` written on each shape/track.
    IDAH `properties` are empty in practice, so attributes are emitted empty.
    """
    seen: dict[str, str] = {}          # label id -> color
    for shape_cfg in (labeling_config or {}).values():
        for value in shape_cfg.get("values", []):
            vid = value.get("id") or value.get("label")
            if vid and vid not in seen:
                seen[vid] = value.get("color", "")

    lines = ["      <labels>"]
    for vid, color in seen.items():
        lines += [
            "        <label>",
            f"          <name>{escape(vid)}</name>",
            f"          <color>{escape(color)}</color>",
            "          <type>any</type>",
            "          <attributes></attributes>",
            "        </label>",
        ]
    lines.append("      </labels>")
    return "\n".join(lines)


def build_meta(*, task_id: int, name: str, size: int, labels_xml: str,
               width: int, height: int, source: str) -> str:
    """Emit the CVAT <meta> block (video / interpolation mode)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
    return f"""  <meta>
    <task>
      <id>{task_id}</id>
      <name>{escape(name)}</name>
      <size>{size}</size>
      <mode>interpolation</mode>
      <overlap>0</overlap>
      <bugtracker></bugtracker>
      <created>{now}</created>
      <updated>{now}</updated>
      <subset>default</subset>
      <start_frame>0</start_frame>
      <stop_frame>{max(size - 1, 0)}</stop_frame>
      <frame_filter></frame_filter>
{labels_xml}
      <original_size>
        <width>{width}</width>
        <height>{height}</height>
      </original_size>
      <source>{escape(source)}</source>
    </task>
    <dumped>{now}</dumped>
  </meta>"""


def bbox_to_cvat(points_norm: list[list[float]], angle: float,
                 w: int, h: int) -> tuple[float, float, float, float, float]:
    """Normalised corner points + angle → (xtl, ytl, xbr, ybr, rotation°).

    The stored corners are the *unrotated* axis-aligned box (the frontend keeps
    rotation separate and rotates about the centre at render time), so min/max
    gives the box and ``rotation`` is the angle. IDAH stores the angle in
    radians; CVAT's ``rotation`` attribute is in degrees.
    """
    xs = [p[0] * w for p in points_norm]
    ys = [p[1] * h for p in points_norm]
    return min(xs), min(ys), max(xs), max(ys), math.degrees(angle or 0.0)


def polygon_to_cvat(points_norm: list[list[float]], w: int, h: int) -> str:
    """Normalised points → CVAT 'x1,y1;x2,y2;…' absolute-pixel string."""
    return ";".join(f"{p[0] * w:.2f},{p[1] * h:.2f}" for p in points_norm)


def _shape_suffix(shape_type: str) -> str:
    """shape_type suffix after the modality prefix, e.g. 'bounding-box'."""
    return shape_type.split(":", 1)[-1]


def write_video_body(annotations: list, w: int, h: int, n_frames: int, *,
                     keyframes_only: bool = False) -> str:
    """Build the <track> body for one video from its annotations.

    By default every frame in each track's ``[start, end]`` is materialised
    using the interpolation helper (bbox = linear, polygon = flubber), rather
    than emitting only keyframes and relying on CVAT's own interpolation — so
    the exported geometry matches the frontend's interpolation exactly. The
    original IDAH keyframes are flagged ``keyframe="1"``, interpolated frames
    ``keyframe="0"`` (CVAT "for video 1.1" convention).

    With ``keyframes_only`` only the original IDAH keyframes are emitted (each
    ``keyframe="1"``) and CVAT interpolates between them on import. This yields
    far smaller files; bboxes are identical (both sides interpolate linearly),
    but polygons differ — CVAT's polygon interpolation is not flubber, so the
    in-between shapes will not match the frontend.

    The track ``label=`` is the annotation ``category`` (the IDAH tree-path
    *id*, e.g. ``"vehicles/truck"``), used verbatim so it matches the ``id``-
    keyed ``<labels>`` block — CVAT rejects tracks whose label is not declared.
    """
    blocks: list[str] = []
    track_id = 0

    for ann in annotations:
        suffix = _shape_suffix(ann.shape_type)
        if suffix not in (interp.BBOX, interp.POLYGON):
            print(f"    ! skipping unsupported shape_type {ann.shape_type!r}")
            continue

        sa = ann.shape_args
        frames = sa.get("frames", [])
        if not frames:
            continue
        label = ann.annotation.get("category", "")
        keyframe_nums = {f["frame"] for f in frames}
        end = sa.get("end", frames[-1]["frame"])

        frame_iter = (interp.iter_keyframes(sa, kind=suffix) if keyframes_only
                      else interp.iter_frames(sa, kind=suffix))

        shapes: list[str] = []
        last_points = last_angle = None
        for frame, points, angle in frame_iter:
            kf = 1 if (keyframes_only or frame in keyframe_nums) else 0
            shapes.append(_frame_shape(suffix, frame, points, w, h,
                                       keyframe=kf, outside=0, angle=angle))
            last_points, last_angle = points, angle

        # Terminate the track with an outside="1" shape one frame past the end,
        # unless the track already runs to the last video frame. CVAT marks the
        # terminating outside shape as a keyframe (keyframe="1").
        if end + 1 <= n_frames - 1:
            shapes.append(_frame_shape(suffix, end + 1, last_points, w, h,
                                       keyframe=1, outside=1, angle=last_angle))

        blocks.append(
            f'  <track id="{track_id}" label={quoteattr(label)} source="manual">\n'
            + "\n".join(shapes)
            + "\n  </track>"
        )
        track_id += 1

    return "\n".join(blocks)


def _frame_shape(suffix: str, frame: int, points: list, w: int, h: int, *,
                 keyframe: int, outside: int, angle: float = 0.0) -> str:
    common = f'frame="{frame}" keyframe="{keyframe}" outside="{outside}" occluded="0"'

    if suffix == interp.BBOX:
        xtl, ytl, xbr, ybr, rot = bbox_to_cvat(points, angle, w, h)
        rot_attr = f' rotation="{rot:.2f}"' if rot else ""
        return (f'    <box {common} '
                f'xtl="{xtl:.2f}" ytl="{ytl:.2f}" xbr="{xbr:.2f}" ybr="{ybr:.2f}"'
                f'{rot_attr} z_order="0">\n    </box>')

    # polygon
    pts = polygon_to_cvat(points, w, h)
    return (f'    <polygon {common} points="{pts}" z_order="0">\n'
            f'    </polygon>')


# ---------------------------------------------------------------------------
# CVAT "for images 1.1" builders
# ---------------------------------------------------------------------------

def build_meta_images(*, task_id: int, name: str, size: int, labels_xml: str) -> str:
    """Emit the CVAT <meta> block for an image task (annotation mode)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
    return f"""  <meta>
    <task>
      <id>{task_id}</id>
      <name>{escape(name)}</name>
      <size>{size}</size>
      <mode>annotation</mode>
      <overlap>0</overlap>
      <bugtracker></bugtracker>
      <created>{now}</created>
      <updated>{now}</updated>
      <subset>default</subset>
{labels_xml}
    </task>
    <dumped>{now}</dumped>
  </meta>"""


def _image_shape(suffix: str, shape_args: dict, w: int, h: int, label: str) -> str | None:
    """One CVAT image-format shape element, or None for unsupported types."""
    common = f'label={quoteattr(label)} source="manual" occluded="0"'
    points = shape_args.get("points", [])

    if suffix == "bounding-box":
        xtl, ytl, xbr, ybr, rot = bbox_to_cvat(points, shape_args.get("angle", 0), w, h)
        rot_attr = f' rotation="{rot:.2f}"' if rot else ""
        return (f'    <box {common} '
                f'xtl="{xtl:.2f}" ytl="{ytl:.2f}" xbr="{xbr:.2f}" ybr="{ybr:.2f}"'
                f'{rot_attr} z_order="0"></box>')

    if suffix == "polygon":
        return f'    <polygon {common} points="{polygon_to_cvat(points, w, h)}" z_order="0"></polygon>'

    if suffix == "line":   # IDAH line → CVAT polyline (CVAT has no "line")
        return f'    <polyline {common} points="{polygon_to_cvat(points, w, h)}" z_order="0"></polyline>'

    if suffix in ("ellipse", "circle"):
        # points = [[cx, cy], [rx, ry]] (normalised); circle has rx == ry.
        (cx, cy), (rx, ry) = points[0], points[1]
        rot = math.degrees(shape_args.get("angle", 0) or 0.0)
        rot_attr = f' rotation="{rot:.2f}"' if rot else ""
        return (f'    <ellipse {common} '
                f'cx="{cx * w:.2f}" cy="{cy * h:.2f}" rx="{rx * w:.2f}" ry="{ry * h:.2f}"'
                f'{rot_attr} z_order="0"></ellipse>')

    print(f"    ! skipping unsupported shape_type suffix {suffix!r}")
    return None


def write_image_body(annotations: list, w: int, h: int) -> str:
    """Shape elements for one image (CVAT image format).

    The shape ``label=`` is the annotation ``category`` (the IDAH tree-path
    *id*) verbatim, matching the ``id``-keyed ``<labels>`` block.
    """
    out: list[str] = []
    for ann in annotations:
        suffix = _shape_suffix(ann.shape_type)
        label = ann.annotation.get("category", "")
        el = _image_shape(suffix, ann.shape_args, w, h, label)
        if el is not None:
            out.append(el)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Export driver
# ---------------------------------------------------------------------------

def export_video_entry(upd, ds, entry, out_dir: Path, *, task_id: int,
                       with_images: bool, keyframes_only: bool = False) -> None:
    media = upd.medias.get(entry.local_media_id)
    if media is None or media.blob_data is None:
        print(f"  ! entry {entry.id}: media missing, skipping")
        return

    entry_name = entry.metadata.get("Name") or entry.local_media_id
    suffix = Path(entry.local_media_id).suffix or ".mp4"

    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(media.blob_data)
        tmp.flush()
        width, height, n_frames = probe_video(tmp.name)

        annotations = upd.annotations.for_entry(entry.id)
        labels_xml = build_labels(ds.metadata.get("Labeling-Configuration", {}))
        meta = build_meta(
            task_id=task_id, name=entry_name, size=n_frames,
            labels_xml=labels_xml, width=width, height=height, source=entry_name,
        )
        body = write_video_body(annotations, width, height, n_frames,
                                keyframes_only=keyframes_only)

        xml = (f'<?xml version="1.0" encoding="utf-8"?>\n'
               f'<annotations>\n  <version>1.1</version>\n'
               f'{meta}\n{body}\n</annotations>\n')

        # Several entries can share the same human Name (same source video);
        # the media id is unique, so fold it in to keep task folders distinct.
        folder = f"{_safe_name(Path(entry_name).stem)}_{Path(entry.local_media_id).stem}"
        task_dir = out_dir / _safe_name(ds.name) / folder
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "annotations.xml").write_text(xml, encoding="utf-8")

        n_tracks = body.count("<track ")
        msg = f"  [{entry_name}] {width}x{height}, {n_frames} frames, {n_tracks} tracks"

        if with_images:
            n = extract_frames(tmp.name, task_dir / "images")
            msg += f", {n} frames extracted"
        print(msg)


def export_image_dataset(upd, ds, out_dir: Path, *, task_id: int, with_images: bool) -> None:
    """Export an idah-image dataset to a single CVAT 'for images 1.1' task.

    All entries become ``<image>`` elements in one ``annotations.xml`` (the CVAT
    images convention), optionally alongside an ``images/`` folder.
    """
    labels_xml = build_labels(ds.metadata.get("Labeling-Configuration", {}))

    task_dir = out_dir / _safe_name(ds.name)
    images_dir = task_dir / "images"
    blocks: list[str] = []
    seen_names: set[str] = set()
    n_shapes = 0

    entries = [e for e in upd.entries.for_dataset(ds.id) if e.is_local]
    for img_id, entry in enumerate(entries):
        media = upd.medias.get(entry.local_media_id)
        if media is None or media.blob_data is None:
            print(f"  ! entry {entry.id}: media missing, skipping")
            continue

        name = entry.metadata.get("Name") or entry.local_media_id
        if name in seen_names:                       # keep image names unique
            name = f"{Path(name).stem}_{Path(entry.local_media_id).stem}{Path(name).suffix}"
        seen_names.add(name)

        suffix = Path(entry.local_media_id).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(media.blob_data)
            tmp.flush()
            width, height = probe_image(tmp.name)

        annotations = upd.annotations.for_entry(entry.id)
        body = write_image_body(annotations, width, height)
        n_shapes += body.count("<")
        blocks.append(
            f'  <image id="{img_id}" name="{escape(name)}" '
            f'width="{width}" height="{height}">\n{body}\n  </image>'
            if body else
            f'  <image id="{img_id}" name="{escape(name)}" width="{width}" height="{height}"></image>'
        )

        if with_images:
            images_dir.mkdir(parents=True, exist_ok=True)
            (images_dir / name).write_bytes(media.blob_data)

    meta = build_meta_images(task_id=task_id, name=ds.name, size=len(blocks),
                             labels_xml=labels_xml)
    xml = (f'<?xml version="1.0" encoding="utf-8"?>\n'
           f'<annotations>\n  <version>1.1</version>\n'
           f'{meta}\n' + "\n".join(blocks) + "\n</annotations>\n")

    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "annotations.xml").write_text(xml, encoding="utf-8")
    msg = f"  {len(blocks)} images, {n_shapes} shapes"
    if with_images:
        msg += ", images copied"
    print(msg)


def run(upd_path: str, output: str, *, with_images: bool = False,
        dataset_filter: str | None = None, keyframes_only: bool = False) -> None:
    """Export every supported dataset in ``upd_path`` to CVAT under ``output``.

    ``keyframes_only`` (video only) emits just the original IDAH keyframes and
    lets CVAT interpolate between them, instead of materialising every frame —
    see :func:`write_video_body`.
    """
    check_ffmpeg(need_ffmpeg=with_images)
    out_dir = Path(output)

    with UPD.open(upd_path, read_only=True) as upd:
        datasets = upd.datasets.all()
        if dataset_filter:
            datasets = [d for d in datasets if d.id == dataset_filter]

        # CVAT task <id> must be an integer; IDAH ids are UUIDv7 strings. CVAT
        # reassigns its own id on import, so a sequential counter per exported
        # task is sufficient (the IDAH identity is preserved in the task name /
        # folder name).
        task_id = 0

        for ds in datasets:
            print(f"Dataset {ds.name!r} (modality={ds.modality})")

            if ds.modality == "idah-image":
                export_image_dataset(upd, ds, out_dir, task_id=task_id, with_images=with_images)
                task_id += 1
                continue
            if ds.modality != "idah-video":
                print(f"  ! unsupported modality {ds.modality!r}, skipping")
                continue

            for entry in upd.entries.for_dataset(ds.id):
                if not entry.is_local:
                    print(f"  ! entry {entry.id}: non-local media, skipping")
                    continue
                export_video_entry(upd, ds, entry, out_dir, task_id=task_id,
                                    with_images=with_images, keyframes_only=keyframes_only)
                task_id += 1

    print(f"\nWritten: {out_dir}")
