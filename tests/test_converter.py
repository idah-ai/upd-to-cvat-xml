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
    # One colour per label: the first shape type in sorted order that has one.
    assert "<color>#ff0000</color>" in xml           # from "s1"
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


def test_write_video_body_materialises_every_frame():
    ann = make_ann("idah-video:bounding-box", _bbox_track_args())
    body = c.write_video_body([ann], 100, 100, n_frames=10)
    assert body.count("<track ") == 1
    # frames 0,1,2 + one terminating outside shape at frame 3
    assert body.count("<box ") == 4
    # bbox anchors (frames 0, 2) + the outside terminator are keyframes; the
    # materialised in-between (frame 1) is keyframe="0" so CVAT re-interpolates
    # it from the anchors on import (see _segment_shapes docstring).
    assert body.count('keyframe="1"') == 3
    assert body.count('keyframe="0"') == 1
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


# ---------------------------------------------------------------------------
# _rotation_attr — CVAT rejects rotation outside [0, 360]
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("deg,expected", [
    (0.0, ""),                              # upright → attribute omitted
    (2.01, ' rotation="2.01"'),
    (-2.01, ' rotation="357.99"'),          # signed IDAH angle wraps
    (-0.001, ""),                           # rounds to 0 before the wrap, not 360
    (359.999, ""),                          # …and so does a hair under a full turn
    (360.0, ""),                            # a full turn is upright
    (-360.0, ""),
    (400.0, ' rotation="40.00"'),           # beyond one turn wraps too
    (-450.0, ' rotation="270.00"'),
])
def test_rotation_attr_wraps_into_cvat_range(deg, expected):
    assert c._rotation_attr(deg) == expected


def test_rotation_attr_never_emits_a_negative():
    for deg in (-0.01, -1.0, -8.1, -179.99, -180.0, -359.99):
        assert "-" not in c._rotation_attr(deg)


def test_bbox_to_cvat_keeps_the_angle_signed_for_interpolation():
    # The wrap happens at serialisation; the converter stays in signed degrees so
    # callers can interpolate across 0 without sweeping the long way round.
    *_, rot = c.bbox_to_cvat([[0.0, 0.0], [0.5, 0.5]], -math.pi / 2, 100, 100)
    assert rot == pytest.approx(-90.0)


def test_bbox_track_rotation_crossing_zero_takes_the_short_way():
    # Two anchors either side of 0° (-2° → +2°): the in-betweens must pass
    # through 0/360, not through 180.
    pts = [[0.1, 0.1], [0.2, 0.2]]
    seq = [
        (0, pts, math.radians(-2.0), True),
        (1, pts, math.radians(-1.0), False),
        (2, pts, math.radians(2.0), True),
    ]
    emitted = c._bbox_track_shapes(seq, 100, 100, clamp=True)
    mid_rot = emitted[1][1][4]
    assert mid_rot == pytest.approx(0.0, abs=0.01)     # unwrapped midpoint of -2 and 2

    xml = c._box_xml(1, emitted[1][1], keyframe=0, outside=0)
    assert "rotation=" not in xml                      # 0° → no attribute
    assert 'rotation="358.00"' in c._box_xml(0, emitted[0][1], keyframe=1, outside=0)
    assert 'rotation="2.00"' in c._box_xml(2, emitted[2][1], keyframe=1, outside=0)


# ---------------------------------------------------------------------------
# Export levels — level_for / _task_block / build_meta_project / build_meta_job
# ---------------------------------------------------------------------------

def test_level_follows_the_deepest_active_filter():
    assert c.level_for(None, None) == c.PROJECT
    assert c.level_for("ds-1", None) == c.TASK
    assert c.level_for(None, "e-1") == c.JOB
    assert c.level_for("ds-1", "e-1") == c.JOB       # entry wins over dataset


def test_task_block_has_segments_but_no_labels():
    # A project declares its labels once, at project level — never per task.
    xml = c._task_block(task_id=3, name="clip", size=51, mode="interpolation",
                        width=640, height=480)
    assert "<labels>" not in xml
    assert "<segments>" in xml
    assert "<id>3</id>" in xml
    assert "<source>clip</source>" in xml


def test_task_block_frame_range_stays_task_local():
    # Bodies use project-wide frame numbers, but <start_frame>/<stop_frame> of
    # each task stay 0-based — that is what CVAT itself emits.
    xml = c._task_block(task_id=1, name="c", size=46, mode="interpolation",
                        width=1, height=1)
    assert "<start_frame>0</start_frame>" in xml
    assert "<stop_frame>45</stop_frame>" in xml


def test_task_block_omits_original_size_for_image_tasks():
    xml = c._task_block(task_id=0, name="ds", size=4, mode="annotation")
    assert "<original_size>" not in xml
    assert "<source>" not in xml


def test_build_meta_project_nests_tasks_and_hoists_labels():
    xml = c.build_meta_project(project_id=0, name="proj",
                               task_blocks=["<task/>", "<task/>"],
                               labels_xml="LBL")
    assert "<project>" in xml and "</project>" in xml
    assert xml.count("<task/>") == 2
    assert "<subsets>default</subsets>" in xml
    assert xml.index("LBL") < xml.index("</project>")   # labels are the project's


def test_build_meta_job_has_no_name_and_hoists_original_size():
    # A job is a frame range, not a media file: no <name>/<source>, and CVAT
    # writes <original_size> *outside* <job>, after <dumped>.
    xml = c.build_meta_job(job_id=9, size=51, labels_xml="LBL",
                           mode="interpolation", width=640, height=480)
    assert "<name>" not in xml and "<source>" not in xml
    assert "<id>9</id>" in xml
    assert "<stop_frame>50</stop_frame>" in xml
    assert xml.index("</job>") < xml.index("<original_size>")


def test_build_meta_job_omits_original_size_for_image_jobs():
    xml = c.build_meta_job(job_id=0, size=4, labels_xml="L", mode="annotation")
    assert "<original_size>" not in xml


# ---------------------------------------------------------------------------
# write_video_body — project-level offsets
# ---------------------------------------------------------------------------

def test_write_video_body_tags_tracks_for_project_level():
    anns = [make_ann("idah-video:bounding-box", _bbox_track_args(), category="a")]
    plain = c.write_video_body(anns, 100, 100, n_frames=10)
    tagged = c.write_video_body(anns, 100, 100, n_frames=10, track_id_start=7,
                                extra_attrs=' task_id="2" subset="clip-a"')

    assert 'id="7"' in tagged and 'task_id="2"' in tagged
    assert 'subset="clip-a"' in tagged
    # Only the track element changes — frames stay task-local (each subset is
    # an independent CVAT task starting at frame 0) and geometry is untouched.
    frames = lambda b: [int(f) for f in re.findall(r'(?<!key)frame="(\d+)"', b)]
    assert frames(plain) == frames(tagged)
    assert (re.findall(r'xtl="([\d.]+)"', plain)
            == re.findall(r'xtl="([\d.]+)"', tagged))


def test_build_meta_project_lists_every_subset():
    # CVAT creates one task per subset on import, so <subsets> must name them all.
    xml = c.build_meta_project(project_id=0, name="p", task_blocks=["<task/>"],
                               labels_xml="L", subsets=["clip_a", "clip_b"])
    assert "<subsets>clip_a\nclip_b</subsets>" in xml


def test_build_meta_project_defaults_to_single_default_subset():
    xml = c.build_meta_project(project_id=0, name="p", task_blocks=[],
                               labels_xml="L")
    assert "<subsets>default</subsets>" in xml


def test_task_block_carries_its_own_subset():
    xml = c._task_block(task_id=1, name="c", size=5, mode="interpolation",
                        subset="clip_a", width=1, height=1)
    assert "<subset>clip_a</subset>" in xml


# ---------------------------------------------------------------------------
# is_occluded — IDAH occlusion attribute → CVAT occluded flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    # scalars, as stored
    ("Partial", True),
    ("FULL", True),
    ("", False),
    # casing is inconsistent in the source data, so matching ignores it
    ("partial", True),
    ("full", True),
    ("Full", True),
    ("PARTIAL", True),
    (" Partial ", True),        # and tolerates surrounding whitespace
    (" full ", True),
    # sometimes wrapped in a list — every casing shows up there too
    (["Partial"], True),
    (["FULL"], True),
    (["full"], True),
    (["Full"], True),
    ([""], False),
    ([], False),
    # a multi-entry list counts if *any* entry is occluded, not just the first
    (["", "FULL"], True),
    (["FULL", "Partial"], True),
    (["", ""], False),
    # anything else is not occlusion
    ("None", False),
    ("unknown", False),
    (None, False),
])
def test_is_occluded_handles_every_stored_value_shape(value, expected):
    assert c.is_occluded({"attributes": {"occlusion": value}}) is expected


@pytest.mark.parametrize("annotation", [
    {},                                  # no attributes at all
    {"attributes": {}},                  # attributes present but empty
    {"attributes": None},                # attributes explicitly null
    {"attributes": {"other": "x"}},      # unrelated attribute only
    {"category": "human/civilian/undefined"},
    None,
])
def test_is_occluded_defaults_to_false(annotation):
    assert c.is_occluded(annotation) is False


def test_write_video_body_marks_every_frame_of_an_occluded_track():
    ann = make_ann("idah-video:bounding-box", _bbox_track_args())
    ann.annotation["attributes"] = {"occlusion": "Partial"}
    body = c.write_video_body([ann], 100, 100, n_frames=10)
    # Occlusion is a track-level property: it holds for every emitted shape,
    # including the outside="1" terminator.
    assert 'occluded="0"' not in body
    assert body.count('occluded="1"') == body.count("<box ")


def test_write_video_body_unoccluded_track_stays_zero():
    ann = make_ann("idah-video:bounding-box", _bbox_track_args())
    body = c.write_video_body([ann], 100, 100, n_frames=10)
    assert 'occluded="1"' not in body


def test_write_video_body_occlusion_is_per_track_not_global():
    a = make_ann("idah-video:bounding-box", _bbox_track_args(), category="a")
    a.annotation["attributes"] = {"occlusion": "FULL"}
    b = make_ann("idah-video:bounding-box", _bbox_track_args(), category="b")
    body = c.write_video_body([a, b], 100, 100, n_frames=10)

    tracks = body.split("<track ")[1:]
    assert 'occluded="1"' in tracks[0] and 'occluded="0"' not in tracks[0]
    assert 'occluded="0"' in tracks[1] and 'occluded="1"' not in tracks[1]


def test_write_image_body_carries_occlusion_onto_the_shape():
    ann = make_ann("idah-image:bounding-box",
                   {"points": [[0.1, 0.1], [0.5, 0.5]]}, category="veh/truck")
    ann.annotation["attributes"] = {"occlusion": "FULL"}
    assert 'occluded="1"' in c.write_image_body([ann], 100, 100)


# ---------------------------------------------------------------------------
# build_labels — <type> and <color> resolution across shape types
# ---------------------------------------------------------------------------

def _cfg(**shape_types):
    """Labeling-Configuration from {shape_type: [(id, color), …]}."""
    return {st: {"values": [{"id": i, "color": c} for i, c in vals]}
            for st, vals in shape_types.items()}


@pytest.mark.parametrize("shape_type, expected", [
    ("idah-video:bounding-box", "rectangle"),
    ("idah-video:polygon", "polygon"),
    ("idah-image:bounding-box", "rectangle"),
    ("idah-image:polygon", "polygon"),
    ("idah-image:line", "polyline"),      # CVAT calls an open path a polyline
    ("idah-image:ellipse", "ellipse"),
    ("idah-image:circle", "ellipse"),     # CVAT has no circle primitive
])
def test_build_labels_types_track_the_declared_shape(shape_type, expected):
    xml = c.build_labels(_cfg(**{shape_type: [("veh/car", "#FF0000")]}))
    assert f"<type>{expected}</type>" in xml


def test_build_labels_unknown_shape_type_falls_back_to_any():
    xml = c.build_labels(_cfg(**{"idah-video:mask": [("veh/car", "#FF0000")]}))
    assert "<type>any</type>" in xml


def test_build_labels_label_in_several_shape_types_is_any():
    # One CVAT label per name, so a label drawn as both a box and a polygon
    # cannot be constrained to either.
    xml = c.build_labels({
        "idah-video:bounding-box": {"values": [{"id": "a", "color": "#111111"}]},
        "idah-video:polygon": {"values": [{"id": "a", "color": "#111111"}]},
    })
    assert xml.count("<label>") == 1
    assert "<type>any</type>" in xml


def test_build_labels_mixed_declaration_types_are_independent():
    xml = c.build_labels({
        "idah-video:bounding-box": {"values": [{"id": "both", "color": "#111111"},
                                               {"id": "boxonly", "color": "#222222"}]},
        "idah-video:polygon": {"values": [{"id": "both", "color": "#111111"}]},
    })
    both = xml[xml.index("<name>both</name>"):]
    boxonly = xml[xml.index("<name>boxonly</name>"):]
    assert "<type>any</type>" in both.split("</label>")[0]
    assert "<type>rectangle</type>" in boxonly.split("</label>")[0]


def test_build_labels_conflicting_colors_resolve_deterministically():
    # IDAH gives the same label a different colour per shape type; the source is
    # ambiguous, so the rule only has to be stable — first shape type by name.
    cfg = {
        "idah-image:polygon": {"values": [{"id": "car", "color": "#00A6F5"}]},
        "idah-image:bounding-box": {"values": [{"id": "car", "color": "#FFEC16"}]},
        "idah-image:line": {"values": [{"id": "car", "color": "#46AF4A"}]},
    }
    xml = c.build_labels(cfg)
    assert "<color>#FFEC16</color>" in xml          # bounding-box sorts first
    # …and does not depend on how the config dict happens to be ordered.
    reordered = dict(reversed(list(cfg.items())))
    assert c.build_labels(reordered) == xml


def test_build_labels_skips_empty_colors_when_choosing():
    xml = c.build_labels({
        "idah-image:bounding-box": {"values": [{"id": "car", "color": ""}]},
        "idah-image:polygon": {"values": [{"id": "car", "color": "#00A6F5"}]},
    })
    assert "<color>#00A6F5</color>" in xml


def test_build_labels_no_color_anywhere_emits_empty():
    xml = c.build_labels(_cfg(**{"idah-video:bounding-box": [("car", "")]}))
    assert "<color></color>" in xml


# ---------------------------------------------------------------------------
# Group-Id → one track, with outside="1" over the gaps
# ---------------------------------------------------------------------------

_seg_ids = iter(f"ann{n}" for n in range(1, 10_000))


def make_seg(start, end, group=None, category="veh/car", occlusion=None,
             id=None):
    """One annotation row spanning [start, end], optionally in a Group-Id.

    Every row gets a unique ``id``, as real IDAH rows do — it is what a
    follower's ``Group-Id`` points at (see :func:`converter._group_annotations`).
    """
    ann = SimpleNamespace(
        id=id or next(_seg_ids),
        shape_type="idah-video:bounding-box",
        shape_args={"start": start, "end": end, "frames": [
            {"frame": start, "points": [[0.0, 0.0], [0.5, 0.5]]},
            {"frame": end, "points": [[0.0, 0.0], [0.6, 0.6]]},
        ]},
        annotation={"category": category},
        metadata={"Group-Id": group} if group else {},
    )
    if occlusion is not None:
        ann.annotation["attributes"] = {"occlusion": occlusion}
    return ann


def _frames(block):
    return [int(f) for f in re.findall(r'(?<!key)frame="(\d+)"', block)]


def _outside_frames(block):
    return [int(f) for f in
            re.findall(r'(?<!key)frame="(\d+)"[^>]*outside="1"', block)]


def test_grouped_rows_become_one_track():
    # Contiguous segments of one object: IDAH stores 3 rows, CVAT gets 1 track.
    anns = [make_seg(0, 4, "g1"), make_seg(5, 9, "g1"), make_seg(10, 14, "g1")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 1
    assert 'id="0"' in body


def test_ungrouped_rows_stay_separate_tracks():
    anns = [make_seg(0, 4), make_seg(10, 14)]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 2


def test_leader_row_merges_with_its_followers():
    # How IDAH actually stores a group: the first row carries no Group-Id, and
    # each later row carries the *leader's annotation id*. All one object.
    leader = make_seg(0, 8, id="lead")
    anns = [leader, make_seg(9, 20, "lead"), make_seg(21, 30, "lead")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 1
    assert _outside_frames(body) == [31]           # contiguous: only the end


def test_leader_row_merges_when_it_comes_after_its_follower():
    anns = [make_seg(9, 20, "lead"), make_seg(0, 8, id="lead")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 1
    assert _frames(body) == sorted(_frames(body))


def test_follower_keeps_its_own_occlusion_over_the_leaders_span():
    # The real case: leader 0..8 clear, follower 9..50 Partial, one track.
    leader = make_seg(0, 8, id="lead")
    anns = [leader, make_seg(9, 20, "lead", occlusion="Partial")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 1
    assert 'occluded="0"' in re.search(r'<box frame="8"[^>]*>', body).group()
    assert 'occluded="1"' in re.search(r'<box frame="9"[^>]*>', body).group()


def test_follower_whose_leader_is_missing_stands_alone():
    # A Group-Id naming a row that is not in the entry (deleted leader): the
    # row is a track of its own, and does not collect unrelated rows.
    anns = [make_seg(0, 4, "gone"), make_seg(10, 14)]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 2


def test_leader_and_follower_of_different_categories_still_split():
    leader = make_seg(0, 4, category="a", id="lead")
    anns = [leader, make_seg(5, 9, "lead", category="b")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 2


def test_contiguous_group_has_no_interior_outside():
    anns = [make_seg(0, 4, "g1"), make_seg(5, 9, "g1")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    # Only the terminator at 10; the object never left between the segments.
    assert _outside_frames(body) == [10]


def test_gap_between_segments_emits_interior_outside():
    # The case from the spec: visible 5..9, away, back 23..25.
    anns = [make_seg(5, 9, "g1"), make_seg(23, 25, "g1")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)

    assert body.count("<track ") == 1
    assert _outside_frames(body) == [10, 26]      # gap starts, then terminator
    frames = _frames(body)
    assert min(frames) == 5 and max(frames) == 26
    # Nothing is emitted inside the gap — the object is not there.
    assert not any(10 < f < 23 for f in frames)


def test_segment_after_a_gap_reopens_with_a_keyframe():
    # A leading keyframe="0" would be discarded on import and the object would
    # reappear late.
    anns = [make_seg(5, 9, "g1"), make_seg(23, 25, "g1")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    resume = re.search(r'<box frame="23"[^>]*>', body).group()
    assert 'keyframe="1"' in resume and 'outside="0"' in resume


def test_multiple_gaps_each_get_their_own_outside():
    anns = [make_seg(0, 2, "g1"), make_seg(10, 12, "g1"), make_seg(20, 22, "g1")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert _outside_frames(body) == [3, 13, 23]


def test_group_segments_are_ordered_by_start_not_input_order():
    anns = [make_seg(23, 25, "g1"), make_seg(5, 9, "g1")]     # reversed input
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert _frames(body) == sorted(_frames(body))
    assert _outside_frames(body) == [10, 26]


def test_group_splits_into_one_track_per_category():
    # An IDAH group can carry rows of more than one category; a CVAT track holds
    # one label, so each category becomes its own track.
    anns = [make_seg(0, 4, "g1", category="undefined"),
            make_seg(5, 9, "g1", category="human/military")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 2
    assert 'label="undefined"' in body
    assert 'label="human/military"' in body


def test_same_category_rows_in_a_group_still_merge():
    anns = [make_seg(0, 4, "g1", category="human/military"),
            make_seg(5, 9, "g1", category="human/military")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 1


def test_mixed_category_group_gaps_are_per_category():
    # Category A holds 0..4, category B holds 5..9. Split by category, A's track
    # ends at 4 (terminator at 5) and B's runs 5..9 — the "gap" between them is
    # not a gap, since they are different objects/tracks now.
    anns = [make_seg(0, 4, "g1", category="a"),
            make_seg(5, 9, "g1", category="b")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    track_a = body.split("</track>")[0]
    track_b = body.split("</track>")[1]
    assert 'label="a"' in track_a and _outside_frames(track_a) == [5]
    assert 'label="b"' in track_b and _outside_frames(track_b) == [10]


def test_split_category_with_a_gap_still_merges_its_own_segments():
    # Same category twice with a hole → one track with an interior outside;
    # the other category is independent.
    anns = [make_seg(0, 4, "g1", category="a"),
            make_seg(10, 14, "g1", category="a"),
            make_seg(0, 20, "g1", category="b")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert body.count("<track ") == 2
    track_a = next(t for t in body.split("<track ")[1:] if 'label="a"' in t)
    assert _outside_frames(track_a) == [5, 15]     # gap at 5, terminator at 15


def test_occlusion_is_per_segment_within_a_merged_track():
    # Each row carries its own occlusion, so a merged track can vary over time.
    anns = [make_seg(0, 4, "g1", occlusion="Partial"), make_seg(5, 9, "g1")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    early = re.search(r'<box frame="0"[^>]*>', body).group()
    late = re.search(r'<box frame="9"[^>]*>', body).group()
    assert 'occluded="1"' in early
    assert 'occluded="0"' in late


def test_track_running_to_last_frame_has_no_terminator():
    anns = [make_seg(0, 4, "g1"), make_seg(5, 49, "g1")]
    body = c.write_video_body(anns, 100, 100, n_frames=50)
    assert _outside_frames(body) == []


def test_group_with_no_usable_segments_emits_no_track():
    ann = make_seg(0, 4, "g1")
    ann.shape_args["frames"] = []
    assert c.write_video_body([ann], 100, 100, n_frames=50) == ""


def test_out_of_range_span_warns_but_emits_unchanged(capsys):
    # One real dataset holds a polygon with start=-1; CVAT frames are
    # non-negative, so this is surfaced rather than silently rewritten.
    ann = make_seg(-1, 11, category="vehicles/bus")
    ann.id = "ann-x"
    body = c.write_video_body([ann], 100, 100, n_frames=100, where="clip.ts")
    assert 'frame="-1"' in body                       # geometry untouched
    warning = capsys.readouterr().out
    assert "clip.ts" in warning and "ann-x" in warning and "-1..11" in warning


def test_in_range_span_does_not_warn(capsys):
    c.write_video_body([make_seg(0, 4)], 100, 100, n_frames=50)
    assert "outside the video" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Warnings: everything dropped or split has to be visible in the log
# ---------------------------------------------------------------------------

def test_empty_frames_row_is_reported(capsys):
    ann = make_seg(0, 4, "g1")
    ann.id = "ann-empty"
    ann.shape_args["frames"] = []
    c.write_video_body([ann], 100, 100, n_frames=50)
    out = capsys.readouterr().out
    assert "no frames" in out and "ann-empty" in out and "0..4" in out


def test_category_split_is_reported_with_rows_and_spans(capsys):
    leader = make_seg(0, 4, category="a", id="lead")
    anns = [leader, make_seg(5, 9, "lead", category="b", id="follow")]
    c.write_video_body(anns, 100, 100, n_frames=50, where="clip.ts")
    out = capsys.readouterr().out
    assert "clip.ts" in out and "lead" in out and "follow" in out
    assert "2 categories" in out and "splits into 2 tracks" in out
    assert "'a'" in out and "'b'" in out
    assert "0..4" in out and "5..9" in out


def test_single_category_group_is_not_reported(capsys):
    c.write_video_body([make_seg(0, 4, "g1"), make_seg(5, 9, "g1")],
                       100, 100, n_frames=50)
    assert "categories" not in capsys.readouterr().out


def test_category_represented_only_by_an_empty_row_is_not_a_split(capsys):
    # The dropped row never becomes a track, so the group is not really split
    # and must not be reported as splitting.
    leader = make_seg(0, 9, category="a", id="lead")
    empty = make_seg(0, 0, "lead", category="b")
    empty.shape_args["frames"] = []
    body = c.write_video_body([leader, empty], 100, 100, n_frames=50)
    assert body.count("<track ") == 1
    assert "categories" not in capsys.readouterr().out


def test_rows_sharing_a_dangling_leader_still_merge():
    # They share the same key, so a deleted leader costs only its own frames:
    # 0..3 | 4..13 | 14..55 still come out as the one continuous track.
    anns = [make_seg(0, 3, "gone"), make_seg(4, 13, "gone"),
            make_seg(14, 55, "gone")]
    body = c.write_video_body(anns, 100, 100, n_frames=56)
    assert body.count("<track ") == 1
    assert _outside_frames(body) == []


def _config(*ids):
    """A Labeling-Configuration declaring ``ids`` for bounding boxes."""
    return {"idah-video:bounding-box":
            {"values": [{"id": i, "color": "#fff"} for i in ids]}}


def test_label_ids_collects_every_declared_id():
    cfg = {"idah-video:bounding-box": {"values": [{"id": "a"}, {"id": "b"}]},
           "idah-video:polygon": {"values": [{"id": "b"}, {"id": "c"}]}}
    assert c.label_ids(cfg) == {"a", "b", "c"}


def test_label_ids_of_an_empty_config_is_empty():
    assert c.label_ids({}) == set()
    assert c.label_ids(None) == set()


def test_undeclared_category_is_reported(capsys):
    # The real failure: a 'vessel/civilian-/…' typo CVAT has no label for, which
    # makes it reject the whole package with "annotation has no label".
    ann = make_seg(0, 4, category="vessel/civilian-/merchant")
    ann.id = "ann-typo"
    bad = c.check_categories([ann], c.label_ids(_config("vessel/civilian/merchant")),
                             where="clip.ts")
    assert list(bad) == ["vessel/civilian-/merchant"]
    out = capsys.readouterr().out
    assert "clip.ts" in out and "not declared" in out
    assert "vessel/civilian-/merchant" in out and "ann-typo" in out


def test_declared_category_is_not_reported(capsys):
    ann = make_seg(0, 4, category="vessel/civilian/merchant")
    assert c.check_categories([ann],
                              c.label_ids(_config("vessel/civilian/merchant"))) == {}
    assert capsys.readouterr().out == ""


def test_check_categories_groups_rows_by_category():
    anns = [make_seg(0, 4, category="bad"), make_seg(5, 9, category="bad"),
            make_seg(0, 4, category="ok")]
    bad = c.check_categories(anns, c.label_ids(_config("ok")))
    assert len(bad["bad"]) == 2 and "ok" not in bad


def test_check_categories_skips_when_no_taxonomy_is_declared(capsys):
    # Nothing to validate against — every category would look undeclared.
    assert c.check_categories([make_seg(0, 4, category="x")], set()) == {}
    assert capsys.readouterr().out == ""


def test_dangling_group_id_is_not_warned_about(capsys):
    # The track that comes out is the one those rows would have formed anyway,
    # so there is nothing to act on and the log stays quiet.
    ann = make_seg(14, 50, "no-such-leader")
    ann.id = "orphan"
    c.write_video_body([ann], 100, 100, n_frames=51, where="clip.ts")
    assert capsys.readouterr().out == ""


def test_unsupported_shape_type_names_the_annotation(capsys):
    ann = make_seg(0, 4)
    ann.id = "ann-mask"
    ann.shape_type = "idah-video:mask"
    c.write_video_body([ann], 100, 100, n_frames=50)
    out = capsys.readouterr().out
    assert "ann-mask" in out and "idah-video:mask" in out


def test_shape_type_mismatch_inside_a_group_names_the_annotation(capsys):
    anns = [make_seg(0, 4, "g1")]
    other = make_seg(5, 9, "g1")
    other.id = "ann-poly"
    other.shape_type = "idah-video:polygon"
    c.write_video_body(anns + [other], 100, 100, n_frames=50)
    out = capsys.readouterr().out
    assert "ann-poly" in out and "one track is one shape type" in out


@pytest.mark.parametrize("value", ["Heavy", ["Partial", "Heavy"], "occluded"])
def test_unrecognised_occlusion_is_reported(value, capsys):
    ann = make_seg(0, 4, occlusion=value)
    ann.id = "ann-occ"
    c.write_video_body([ann], 100, 100, n_frames=50)
    out = capsys.readouterr().out
    assert "unrecognised occlusion" in out and "ann-occ" in out
    assert "'Heavy'" in out or "'occluded'" in out


@pytest.mark.parametrize("value", ["Partial", "FULL", "full", " ", "", ["FULL"]])
def test_known_or_blank_occlusion_is_not_reported(value, capsys):
    # Blank and whitespace-only mean "no occlusion recorded" — the normal case.
    c.write_video_body([make_seg(0, 4, occlusion=value)], 100, 100, n_frames=50)
    assert "unrecognised occlusion" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# split_entries / _part_suffix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size,expected", [
    (2, [[1, 2], [3, 4], [5]]),           # last chunk keeps the remainder
    (5, [[1, 2, 3, 4, 5]]),               # size == len → one chunk
    (9, [[1, 2, 3, 4, 5]]),               # size > len → one chunk
    (1, [[1], [2], [3], [4], [5]]),
])
def test_split_entries_chunks_in_order(size, expected):
    assert c.split_entries([1, 2, 3, 4, 5], size) == expected


@pytest.mark.parametrize("size", [None, 0, -1])
def test_split_entries_without_a_size_is_one_group(size):
    assert c.split_entries([1, 2, 3], size) == [[1, 2, 3]]


def test_split_entries_keeps_every_entry_exactly_once():
    entries = list(range(23))
    assert [e for chunk in c.split_entries(entries, 4) for e in chunk] == entries


@pytest.mark.parametrize("part,expected", [
    (None, ""),                            # unsplit → no suffix at all
    ((3, 12), "_part03of12"),              # padded to the width of the total
    ((1, 5), "_part1of5"),
    ((7, 100), "_part007of100"),
])
def test_part_suffix(part, expected):
    assert c._part_suffix(part) == expected
