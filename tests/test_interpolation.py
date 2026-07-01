"""Specs for :mod:`upd_to_cvat.interpolation`.

These cover the pure per-frame interpolation logic: keyframe bracketing, edge
holding, linear bbox lerp, angle interpolation and the polygon flubber morph.
No media / SDK is touched, so everything here is deterministic.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from upd_to_cvat import interpolation as interp


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def bbox_args(*frames, start=None, end=None):
    """Build a shape_args dict from (frame, points) pairs."""
    sa = {"frames": [{"frame": f, "points": p} for f, p in frames]}
    if start is not None:
        sa["start"] = start
    if end is not None:
        sa["end"] = end
    return sa


# ---------------------------------------------------------------------------
# kind_of
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape_type,expected", [
    ("idah-video:polygon", "polygon"),
    ("idah-video:bounding-box", "bounding-box"),
    ("idah-image:ellipse", "ellipse"),
    ("polygon", "polygon"),            # no prefix → returned verbatim
])
def test_kind_of_strips_modality_prefix(shape_type, expected):
    assert interp.kind_of(shape_type) == expected


# ---------------------------------------------------------------------------
# shape_at — bounds and edge holding
# ---------------------------------------------------------------------------

def test_shape_at_returns_none_outside_bounds():
    sa = bbox_args((0, [[0, 0], [1, 1]]), (10, [[0, 0], [2, 2]]))
    assert interp.shape_at(sa, -1, kind=interp.BBOX) is None
    assert interp.shape_at(sa, 11, kind=interp.BBOX) is None


def test_shape_at_holds_first_and_last_keyframe():
    sa = bbox_args((2, [[0, 0], [1, 1]]), (8, [[0, 0], [2, 2]]))
    pts_lo, _ = interp.shape_at(sa, 2, kind=interp.BBOX)
    pts_hi, _ = interp.shape_at(sa, 8, kind=interp.BBOX)
    assert pts_lo == [[0, 0], [1, 1]]
    assert pts_hi == [[0, 0], [2, 2]]


def test_shape_at_respects_explicit_start_end_padding():
    # start/end widen the valid range beyond the keyframes; the edge keyframe
    # is held across the padding.
    sa = bbox_args((2, [[0, 0], [1, 1]]), (8, [[0, 0], [2, 2]]), start=0, end=10)
    assert interp.shape_at(sa, 0, kind=interp.BBOX)[0] == [[0, 0], [1, 1]]
    assert interp.shape_at(sa, 10, kind=interp.BBOX)[0] == [[0, 0], [2, 2]]
    assert interp.shape_at(sa, -1, kind=interp.BBOX) is None
    assert interp.shape_at(sa, 11, kind=interp.BBOX) is None


def test_shape_at_exact_interior_keyframe_returns_raw_points():
    sa = bbox_args(
        (0, [[0, 0], [1, 1]]),
        (5, [[9, 9], [9, 9]]),          # a deliberately "off" middle keyframe
        (10, [[0, 0], [2, 2]]),
    )
    pts, _ = interp.shape_at(sa, 5, kind=interp.BBOX)
    assert pts == [[9, 9], [9, 9]]


# ---------------------------------------------------------------------------
# shape_at — bbox linear interpolation + angle
# ---------------------------------------------------------------------------

def test_shape_at_bbox_lerps_corners_midway():
    sa = bbox_args((0, [[0.0, 0.0], [1.0, 1.0]]),
                   (10, [[0.0, 0.0], [2.0, 2.0]]))
    pts, angle = interp.shape_at(sa, 5, kind=interp.BBOX)
    assert pts == [[0.0, 0.0], [1.5, 1.5]]
    assert angle == 0.0


def test_shape_at_bbox_interpolates_angle_linearly():
    sa = bbox_args(
        (0, [[0, 0], [1, 1]], ),
        (10, [[0, 0], [1, 1]]),
    )
    # inject angles onto the keyframes
    sa["frames"][0]["angle"] = 0.0
    sa["frames"][1]["angle"] = 1.0
    _, angle = interp.shape_at(sa, 3, kind=interp.BBOX)
    assert angle == pytest.approx(0.3)


def test_shape_at_rejects_unknown_kind():
    sa = bbox_args((0, [[0, 0], [1, 1]]), (10, [[0, 0], [2, 2]]))
    with pytest.raises(ValueError):
        interp.shape_at(sa, 5, kind="mask")


# ---------------------------------------------------------------------------
# shape_at — polygon morph
# ---------------------------------------------------------------------------

def _triangle(scale):
    return [[0.0, 0.0], [scale, 0.0], [0.0, scale]]


def test_shape_at_polygon_identity_morph_is_stable():
    # Morphing a ring into an identical ring should return that ring (up to a
    # possible vertex re-ordering) and angle 0.
    tri = _triangle(1.0)
    sa = bbox_args((0, tri), (10, tri))
    pts, angle = interp.shape_at(sa, 5, kind=interp.POLYGON)
    assert angle == 0.0
    got = np.asarray(pts)
    exp = np.asarray(tri)
    # same set of vertices, order-independent
    assert sorted(map(tuple, np.round(got, 6))) == sorted(map(tuple, exp))


def test_shape_at_polygon_midpoint_between_scaled_triangles():
    sa = bbox_args((0, _triangle(1.0)), (10, _triangle(3.0)))
    pts, _ = interp.shape_at(sa, 5, kind=interp.POLYGON)
    arr = np.asarray(pts)
    # every vertex lies within the bounding region of the two keyframes
    assert arr.min() >= -1e-6
    assert arr.max() <= 3.0 + 1e-6
    # output length equals max ring size (both are 3 here)
    assert len(pts) == 3


# ---------------------------------------------------------------------------
# iter_frames
# ---------------------------------------------------------------------------

def test_iter_frames_covers_every_integer_frame_inclusive():
    sa = bbox_args((0, [[0, 0], [1, 1]]), (4, [[0, 0], [2, 2]]))
    frames = [f for f, _, _ in interp.iter_frames(sa, kind=interp.BBOX)]
    assert frames == [0, 1, 2, 3, 4]


def test_iter_frames_matches_shape_at_pointwise():
    sa = bbox_args((0, [[0.0, 0.0], [1.0, 1.0]]),
                   (8, [[0.0, 0.0], [3.0, 3.0]]))
    for frame, points, angle in interp.iter_frames(sa, kind=interp.BBOX):
        exp_pts, exp_angle = interp.shape_at(sa, frame, kind=interp.BBOX)
        assert np.allclose(points, exp_pts)
        assert angle == pytest.approx(exp_angle)


def test_iter_frames_pre_and_post_roll_hold_edge_keyframes():
    sa = bbox_args((3, [[0, 0], [1, 1]]), (6, [[0, 0], [2, 2]]),
                   start=1, end=8)
    out = list(interp.iter_frames(sa, kind=interp.BBOX))
    frames = [f for f, _, _ in out]
    assert frames == [1, 2, 3, 4, 5, 6, 7, 8]
    # pre-roll holds the first keyframe
    assert out[0][1] == [[0, 0], [1, 1]]
    # post-roll holds the last keyframe
    assert out[-1][1] == [[0, 0], [2, 2]]


def test_iter_frames_reuses_edge_points_object_identity_not_required():
    # sanity: single-keyframe track yields exactly that one frame
    sa = bbox_args((5, [[0, 0], [1, 1]]))
    out = list(interp.iter_frames(sa, kind=interp.BBOX))
    assert [f for f, _, _ in out] == [5]


# ---------------------------------------------------------------------------
# _get_area_2d shim (numpy 2.x compatibility)
# ---------------------------------------------------------------------------

def test_get_area_2d_signed_area_of_unit_square():
    # CCW unit square → area 1.0 (sign depends on winding).
    square = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    assert abs(interp._get_area_2d(square)) == pytest.approx(1.0)
