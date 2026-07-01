"""Specs for the pure builders in :mod:`upd_to_cvat.converter`.

Covers name sanitising, coordinate clamping / polygon clipping, the bbox and
polygon → CVAT converters, the label / meta / shape XML builders and the video
/ image body writers. Media probing and frame extraction (which need PyAV and
real blobs) are intentionally out of scope here.
"""

from __future__ import annotations

import math
import re
from types import SimpleNamespace

import pytest

from upd_to_cvat import converter as c
from upd_to_cvat import interpolation as interp


def make_ann(shape_type, shape_args, category="veh/car"):
    """A minimal stand-in for an upd annotation record."""
    return SimpleNamespace(
        shape_type=shape_type,
        shape_args=shape_args,
        annotation={"category": category},
    )


# ---------------------------------------------------------------------------
# _safe_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("a b/c", "a_b_c"),
    ("clip-01.mp4", "clip-01.mp4"),          # allowed chars preserved
    ("hé  llo", "h_llo"),                     # runs collapse, non-ascii → _
    ("***", "unnamed"),                       # all-stripped → fallback
    ("__lead__", "lead"),                     # leading/trailing _ stripped
])
def test_safe_name(raw, expected):
    assert c._safe_name(raw) == expected


# ---------------------------------------------------------------------------
# _clamp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v,hi,expected", [
    (-5.0, 100.0, 0.0),
    (150.0, 100.0, 100.0),
    (42.0, 100.0, 42.0),
    (0.0, 100.0, 0.0),
    (100.0, 100.0, 100.0),
])
def test_clamp(v, hi, expected):
    assert c._clamp(v, hi) == expected


# ---------------------------------------------------------------------------
# bbox_to_cvat
# ---------------------------------------------------------------------------

def test_bbox_to_cvat_min_max_of_corners():
    pts = [[0.2, 0.3], [0.8, 0.1], [0.5, 0.9], [0.1, 0.4]]
    xtl, ytl, xbr, ybr, rot = c.bbox_to_cvat(pts, 0.0, 100, 100)
    assert (xtl, ytl, xbr, ybr) == (10.0, 10.0, 80.0, 90.0)
    assert rot == 0.0


def test_bbox_to_cvat_clamps_out_of_bounds_corners():
    pts = [[-0.1, 0.2], [0.5, 0.9]]
    assert c.bbox_to_cvat(pts, 0.0, 100, 100) == (0.0, 20.0, 50.0, 90.0, 0.0)


def test_bbox_to_cvat_noclamp_keeps_negative():
    pts = [[-0.1, 0.2], [0.5, 0.9]]
    assert c.bbox_to_cvat(pts, 0.0, 100, 100, clamp=False) == (-10.0, 20.0, 50.0, 90.0, 0.0)


def test_bbox_to_cvat_rotation_converted_to_degrees_and_skips_clamp():
    # A rotated box is not clamped even when clamp=True; angle → degrees.
    pts = [[-0.5, -0.5], [1.5, 1.5]]
    xtl, ytl, xbr, ybr, rot = c.bbox_to_cvat(pts, math.pi / 2, 100, 100, clamp=True)
    assert rot == pytest.approx(90.0)
    assert (xtl, ytl) == (-50.0, -50.0)     # unclamped despite clamp=True
    assert (xbr, ybr) == (150.0, 150.0)


# ---------------------------------------------------------------------------
# _clip_polygon / polygon_to_cvat
# ---------------------------------------------------------------------------

def test_clip_polygon_leaves_fully_inside_polygon_unchanged():
    pts = [(10.0, 10.0), (90.0, 10.0), (90.0, 90.0), (10.0, 90.0)]
    assert c._clip_polygon(pts, 100, 100) == pts


def test_clip_polygon_clips_to_frame_bounds():
    # A square that overhangs the right/bottom edges.
    pts = [(50.0, 50.0), (150.0, 50.0), (150.0, 150.0), (50.0, 150.0)]
    clipped = c._clip_polygon(pts, 100, 100)
    xs = [x for x, _ in clipped]
    ys = [y for _, y in clipped]
    assert max(xs) <= 100.0 + 1e-9
    assert max(ys) <= 100.0 + 1e-9
    assert min(xs) >= -1e-9 and min(ys) >= -1e-9


def test_polygon_to_cvat_noclamp_raw_pixels():
    got = c.polygon_to_cvat([[0.1, 0.2], [0.3, 0.4]], 100, 100, clamp=False)
    assert got == "10.00,20.00;30.00,40.00"


def test_polygon_to_cvat_open_path_is_clamped_per_vertex():
    # closed=False (polyline): each vertex pinned, count preserved.
    got = c.polygon_to_cvat([[-0.5, 0.5], [1.5, 0.5]], 100, 100, clamp=True, closed=False)
    assert got == "0.00,50.00;100.00,50.00"


def test_polygon_to_cvat_degenerate_clip_falls_back_to_clamp():
    # A tiny fully-off-frame polygon clips away to <3 pts → per-vertex clamp,
    # so we never emit empty geometry.
    pts = [[2.0, 2.0], [2.1, 2.0], [2.05, 2.1]]
    got = c.polygon_to_cvat(pts, 100, 100, clamp=True, closed=True)
    assert got  # non-empty
    # all clamped to the far corner (200 → 100)
    assert set(got.split(";")) == {"100.00,100.00"}


# ---------------------------------------------------------------------------
# build_labels
# ---------------------------------------------------------------------------

def test_build_labels_keys_on_id_and_dedupes():
    cfg = {
        "s1": {"values": [
            {"id": "veh/truck", "label": "Truck", "color": "#ff0000"},
            {"id": "veh/car", "label": "Car"},
        ]},
        "s2": {"values": [
            {"id": "veh/truck", "label": "Truck", "color": "#00ff00"},  # dup id
        ]},
    }
    xml = c.build_labels(cfg)
    assert xml.count("<label>") == 2                 # deduped
    assert "<name>veh/truck</name>" in xml
    assert "<name>veh/car</name>" in xml
    assert "<color>#ff0000</color>" in xml           # first color wins
    assert "<color>#00ff00</color>" not in xml


def test_build_labels_falls_back_to_label_when_no_id():
    cfg = {"s1": {"values": [{"label": "Pedestrian", "color": "#123456"}]}}
    xml = c.build_labels(cfg)
    assert "<name>Pedestrian</name>" in xml


def test_build_labels_empty_config():
    xml = c.build_labels({})
    assert "<labels>" in xml and "</labels>" in xml
    assert "<label>" not in xml


# ---------------------------------------------------------------------------
# build_meta / build_meta_images
# ---------------------------------------------------------------------------

def test_build_meta_video_fields():
    xml = c.build_meta(task_id=7, name="clip", size=50, labels_xml="LBL",
                       width=640, height=480, source="clip.mp4")
    assert "<id>7</id>" in xml
    assert "<mode>interpolation</mode>" in xml
    assert "<size>50</size>" in xml
    assert "<stop_frame>49</stop_frame>" in xml       # size - 1
    assert "<width>640</width>" in xml
    assert "<height>480</height>" in xml
    assert "LBL" in xml


def test_build_meta_stop_frame_never_negative_for_empty_video():
    xml = c.build_meta(task_id=0, name="n", size=0, labels_xml="L",
                       width=1, height=1, source="s")
    assert "<stop_frame>0</stop_frame>" in xml


def test_build_meta_images_annotation_mode():
    xml = c.build_meta_images(task_id=2, name="ds", size=12, labels_xml="LBL")
    assert "<mode>annotation</mode>" in xml
    assert "<size>12</size>" in xml
    assert "<id>2</id>" in xml
    # image meta has no frame range / original_size
    assert "<stop_frame>" not in xml
    assert "<original_size>" not in xml


# ---------------------------------------------------------------------------
# _image_shape
# ---------------------------------------------------------------------------

def test_image_shape_box():
    el = c._image_shape("bounding-box", {"points": [[0.1, 0.1], [0.5, 0.5]]},
                        100, 100, "L")
    assert el.startswith("    <box ")
    assert 'xtl="10.00"' in el and 'ybr="50.00"' in el
    assert 'label="L"' in el


def test_image_shape_polygon():
    el = c._image_shape("polygon", {"points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]},
                        100, 100, "L")
    assert el.startswith("    <polygon ") and 'points="' in el


def test_image_shape_line_becomes_polyline():
    el = c._image_shape("line", {"points": [[0.1, 0.1], [0.9, 0.9]]}, 100, 100, "L")
    assert el.startswith("    <polyline ")
    assert 'points="10.00,10.00;90.00,90.00"' in el


def test_image_shape_ellipse():
    el = c._image_shape("ellipse", {"points": [[0.5, 0.5], [0.25, 0.1]]},
                        100, 100, "L")
    assert 'cx="50.00"' in el and 'cy="50.00"' in el
    assert 'rx="25.00"' in el and 'ry="10.00"' in el


def test_image_shape_unsupported_returns_none():
    assert c._image_shape("mask", {"points": []}, 100, 100, "L") is None


# ---------------------------------------------------------------------------
# write_image_body
# ---------------------------------------------------------------------------

def test_write_image_body_uses_category_as_label_and_skips_unsupported():
    anns = [
        make_ann("idah-image:bounding-box", {"points": [[0.1, 0.1], [0.5, 0.5]]},
                 category="veh/truck"),
        make_ann("idah-image:mask", {"points": []}, category="veh/car"),
    ]
    body = c.write_image_body(anns, 100, 100)
    assert body.count("<box ") == 1
    assert 'label="veh/truck"' in body
    assert "mask" not in body


# ---------------------------------------------------------------------------
# write_video_body
# ---------------------------------------------------------------------------

def _bbox_track_args():
    return {"frames": [
        {"frame": 0, "points": [[0.0, 0.0], [0.5, 0.5]]},
        {"frame": 2, "points": [[0.0, 0.0], [0.6, 0.6]]},
    ]}


def test_write_video_body_materialises_every_frame_as_keyframe():
    ann = make_ann("idah-video:bounding-box", _bbox_track_args())
    body = c.write_video_body([ann], 100, 100, n_frames=10)
    assert body.count("<track ") == 1
    # frames 0,1,2 + one terminating outside shape at frame 3
    assert body.count("<box ") == 4
    assert body.count('keyframe="0"') == 0            # every shape is a keyframe
    assert 'label="veh/car"' in body


def test_write_video_body_appends_outside_terminator():
    ann = make_ann("idah-video:bounding-box", _bbox_track_args())
    body = c.write_video_body([ann], 100, 100, n_frames=10)
    assert body.count('outside="1"') == 1
    assert 'frame="3" keyframe="1" outside="1"' in body


def test_write_video_body_no_terminator_when_track_reaches_last_frame():
    # end (2) + 1 == n_frames-1? here n_frames=3 → last frame index 2, so the
    # terminator at frame 3 would exceed the video and must be suppressed.
    ann = make_ann("idah-video:bounding-box", _bbox_track_args())
    body = c.write_video_body([ann], 100, 100, n_frames=3)
    assert 'outside="1"' not in body


def test_write_video_body_skips_unsupported_shape_and_empty_frames():
    unsupported = make_ann("idah-video:mask", {"frames": [{"frame": 0, "points": []}]})
    empty = make_ann("idah-video:bounding-box", {"frames": []})
    body = c.write_video_body([unsupported, empty], 100, 100, n_frames=10)
    assert body == ""


def test_write_video_body_assigns_sequential_track_ids():
    anns = [
        make_ann("idah-video:bounding-box", _bbox_track_args(), category="a"),
        make_ann("idah-video:bounding-box", _bbox_track_args(), category="b"),
    ]
    body = c.write_video_body(anns, 100, 100, n_frames=10)
    assert 'id="0"' in body and 'id="1"' in body


# ---------------------------------------------------------------------------
# _frame_shape / _shape_suffix
# ---------------------------------------------------------------------------

def test_shape_suffix():
    assert c._shape_suffix("idah-video:polygon") == "polygon"
    assert c._shape_suffix("polygon") == "polygon"


def test_frame_shape_bbox_emits_rotation_only_when_nonzero():
    plain = c._frame_shape(interp.BBOX, 0, [[0.0, 0.0], [0.5, 0.5]], 100, 100,
                           keyframe=1, outside=0, angle=0.0)
    rotated = c._frame_shape(interp.BBOX, 0, [[0.0, 0.0], [0.5, 0.5]], 100, 100,
                             keyframe=1, outside=0, angle=math.pi / 2)
    assert "rotation=" not in plain
    assert 'rotation="90.00"' in rotated
