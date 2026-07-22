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

- ``idah-video`` → *CVAT for video 1.1*. Each entry is one video, with
  ``<track>``s.
- ``idah-image`` → *CVAT for images 1.1*. The whole dataset is one task, one
  ``<image>`` per entry.

CVAT dumps annotations at three levels — project, task and job — which differ in
their ``<meta>`` block and in how the media is laid out. IDAH has only two levels
(dataset → entry), so the level is chosen by how deeply the export is filtered;
see :data:`PROJECT` and :func:`level_for` for the mapping::

    (no filter)      project   <output>/project_<dataset>/annotations.xml
                                                          images/<subset>/frame_000000.PNG
    --dataset-id     task      <output>/<dataset>/<entry>_<media-id>/annotations.xml
                                                                     images/frame_000000.PNG
    --entry-id       job       <output>/<dataset>/job_<entry>_<media-id>/annotations.xml
                                                                        images/frame_000000.PNG

At project level each entry gets its own *subset*, since CVAT creates one task
per subset on import — see :func:`export_video_project`.

For ``idah-image`` the dataset is a single task at every level, so the task- and
job-level paths lose the per-entry folder and the images are ``<name>.jpg``.

CVAT stores absolute pixel coordinates while IDAH stores normalised [0, 1]
points, so each video/image is probed with PyAV (``av``) for its dimensions
(and frame count, for video). PyAV bundles the ffmpeg libraries in its wheel,
so no system ``ffmpeg``/``ffprobe`` binary is required; frame decoding to PNGs
is only done with ``with_images`` for video.
"""

from __future__ import annotations

import math
import os
import re
import sys
import tempfile
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import av
from upd import UPD

from . import interpolation as interp


# ---------------------------------------------------------------------------
# Media probing / frame extraction (PyAV — bundles ffmpeg, no system binary)
# ---------------------------------------------------------------------------

def probe_video(path: str) -> tuple[int, int, int]:
    """Return (width, height, n_frames) for a video file via PyAV.

    ``stream.frames`` is the container's *metadata* count. It is 0 for some
    containers, and for others (notably the ``.ts`` transport streams IDAH
    exports) it is simply wrong — one sample reports 3193 frames where only 3081
    decode. An overstated count is not cosmetic: ``<size>`` in the ``<meta>``
    then promises CVAT more frames than the export ships, and the track
    terminator is placed past the end of the video.

    So the metadata count is cross-checked against ``duration × average_rate``,
    which is independent of it, and only trusted when the two agree. Otherwise
    the frames are decode-counted — expensive, but it is the only way to be
    sure, and it is what ``--with-images`` will produce anyway.
    """
    with av.open(path) as container:
        stream = container.streams.video[0]
        width = stream.codec_context.width
        height = stream.codec_context.height
        n_frames = stream.frames
        estimate = None
        if container.duration and stream.average_rate:
            estimate = (container.duration / 1_000_000) * float(stream.average_rate)

    # Allow a frame of slack: duration/rate are themselves rounded.
    if n_frames <= 0 or (estimate is not None and abs(n_frames - estimate) > 1):
        n_frames = _count_frames(path)
    return width, height, n_frames


def _count_frames(path: str) -> int:
    """Exact frame count by decoding every frame of the first video stream."""
    with av.open(path) as container:
        return sum(1 for _ in container.decode(video=0))


def _status(text: str, *, final: bool = False) -> None:
    """Render an in-place progress line.

    On a TTY the line is rewritten with ``\\r`` (padded to clear the previous,
    possibly longer, line) and only terminated with a newline when ``final``.
    When output is redirected (not a TTY) the intermediate updates are dropped —
    only the final line is emitted — so logs and pipes stay clean.
    """
    if sys.stdout.isatty():
        print(f"\r{text:<56}", end="\n" if final else "", flush=True)
    elif final:
        print(text)


def extract_frames(video_path: str, images_dir: Path, *,
                   total: int | None = None) -> int:
    """Extract every frame to images/frame_%06d.PNG. Returns the count.

    Frames are decoded sequentially (with ffmpeg's multithreaded decoding) while
    the PNG encode + write of each frame is fanned out across a thread pool —
    PyAV releases the GIL inside ``encode``, so this scales across cores. PNG
    stays lossless but uses ``compression_level=1`` (fast, ~5% larger files than
    the default). ``total`` (the expected frame count) drives the progress line.

    A bounded in-flight window applies backpressure so decoded frames can't pile
    up in memory faster than they are written (~6 MB per 1080p RGB frame).
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    workers = min(8, os.cpu_count() or 4)
    max_inflight = workers * 4
    step = max(1, (total or 100) // 100)

    def encode_write(idx: int, rgb: av.VideoFrame) -> None:
        enc = av.CodecContext.create("png", "w")
        enc.pix_fmt = "rgb24"
        enc.width, enc.height = rgb.width, rgb.height
        enc.options = {"compression_level": "1"}  # lossless; low level = fast
        data = b"".join(bytes(p) for p in enc.encode(rgb))
        (images_dir / f"frame_{idx:06d}.PNG").write_bytes(data)

    n = 0
    pending: deque[Future] = deque()
    with av.open(video_path) as container, \
            ThreadPoolExecutor(max_workers=workers) as pool:
        container.streams.video[0].thread_type = "AUTO"
        for frame in container.decode(video=0):
            rgb = frame.reformat(format="rgb24")
            pending.append(pool.submit(encode_write, n, rgb))
            n += 1
            if len(pending) >= max_inflight:
                pending.popleft().result()        # backpressure
            if n % step == 0:
                pct = f" ({n * 100 // total}%)" if total else ""
                _status(f"    extracting frames: {n}/{total or '?'}{pct}")
        for fut in pending:                       # drain remaining writes
            fut.result()
    _status(f"    {n} frames extracted", final=True)
    return n


def probe_image(path: str) -> tuple[int, int]:
    """Return (width, height) for an image file via PyAV."""
    with av.open(path) as container:
        stream = container.streams.video[0]
        return stream.codec_context.width, stream.codec_context.height


# ---------------------------------------------------------------------------
# CVAT XML builders
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """Filesystem-safe folder name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "unnamed"


#: IDAH shape_type suffix → CVAT label ``<type>``. The type pins which tool a
#: label may be drawn with; ``any`` leaves it unconstrained. CVAT has no circle
#: primitive, so IDAH circles are ellipses here exactly as :func:`_image_shape`
#: emits them.
CVAT_LABEL_TYPES = {
    "bounding-box": "rectangle",
    "polygon": "polygon",
    "line": "polyline",
    "ellipse": "ellipse",
    "circle": "ellipse",
    "points": "points",
}


def build_labels(labeling_config: dict) -> str:
    """Build the CVAT <labels> block from a dataset Labeling-Configuration.

    Uses the value ``id`` (the IDAH tree-path key, e.g. ``"vehicles/truck"``)
    as the CVAT label ``<name>``, *not* the human ``label`` ("Truck"). IDAH's
    labels are a tree, so the same ``label`` can appear under different branches
    — only the ``id`` is unique. CVAT label names must be unique within a task
    and shapes reference labels by name, so keying on the ``id`` keeps every
    category distinct and matches the ``label=`` written on each shape/track.
    IDAH `properties` are empty in practice, so attributes are emitted empty
    (occlusion rides on CVAT's built-in flag — see :func:`is_occluded`).

    The Labeling-Configuration is keyed by *shape type*, and the same label id
    routinely appears under several of them — ``"car"`` is declared for
    bounding-box, polygon, circle, ellipse and line alike. CVAT has one label per
    name, so those declarations have to be merged:

    - ``<type>`` is the CVAT type of the shape the label is declared for, which
      constrains it to the right drawing tool. A label declared under *several*
      shape types cannot be constrained to one and falls back to ``any``.
    - ``<color>`` is taken from the first shape type in sorted order that
      supplies one. Where a label is declared once this is simply its colour;
      where it is declared many times IDAH often gives it a *different* colour
      per shape type (``"car"`` is ``#FFEC16`` as a box but ``#00A6F5`` as a
      polygon) and the source is genuinely ambiguous, so the rule only has to be
      deterministic — previously this fell out of dict ordering and effectively
      picked at random.
    """
    colors: dict[str, dict[str, str]] = {}   # label id -> {shape suffix: color}
    types: dict[str, set[str]] = {}          # label id -> CVAT types declared

    for shape_type in sorted(labeling_config or {}):
        suffix = _shape_suffix(shape_type)
        for value in (labeling_config[shape_type] or {}).get("values", []):
            vid = value.get("id") or value.get("label")
            if not vid:
                continue
            colors.setdefault(vid, {})[suffix] = value.get("color", "")
            types.setdefault(vid, set()).add(CVAT_LABEL_TYPES.get(suffix, "any"))

    lines = ["      <labels>"]
    for vid, palette in colors.items():
        declared = types[vid]
        kind = declared.pop() if len(declared) == 1 else "any"
        color = next((palette[s] for s in sorted(palette) if palette[s]), "")
        lines += [
            "        <label>",
            f"          <name>{escape(vid)}</name>",
            f"          <color>{escape(color)}</color>",
            f"          <type>{kind}</type>",
            "          <attributes></attributes>",
            "        </label>",
        ]
    lines.append("      </labels>")
    return "\n".join(lines)


def _now() -> str:
    """UTC timestamp in the format CVAT writes into <created>/<dumped>."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


def build_meta(*, task_id: int, name: str, size: int, labels_xml: str,
               width: int, height: int, source: str) -> str:
    """Emit the task-level CVAT <meta> block (video / interpolation mode)."""
    now = _now()
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
      <segments>
        <segment>
          <id>{task_id}</id>
          <start>0</start>
          <stop>{max(size - 1, 0)}</stop>
          <url></url>
        </segment>
      </segments>
      <owner>
        <username></username>
        <email></email>
      </owner>
      <assignee></assignee>
{labels_xml}
      <original_size>
        <width>{width}</width>
        <height>{height}</height>
      </original_size>
      <source>{escape(source)}</source>
    </task>
    <dumped>{now}</dumped>
  </meta>"""


def _task_block(*, task_id: int, name: str, size: int, mode: str,
                subset: str = "default", width: int | None = None,
                height: int | None = None, indent: int = 8) -> str:
    """One <task> element of a project-level <tasks> list.

    Unlike the task-level :func:`build_meta`, this block carries no ``<labels>``
    — a CVAT project declares its label set once, at project level — and it does
    carry ``<segments>``, since a project dump describes the job layout of each
    of its tasks.

    ``subset`` is what actually reconstitutes this task on import: CVAT creates
    one task per *subset*, so each task needs its own (see
    :func:`export_video_project`). ``width``/``height`` are omitted for image
    tasks, which have no single original size (each ``<image>`` carries its own).
    """
    p = " " * indent
    now = _now()
    stop = max(size - 1, 0)
    original_size = "" if width is None else (
        f"{p}  <original_size>\n"
        f"{p}    <width>{width}</width>\n"
        f"{p}    <height>{height}</height>\n"
        f"{p}  </original_size>\n"
        f"{p}  <source>{escape(name)}</source>\n"
    )
    return (
        f"{p}<task>\n"
        f"{p}  <id>{task_id}</id>\n"
        f"{p}  <name>{escape(name)}</name>\n"
        f"{p}  <size>{size}</size>\n"
        f"{p}  <mode>{mode}</mode>\n"
        f"{p}  <overlap>0</overlap>\n"
        f"{p}  <bugtracker></bugtracker>\n"
        f"{p}  <created>{now}</created>\n"
        f"{p}  <updated>{now}</updated>\n"
        f"{p}  <subset>{escape(subset)}</subset>\n"
        f"{p}  <start_frame>0</start_frame>\n"
        f"{p}  <stop_frame>{stop}</stop_frame>\n"
        f"{p}  <frame_filter></frame_filter>\n"
        f"{p}  <segments>\n"
        f"{p}    <segment>\n"
        f"{p}      <id>{task_id}</id>\n"
        f"{p}      <start>0</start>\n"
        f"{p}      <stop>{stop}</stop>\n"
        f"{p}      <url></url>\n"
        f"{p}    </segment>\n"
        f"{p}  </segments>\n"
        f"{p}  <owner>\n"
        f"{p}    <username></username>\n"
        f"{p}    <email></email>\n"
        f"{p}  </owner>\n"
        f"{p}  <assignee></assignee>\n"
        f"{original_size}"
        f"{p}</task>"
    )


def build_meta_project(*, project_id: int, name: str, task_blocks: list[str],
                       labels_xml: str, subsets: list[str] | None = None) -> str:
    """Emit the project-level CVAT <meta> block.

    The label set lives on the ``<project>`` (not on each ``<task>``).
    ``<subsets>`` is a newline-separated list; CVAT creates one task per subset
    on import, so it must name every task's subset.
    """
    now = _now()
    tasks = "\n".join(task_blocks)
    subsets_text = escape("\n".join(subsets or ["default"]))
    return f"""  <meta>
    <project>
      <id>{project_id}</id>
      <name>{escape(name)}</name>
      <bugtracker></bugtracker>
      <created>{now}</created>
      <updated>{now}</updated>
      <tasks>
{tasks}
      </tasks>
      <subsets>{subsets_text}</subsets>
      <owner>
        <username></username>
        <email></email>
      </owner>
      <assignee></assignee>
{labels_xml}
    </project>
    <dumped>{now}</dumped>
  </meta>"""


def build_meta_job(*, job_id: int, size: int, labels_xml: str, mode: str,
                   width: int | None = None, height: int | None = None) -> str:
    """Emit the job-level CVAT <meta> block.

    A job dump differs from a task dump in three ways, all reproduced here:
    there is no ``<name>``/``<source>`` (a job is a frame range, not a media
    file), and ``<original_size>`` sits *outside* ``<job>``, after ``<dumped>``.
    """
    now = _now()
    stop = max(size - 1, 0)
    original_size = "" if width is None else (
        f"    <original_size>\n"
        f"      <width>{width}</width>\n"
        f"      <height>{height}</height>\n"
        f"    </original_size>\n"
    )
    return f"""  <meta>
    <job>
      <id>{job_id}</id>
      <size>{size}</size>
      <mode>{mode}</mode>
      <overlap>0</overlap>
      <bugtracker></bugtracker>
      <created>{now}</created>
      <updated>{now}</updated>
      <subset>default</subset>
      <start_frame>0</start_frame>
      <stop_frame>{stop}</stop_frame>
      <frame_filter></frame_filter>
      <segments>
        <segment>
          <id>{job_id}</id>
          <start>0</start>
          <stop>{stop}</stop>
          <url></url>
        </segment>
      </segments>
      <owner>
        <username></username>
        <email></email>
      </owner>
      <assignee></assignee>
{labels_xml}
    </job>
    <dumped>{now}</dumped>
{original_size}  </meta>"""


def _document(meta: str, body: str) -> str:
    """Wrap a <meta> block and a body in the CVAT 1.1 <annotations> document."""
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<annotations>\n  <version>1.1</version>\n'
            f'{meta}\n{body}\n</annotations>\n')


def _clamp(v: float, hi: float) -> float:
    """Clamp a pixel coordinate into ``[0, hi]``."""
    return 0.0 if v < 0.0 else hi if v > hi else v


def _clip_polygon(pts: list[tuple[float, float]], w: float, h: float
                  ) -> list[tuple[float, float]]:
    """Sutherland–Hodgman clip of a *closed* polygon to the rect ``[0, w]×[0, h]``.

    Unlike per-vertex clamping (which pins each off-frame vertex onto the border
    and leaves a degenerate run of collinear points along the edge), this drops
    the vertices that fall outside the frame and inserts the exact points where
    the polygon's edges cross the boundary — yielding the true visible
    silhouette. Coordinates are in pixels.
    """
    def clip(poly, inside, intersect):
        out: list[tuple[float, float]] = []
        for i in range(len(poly)):
            a, b = poly[i - 1], poly[i]      # edge a→b (wraps: closed polygon)
            a_in, b_in = inside(a), inside(b)
            if b_in:
                if not a_in:
                    out.append(intersect(a, b))
                out.append(b)
            elif a_in:
                out.append(intersect(a, b))
        return out

    def at_x(a, b, x):
        t = (x - a[0]) / (b[0] - a[0])
        return (x, a[1] + (b[1] - a[1]) * t)

    def at_y(a, b, y):
        t = (y - a[1]) / (b[1] - a[1])
        return (a[0] + (b[0] - a[0]) * t, y)

    poly = pts
    for inside, intersect in (
        (lambda p: p[0] >= 0, lambda a, b: at_x(a, b, 0)),   # left
        (lambda p: p[0] <= w, lambda a, b: at_x(a, b, w)),   # right
        (lambda p: p[1] >= 0, lambda a, b: at_y(a, b, 0)),   # top
        (lambda p: p[1] <= h, lambda a, b: at_y(a, b, h)),   # bottom
    ):
        if not poly:
            break
        poly = clip(poly, inside, intersect)
    return poly


def bbox_to_cvat(points_norm: list[list[float]], angle: float,
                 w: int, h: int, *, clamp: bool = True) -> tuple[float, float, float, float, float]:
    """Normalised corner points + angle → (xtl, ytl, xbr, ybr, rotation°).

    The stored corners are the *unrotated* axis-aligned box (the frontend keeps
    rotation separate and rotates about the centre at render time), so min/max
    gives the box and ``rotation`` is the angle. IDAH stores the angle in
    radians; CVAT's ``rotation`` attribute is in degrees.

    With ``clamp`` (default) the box is clipped to the image bounds so no corner
    lands outside ``[0, w] × [0, h]`` — IDAH normalised points can drift outside
    ``[0, 1]``. Clamping is skipped for rotated boxes, where the stored corners
    are the *unrotated* AABB and clipping them would distort the rendered shape.
    """
    xs = [p[0] * w for p in points_norm]
    ys = [p[1] * h for p in points_norm]
    xtl, ytl, xbr, ybr = min(xs), min(ys), max(xs), max(ys)
    if clamp and not angle:
        xtl, xbr = _clamp(xtl, w), _clamp(xbr, w)
        ytl, ybr = _clamp(ytl, h), _clamp(ybr, h)
    return xtl, ytl, xbr, ybr, math.degrees(angle or 0.0)


def polygon_to_cvat(points_norm: list[list[float]], w: int, h: int, *,
                    clamp: bool = True, closed: bool = False) -> str:
    """Normalised points → CVAT 'x1,y1;x2,y2;…' absolute-pixel string.

    IDAH normalised points can drift outside ``[0, 1]``. With ``clamp`` (default):

    - ``closed`` (a polygon) is Sutherland–Hodgman *clipped* to the frame — the
      off-frame vertices are removed and boundary-crossing points inserted, so
      the result is the true visible silhouette (see :func:`_clip_polygon`).
    - an open path (a polyline) is *clamped* per-vertex instead: clipping an open
      path could split it into disjoint pieces, which a single CVAT polyline
      can't represent, so each point is pinned into ``[0, w] × [0, h]``.

    A polygon clipped away to nothing (fully off-frame) falls back to per-vertex
    clamping so we never emit empty geometry.
    """
    if not clamp:
        return ";".join(f"{p[0] * w:.2f},{p[1] * h:.2f}" for p in points_norm)
    if closed:
        clipped = _clip_polygon([(p[0] * w, p[1] * h) for p in points_norm], w, h)
        if len(clipped) >= 3:
            return ";".join(f"{x:.2f},{y:.2f}" for x, y in clipped)
        # Degenerate/empty clip: fall back to per-vertex clamp below.
    return ";".join(f"{_clamp(p[0] * w, w):.2f},{_clamp(p[1] * h, h):.2f}"
                    for p in points_norm)


def _shape_suffix(shape_type: str) -> str:
    """shape_type suffix after the modality prefix, e.g. 'bounding-box'."""
    return shape_type.split(":", 1)[-1]


#: IDAH ``occlusion`` values that mean the object is occluded.
OCCLUSION_VALUES = frozenset({"partial", "full"})


def is_occluded(annotation: dict) -> bool:
    """Whether an IDAH annotation is occluded — CVAT's ``occluded`` flag.

    IDAH stores this as ``annotation.attributes.occlusion``. It is a property of
    the *annotation*, i.e. of the whole track: the per-frame records carry only
    ``frame``/``points``/``angle``, so there is no per-frame occlusion to read
    and every shape of a track inherits the one value. (CVAT does allow it to
    vary per frame, so this is a real narrowing — but it is the only thing the
    source data supports.)

    The value is ``"Partial"``, ``"FULL"`` or empty, and is sometimes wrapped in
    a list (``["Partial"]``), so a list counts as occluded when *any* of its
    entries is — not just the first, which would miss ``["", "FULL"]``. Matching
    is case-insensitive and tolerates surrounding whitespace, since the casing
    is inconsistent in practice (``"FULL"`` alongside ``"Partial"``).

    Both degrees collapse to ``occluded="1"`` because CVAT's flag is a plain
    boolean; the Partial/FULL distinction has nowhere to go. Fully-occluded is
    deliberately *not* mapped to ``outside="1"``: outside means the object is
    absent from the frame, whereas a FULL-occluded IDAH annotation still carries
    the box the annotator drew. CVAT's own exports agree — in the reference dump
    ``outside="1"`` appears only as a track's final terminator.
    """
    attributes = (annotation or {}).get("attributes") or {}
    value = attributes.get("occlusion", "") if isinstance(attributes, dict) else ""
    values = value if isinstance(value, list) else [value]
    return any(str(v).strip().lower() in OCCLUSION_VALUES for v in values)


COORD_DP = 2   # decimal places the CVAT XML carries for box coordinates


def _box_xml(frame: int, box: tuple, *, keyframe: int, outside: int,
             occluded: int = 0) -> str:
    xtl, ytl, xbr, ybr, rot = box
    rot_attr = f' rotation="{rot:.2f}"' if rot else ""
    return (f'    <box frame="{frame}" keyframe="{keyframe}" outside="{outside}" '
            f'occluded="{occluded}" xtl="{xtl:.2f}" ytl="{ytl:.2f}" '
            f'xbr="{xbr:.2f}" ybr="{ybr:.2f}"{rot_attr} z_order="0">\n    </box>')


def _bbox_track_shapes(seq: list, w: int, h: int, *, clamp: bool) -> list:
    """Per-frame ``(keyframe, box)`` for a bbox track, reproducing CVAT exactly.

    ``seq`` is the materialised :func:`interpolation.iter_frames` output. Anchor
    frames — the original IDAH keyframes, plus frame 0 so the track starts on
    time — are rendered from the source geometry and rounded to the precision
    the XML actually carries. Every in-between is then the linear blend of its
    *bracketing rounded anchors*, rounded the same way.

    That is exactly what CVAT recomputes: on import it discards the
    ``keyframe="0"`` shapes and keeps only the anchors *at the precision we
    wrote*, then re-interpolates between them on export. Deriving in-betweens
    from the emitted anchors rather than from full-precision source geometry
    removes the double-rounding (``round(lerp(exact))`` vs
    ``round(lerp(round(exact)))``, worth up to 1 unit in the last decimal) and
    also the clamping non-linearity, since anchors are clamped *before* the
    blend and ``clamp(lerp(a, b)) != lerp(clamp(a), clamp(b))``. The result is a
    file CVAT returns byte-identical.
    """
    rnd = lambda v: round(v, COORD_DP)
    anchors = [i for i, (_f, _p, _a, is_kf) in enumerate(seq) if is_kf or i == 0]
    boxes = {i: tuple(rnd(v) for v in
                      bbox_to_cvat(seq[i][1], seq[i][2], w, h, clamp=clamp))
             for i in anchors}

    out: list = [None] * len(seq)
    for i in anchors:
        out[i] = (1, boxes[i])
    for lo, hi in zip(anchors, anchors[1:]):
        f0, f1 = seq[lo][0], seq[hi][0]
        a, b = boxes[lo], boxes[hi]
        for i in range(lo + 1, hi):
            t = (seq[i][0] - f0) / (f1 - f0)
            out[i] = (0, tuple(rnd(a[k] + (b[k] - a[k]) * t) for k in range(len(a))))
    for i in range(anchors[-1] + 1, len(seq)):   # trailing frames hold the last anchor
        out[i] = (0, boxes[anchors[-1]])
    return out


def write_video_body(annotations: list, w: int, h: int, n_frames: int, *,
                     clamp: bool = True, track_id_start: int = 0,
                     extra_attrs: str = "") -> str:
    """Build the <track> body for one video from its annotations.

    Every frame in each track's ``[start, end]`` is materialised using the
    interpolation helper (bbox = linear, polygon = flubber), rather than
    emitting only keyframes and relying on CVAT's own interpolation.

    The ``keyframe=`` flag then differs per shape type, because CVAT stores only
    keyframe shapes — on import it discards ``keyframe="0"`` frames and
    re-interpolates linearly between the real keyframes:

    - **polygon** — every emitted frame is ``keyframe="1"``. CVAT's polygon
      interpolation is not flubber, so anything it re-derives would not match
      the frontend; flagging each frame is what makes it keep our geometry.
    - **bounding-box** — only the original IDAH keyframes are ``keyframe="1"``;
      the materialised in-betweens are ``keyframe="0"``. Both sides interpolate
      boxes linearly, so CVAT rebuilds byte-identical geometry from the
      keyframes alone, and the track stays cheap to edit in the UI.

    The first emitted shape of a track is always a keyframe regardless: a
    leading ``keyframe="0"`` would be discarded and the track would start late.

    Bbox in-betweens are computed from the *emitted, rounded* keyframes rather
    than from full-precision source geometry, so importing and re-exporting the
    file through CVAT returns it unchanged — see :func:`_bbox_track_shapes`.

    The track ``label=`` is the annotation ``category`` (the IDAH tree-path
    *id*, e.g. ``"vehicles/truck"``), used verbatim so it matches the ``id``-
    keyed ``<labels>`` block — CVAT rejects tracks whose label is not declared.

    ``track_id_start`` and ``extra_attrs`` exist for the project level, where
    track ids must stay unique across every task in the project and each track
    carries the ``task_id=``/``subset=`` naming the task it belongs to.
    """
    blocks: list[str] = []
    track_id = track_id_start

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
        end = sa.get("end", frames[-1]["frame"])
        # IDAH occlusion is a property of the annotation, so it holds for every
        # frame of the track — including the terminator (see is_occluded).
        occluded = int(is_occluded(ann.annotation))

        seq = list(interp.iter_frames(sa, kind=suffix))
        if not seq:
            continue

        shapes: list[str] = []
        if suffix == interp.BBOX:
            # Only the real keyframes are keyframe="1"; the in-betweens are
            # derived from those emitted anchors so CVAT rebuilds them exactly.
            emitted = _bbox_track_shapes(seq, w, h, clamp=clamp)
            for (frame, *_), (keyframe, box) in zip(seq, emitted):
                shapes.append(_box_xml(frame, box, keyframe=keyframe,
                                       outside=0, occluded=occluded))
            tail = emitted[-1][1]
        else:
            # Polygons keep every frame a keyframe: CVAT's polygon interpolation
            # is not flubber, so anything it re-derived would not match.
            for frame, points, angle, _is_kf in seq:
                shapes.append(_frame_shape(suffix, frame, points, w, h,
                                           keyframe=1, outside=0, angle=angle,
                                           clamp=clamp, occluded=occluded))
            tail = (seq[-1][1], seq[-1][2])

        # Terminate the track with an outside="1" shape one frame past the end,
        # unless the track already runs to the last video frame. CVAT marks the
        # terminating outside shape as a keyframe (keyframe="1").
        if end + 1 <= n_frames - 1:
            if suffix == interp.BBOX:
                shapes.append(_box_xml(end + 1, tail, keyframe=1, outside=1,
                                       occluded=occluded))
            else:
                shapes.append(_frame_shape(suffix, end + 1, tail[0], w, h,
                                           keyframe=1, outside=1, angle=tail[1],
                                           clamp=clamp, occluded=occluded))

        blocks.append(
            f'  <track id="{track_id}" label={quoteattr(label)} '
            f'source="manual"{extra_attrs}>\n'
            + "\n".join(shapes)
            + "\n  </track>"
        )
        track_id += 1

    return "\n".join(blocks)


def _frame_shape(suffix: str, frame: int, points: list, w: int, h: int, *,
                 keyframe: int, outside: int, angle: float = 0.0,
                 clamp: bool = True, occluded: int = 0) -> str:
    common = (f'frame="{frame}" keyframe="{keyframe}" outside="{outside}" '
              f'occluded="{occluded}"')

    if suffix == interp.BBOX:
        xtl, ytl, xbr, ybr, rot = bbox_to_cvat(points, angle, w, h, clamp=clamp)
        rot_attr = f' rotation="{rot:.2f}"' if rot else ""
        return (f'    <box {common} '
                f'xtl="{xtl:.2f}" ytl="{ytl:.2f}" xbr="{xbr:.2f}" ybr="{ybr:.2f}"'
                f'{rot_attr} z_order="0">\n    </box>')

    # polygon
    pts = polygon_to_cvat(points, w, h, clamp=clamp, closed=True)
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


def _image_shape(suffix: str, shape_args: dict, w: int, h: int, label: str, *,
                 clamp: bool = True, occluded: int = 0) -> str | None:
    """One CVAT image-format shape element, or None for unsupported types."""
    common = f'label={quoteattr(label)} source="manual" occluded="{occluded}"'
    points = shape_args.get("points", [])

    if suffix == "bounding-box":
        xtl, ytl, xbr, ybr, rot = bbox_to_cvat(points, shape_args.get("angle", 0), w, h, clamp=clamp)
        rot_attr = f' rotation="{rot:.2f}"' if rot else ""
        return (f'    <box {common} '
                f'xtl="{xtl:.2f}" ytl="{ytl:.2f}" xbr="{xbr:.2f}" ybr="{ybr:.2f}"'
                f'{rot_attr} z_order="0"></box>')

    if suffix == "polygon":
        return f'    <polygon {common} points="{polygon_to_cvat(points, w, h, clamp=clamp, closed=True)}" z_order="0"></polygon>'

    if suffix == "line":   # IDAH line → CVAT polyline (CVAT has no "line")
        return f'    <polyline {common} points="{polygon_to_cvat(points, w, h, clamp=clamp)}" z_order="0"></polyline>'

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


def write_image_body(annotations: list, w: int, h: int, *, clamp: bool = True) -> str:
    """Shape elements for one image (CVAT image format).

    The shape ``label=`` is the annotation ``category`` (the IDAH tree-path
    *id*) verbatim, matching the ``id``-keyed ``<labels>`` block, and
    ``occluded=`` comes from the IDAH occlusion attribute (see
    :func:`is_occluded`).
    """
    out: list[str] = []
    for ann in annotations:
        suffix = _shape_suffix(ann.shape_type)
        label = ann.annotation.get("category", "")
        el = _image_shape(suffix, ann.shape_args, w, h, label, clamp=clamp,
                          occluded=int(is_occluded(ann.annotation)))
        if el is not None:
            out.append(el)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Export levels
# ---------------------------------------------------------------------------

PROJECT, TASK, JOB = "project", "task", "job"

#: How IDAH maps onto the three CVAT export levels.
#:
#: IDAH has two levels (dataset → entry) and CVAT has three
#: (project → task → job), so the mapping is modality-dependent:
#:
#: - ``idah-video``: dataset = project, entry = task, and one job per task
#:   (an entry is a single video, which CVAT segments into exactly one job).
#: - ``idah-image``: dataset = project *and* its single task, entry = image;
#:   a job is then a frame range over that task.
#:
#: The level follows the filter — the deeper you filter, the lower the level:
#:
#: ===================  ========  ================================================
#: filter               level     output
#: ===================  ========  ================================================
#: (none)               project   one XML per dataset, all entries merged
#: ``--dataset-id``     task      one XML per entry (video) / per dataset (image)
#: ``--entry-id``       job       one XML for that entry
#: ===================  ========  ================================================


def level_for(dataset_id: str | None, entry_id: str | None) -> str:
    """The export level implied by the active filters (deepest filter wins)."""
    if entry_id:
        return JOB
    if dataset_id:
        return TASK
    return PROJECT


# ---------------------------------------------------------------------------
# Export driver
# ---------------------------------------------------------------------------

@contextmanager
def _entry_media(upd, entry):
    """Yield a filesystem path to an entry's media blob, or None if missing.

    The blob lives inside the UPD file, but PyAV needs a real path, so it is
    spilled to a temp file for the duration of the block.
    """
    media = upd.medias.get(entry.local_media_id)
    if media is None or media.blob_data is None:
        print(f"  ! entry {entry.id}: media missing, skipping")
        yield None
        return
    suffix = Path(entry.local_media_id).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(media.blob_data)
        tmp.flush()
        yield tmp.name


def _entry_name(entry) -> str:
    return entry.metadata.get("Name") or entry.local_media_id


def _entry_folder(entry) -> str:
    """Folder name for one entry.

    Several entries can share the same human Name (same source video); the media
    id is unique, so fold it in to keep the folders distinct.
    """
    return (f"{_safe_name(Path(_entry_name(entry)).stem)}"
            f"_{Path(entry.local_media_id).stem}")


def export_video_entry(upd, ds, entry, out_dir: Path, *, task_id: int,
                       with_images: bool, clamp: bool = True,
                       level: str = TASK) -> None:
    """Export one idah-video entry as a task-level or job-level CVAT package.

    Both levels describe the same single video with the same 0-based frame
    numbering and the same ``<track>`` body; only the ``<meta>`` block differs
    (see :func:`build_meta` and :func:`build_meta_job`).
    """
    with _entry_media(upd, entry) as media_path:
        if media_path is None:
            return

        name = _entry_name(entry)
        width, height, n_frames = probe_video(media_path)

        labels_xml = build_labels(ds.metadata.get("Labeling-Configuration", {}))
        if level == JOB:
            meta = build_meta_job(job_id=task_id, size=n_frames,
                                  labels_xml=labels_xml, mode="interpolation",
                                  width=width, height=height)
        else:
            meta = build_meta(task_id=task_id, name=name, size=n_frames,
                              labels_xml=labels_xml, width=width, height=height,
                              source=name)

        body = write_video_body(upd.annotations.for_entry(entry.id),
                                width, height, n_frames, clamp=clamp)

        folder = _entry_folder(entry)
        if level == JOB:
            folder = f"job_{folder}"
        task_dir = out_dir / _safe_name(ds.name) / folder
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "annotations.xml").write_text(_document(meta, body),
                                                  encoding="utf-8")

        n_tracks = body.count("<track ")
        print(f"  [{name}] {width}x{height}, {n_frames} frames, {n_tracks} tracks")

        if with_images:
            extract_frames(media_path, task_dir / "images", total=n_frames)


def export_video_project(upd, ds, entries: list, out_dir: Path, *,
                         project_id: int, with_images: bool,
                         clamp: bool = True) -> None:
    """Export a whole idah-video dataset as one project-level CVAT package.

    Every entry becomes a ``<task>`` inside ``<meta><project><tasks>`` *and* its
    own **subset**, because a subset is the only thing CVAT reconstitutes a task
    from on import. The ``<tasks>`` list is descriptive metadata that the
    importer ignores, so a project whose tasks all sit in one subset (which is
    what CVAT's own project dump emits) collapses back into a single task —
    giving each entry a distinct subset is what makes the round trip preserve
    all N tasks.

    Frames are therefore numbered *per task* (each subset is an independent task
    that starts at frame 0) and each task's frames live in their own
    ``images/<subset>/`` folder. Track ids stay unique across the whole project,
    which also makes them unique within every task.
    """
    labels_xml = build_labels(ds.metadata.get("Labeling-Configuration", {}))
    project_dir = out_dir / f"project_{_safe_name(ds.name)}"

    task_blocks: list[str] = []
    bodies: list[str] = []
    subsets: list[str] = []
    task_id = track_id = total_frames = 0

    for entry in entries:
        with _entry_media(upd, entry) as media_path:
            if media_path is None:
                continue

            name = _entry_name(entry)
            # The subset doubles as a directory name and must be unique: several
            # entries can share the same human Name, so disambiguate on collision.
            subset = _safe_name(Path(name).stem)
            if subset in subsets:
                subset = _entry_folder(entry)
            subsets.append(subset)

            width, height, n_frames = probe_video(media_path)

            task_blocks.append(_task_block(
                task_id=task_id, name=name, size=n_frames, mode="interpolation",
                subset=subset, width=width, height=height,
            ))
            body = write_video_body(
                upd.annotations.for_entry(entry.id), width, height, n_frames,
                clamp=clamp, track_id_start=track_id,
                extra_attrs=f' task_id="{task_id}" subset={quoteattr(subset)}',
            )
            n_tracks = body.count("<track ")
            if body:
                bodies.append(body)

            print(f"  [{name}] {width}x{height}, {n_frames} frames, "
                  f"{n_tracks} tracks  → subset {subset!r}")

            if with_images:
                extract_frames(media_path, project_dir / "images" / subset,
                               total=n_frames)

            task_id += 1
            track_id += n_tracks
            total_frames += n_frames

    meta = build_meta_project(project_id=project_id, name=ds.name,
                              task_blocks=task_blocks, labels_xml=labels_xml,
                              subsets=subsets)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "annotations.xml").write_text(
        _document(meta, "\n".join(bodies)), encoding="utf-8")
    print(f"  {task_id} tasks / subsets, {total_frames} frames, {track_id} tracks")


def export_image_dataset(upd, ds, out_dir: Path, *, task_id: int,
                         with_images: bool, clamp: bool = True,
                         level: str = TASK, entries: list | None = None) -> None:
    """Export an idah-image dataset to a CVAT 'for images 1.1' package.

    An image dataset is a *single* CVAT task whatever the level — all entries
    become ``<image>`` elements of one ``annotations.xml`` — so the three levels
    differ only in the ``<meta>`` wrapper (and, at project level, in the
    ``images/default/`` layout and the ``task_id=``/``subset=`` on each image).
    ``entries`` narrows which entries are included (job level passes one).
    """
    labels_xml = build_labels(ds.metadata.get("Labeling-Configuration", {}))

    if entries is None:
        entries = [e for e in upd.entries.for_dataset(ds.id) if e.is_local]

    if level == PROJECT:
        task_dir = out_dir / f"project_{_safe_name(ds.name)}"
        images_dir = task_dir / "images" / "default"
        image_attrs = f' task_id="{task_id}" subset="default"'
    elif level == JOB:
        task_dir = out_dir / _safe_name(ds.name) / f"job_{_entry_folder(entries[0])}"
        images_dir = task_dir / "images"
        image_attrs = ""
    else:
        task_dir = out_dir / _safe_name(ds.name)
        images_dir = task_dir / "images"
        image_attrs = ""

    blocks: list[str] = []
    seen_names: set[str] = set()
    n_shapes = 0

    for img_id, entry in enumerate(entries):
        media = upd.medias.get(entry.local_media_id)
        if media is None or media.blob_data is None:
            print(f"  ! entry {entry.id}: media missing, skipping")
            continue

        name = _entry_name(entry)
        if name in seen_names:                       # keep image names unique
            name = f"{Path(name).stem}_{Path(entry.local_media_id).stem}{Path(name).suffix}"
        seen_names.add(name)

        suffix = Path(entry.local_media_id).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(media.blob_data)
            tmp.flush()
            width, height = probe_image(tmp.name)

        annotations = upd.annotations.for_entry(entry.id)
        body = write_image_body(annotations, width, height, clamp=clamp)
        n_shapes += body.count("<")
        open_tag = (f'  <image id="{img_id}" name="{escape(name)}" '
                    f'width="{width}" height="{height}"{image_attrs}>')
        blocks.append(f'{open_tag}\n{body}\n  </image>' if body
                      else f'{open_tag}</image>')

        if with_images:
            images_dir.mkdir(parents=True, exist_ok=True)
            (images_dir / name).write_bytes(media.blob_data)

    if level == PROJECT:
        meta = build_meta_project(
            project_id=task_id, name=ds.name, labels_xml=labels_xml,
            task_blocks=[_task_block(task_id=task_id, name=ds.name,
                                     size=len(blocks), mode="annotation")],
        )
    elif level == JOB:
        meta = build_meta_job(job_id=task_id, size=len(blocks),
                              labels_xml=labels_xml, mode="annotation")
    else:
        meta = build_meta_images(task_id=task_id, name=ds.name,
                                 size=len(blocks), labels_xml=labels_xml)

    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "annotations.xml").write_text(
        _document(meta, "\n".join(blocks)), encoding="utf-8")
    msg = f"  {len(blocks)} images, {n_shapes} shapes"
    if with_images:
        msg += ", images copied"
    print(msg)


def run(upd_path: str, output: str, *, with_images: bool = False,
        dataset_id: str | None = None, entry_id: str | None = None,
        clamp: bool = True) -> None:
    """Export the datasets in ``upd_path`` to CVAT packages under ``output``.

    The *level* of the export follows the filters — see :func:`level_for`. With
    no filter every dataset is dumped as a CVAT project; ``dataset_id`` narrows
    to one dataset and drops to task level; ``entry_id`` narrows to one entry
    and drops to job level.

    ``clamp`` (default) clips every shape to the image/frame bounds so no point
    lands outside the media — IDAH normalised points can drift outside
    ``[0, 1]``. Disable it to preserve the raw out-of-bounds coordinates.
    """
    out_dir = Path(output)
    level = level_for(dataset_id, entry_id)

    with UPD.open(upd_path, read_only=True) as upd:
        datasets = upd.datasets.all()
        if dataset_id:
            datasets = [d for d in datasets if d.id == dataset_id]
            if not datasets:
                raise SystemExit(f"no dataset with id {dataset_id!r}")

        # CVAT project/task/job <id>s must be integers; IDAH ids are UUIDv7
        # strings. CVAT reassigns its own ids on import, so a sequential counter
        # per exported unit is sufficient (the IDAH identity is preserved in the
        # name / folder name).
        unit_id = 0
        exported = 0

        for ds in datasets:
            entries = [e for e in upd.entries.for_dataset(ds.id) if e.is_local]
            if entry_id:
                entries = [e for e in entries if e.id == entry_id]
                if not entries:
                    continue

            if ds.modality not in ("idah-image", "idah-video"):
                print(f"  ! unsupported modality {ds.modality!r}, skipping")
                continue

            print(f"Dataset {ds.name!r} (modality={ds.modality}, level={level})")
            exported += 1

            if ds.modality == "idah-image":
                export_image_dataset(upd, ds, out_dir, task_id=unit_id,
                                     with_images=with_images, clamp=clamp,
                                     level=level, entries=entries)
                unit_id += 1
                continue

            if level == PROJECT:
                export_video_project(upd, ds, entries, out_dir,
                                     project_id=unit_id,
                                     with_images=with_images, clamp=clamp)
                unit_id += 1
                continue

            for entry in entries:
                export_video_entry(upd, ds, entry, out_dir, task_id=unit_id,
                                   with_images=with_images, clamp=clamp,
                                   level=level)
                unit_id += 1

        if entry_id and not exported:
            raise SystemExit(f"no entry with id {entry_id!r}")

    print(f"\nWritten: {out_dir}")
