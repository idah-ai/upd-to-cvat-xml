"""Per-frame interpolation for IDAH track annotations.

IDAH stores only sparse keyframes in ``shape_args["frames"]`` (each with a
``frame`` index and normalised ``points``). This module fills in the shape at
any intermediate frame:

- **bounding-box** — linear interpolation of the 4 corners between the two
  bracketing keyframes. For an axis-aligned box this is identical to the
  frontend's ``interpolateBBox`` (which lerps an ``[x0,y0,x1,y1]`` AABB); here
  the box is stored as 4 corner points, so we lerp each corner instead.
- **polygon** — flubber morph via :mod:`pyflubber`, the same algorithm the
  frontend uses, so server- and client-side interpolation agree. pyflubber
  resamples and point-matches rings of differing vertex counts before
  interpolating (our keyframes vary, e.g. 63–67 points). It only adds the
  minimum vertices needed to equalise the two rings (output length =
  ``max(len(a), len(b))``, no segment subdivision) — matching the frontend's
  ``flubber.interpolate(..., { maxSegmentLength: Infinity })``.
  pyflubber is an independent reimplementation, so morphs are visually
  equivalent but not guaranteed vertex-identical to flubber.js.

All coordinates stay in whatever space the keyframes use (IDAH = normalised
``[0, 1]``); denormalisation to pixels happens later in the CVAT writer.

Two entry points (mirroring the frontend's ``getInterpolatedFrame``, which
returns ``{points, angle}`` — angle is linearly interpolated for bboxes, 0 for
polygons):

    shape_at(shape_args, frame, kind=...)   -> (points, angle) | None
    iter_frames(shape_args, kind=...)       -> Iterator[(frame, points, angle)]
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Iterator, Optional

import numpy as np

# ---------------------------------------------------------------------------
# pyflubber compatibility shim
#
# pyflubber.closed.get_area() calls np.cross() on 2-D vectors, which numpy 2.0
# removed — and Python >= 3.14 forces numpy >= 2. We override that single
# function with a 2-D-safe equivalent (the signed-area cross-product term)
# rather than monkeypatching numpy globally.
# ---------------------------------------------------------------------------
import pyflubber.closed as _fl_closed


def _get_area_2d(line: np.ndarray) -> float:
    line = np.insert(line, 0, line[-1], axis=0)
    a, b = line[1:], line[:-1]
    return float((a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]).sum() / 2)


_fl_closed.get_area = _get_area_2d

from pyflubber import interpolator as _flubber_interpolator  # noqa: E402


BBOX = "bounding-box"
POLYGON = "polygon"


def kind_of(shape_type: str) -> str:
    """`'idah-video:polygon'` -> `'polygon'`."""
    return shape_type.split(":", 1)[-1]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _frame_numbers(shape_args: dict) -> list[int]:
    return [f["frame"] for f in shape_args["frames"]]


def _bounds(shape_args: dict, nums: list[int]) -> tuple[int, int]:
    start = shape_args.get("start", nums[0])
    end = shape_args.get("end", nums[-1])
    return start, end


def _lerp_points(p0: np.ndarray, p1: np.ndarray, t: float) -> list[list[float]]:
    return (p0 + (p1 - p0) * t).tolist()


def _morph(p0: np.ndarray, p1: np.ndarray):
    """A flubber interpolator t -> ring, reusable across a keyframe segment."""
    return _flubber_interpolator(p0, p1, closed=True)


def _raw(frame_kf: dict) -> list[list[float]]:
    return [list(p) for p in frame_kf["points"]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _angle(frame_kf: dict) -> float:
    return float(frame_kf.get("angle", 0) or 0.0)


def shape_at(shape_args: dict, frame: int, *, kind: str) -> Optional[tuple[list[list[float]], float]]:
    """Interpolated ``(points, angle)`` at ``frame`` (normalised points).

    Mirrors the frontend's ``getInterpolatedFrame`` — angle is linearly
    interpolated for bboxes and 0 for polygons. Returns ``None`` when ``frame``
    is outside ``[start, end]``. Exact keyframes return their original
    (un-resampled) points; before the first / after the last keyframe the edge
    keyframe is held.
    """
    frames = shape_args["frames"]
    nums = _frame_numbers(shape_args)
    start, end = _bounds(shape_args, nums)
    if frame < start or frame > end:
        return None

    if frame <= nums[0]:
        return _raw(frames[0]), _angle(frames[0])
    if frame >= nums[-1]:
        return _raw(frames[-1]), _angle(frames[-1])

    i1 = bisect_right(nums, frame)        # first keyframe strictly after `frame`
    i0 = i1 - 1
    f0, f1 = nums[i0], nums[i1]
    if frame == f0:
        return _raw(frames[i0]), _angle(frames[i0])

    t = (frame - f0) / (f1 - f0)
    p0 = np.asarray(frames[i0]["points"], dtype=float)
    p1 = np.asarray(frames[i1]["points"], dtype=float)
    if kind == BBOX:
        angle = _angle(frames[i0]) + (_angle(frames[i1]) - _angle(frames[i0])) * t
        return _lerp_points(p0, p1, t), angle
    if kind == POLYGON:
        return np.asarray(_morph(p0, p1)(t)).tolist(), 0.0
    raise ValueError(f"unsupported kind {kind!r}")


def iter_frames(shape_args: dict, *, kind: str) -> Iterator[tuple[int, list[list[float]], float]]:
    """Yield ``(frame, points, angle)`` for every integer frame in ``[start, end]``.

    One flubber interpolator is built per keyframe segment and reused across
    the frames inside it, so polygon morphing stays cheap. Angle is linearly
    interpolated for bboxes (0 for polygons), matching the frontend.
    """
    frames = shape_args["frames"]
    nums = _frame_numbers(shape_args)
    start, end = _bounds(shape_args, nums)

    # Pre-roll: frames before the first keyframe hold the first keyframe.
    for f in range(start, nums[0]):
        yield f, _raw(frames[0]), _angle(frames[0])

    # Each segment [nums[i], nums[i+1]) — emit the left keyframe raw, then morph.
    for i in range(len(nums) - 1):
        f0, f1 = nums[i], nums[i + 1]
        a0, a1 = _angle(frames[i]), _angle(frames[i + 1])
        p0 = np.asarray(frames[i]["points"], dtype=float)
        p1 = np.asarray(frames[i + 1]["points"], dtype=float)
        morph = _morph(p0, p1) if kind == POLYGON else None
        for f in range(f0, f1):
            if f < start:
                continue
            if f == f0:
                yield f, _raw(frames[i]), a0
                continue
            t = (f - f0) / (f1 - f0)
            if kind == BBOX:
                yield f, _lerp_points(p0, p1, t), a0 + (a1 - a0) * t
            elif kind == POLYGON:
                yield f, np.asarray(morph(t)).tolist(), 0.0
            else:
                raise ValueError(f"unsupported kind {kind!r}")

    # Last keyframe and any post-roll up to `end`.
    for f in range(nums[-1], end + 1):
        yield f, _raw(frames[-1]), _angle(frames[-1])
