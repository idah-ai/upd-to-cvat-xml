"""Specs for the opt-in hierarchical label mode (schema + IDAH-path map).

Covers :func:`build_labels_from_schema`, the checkbox normaliser,
:class:`LabelMapper` resolution, the config validator, and the ``<attribute>``
children the body writers emit on shapes/tracks when a mapper is supplied. The
flag-*off* path (no mapper) is covered by ``test_converter.py``; here every test
supplies a mapper and asserts the hierarchical output.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from upd_to_cvat import converter as c


def make_ann(shape_type, shape_args, category, attributes=None):
    ann = SimpleNamespace(
        shape_type=shape_type,
        shape_args=shape_args,
        annotation={"category": category},
    )
    if attributes is not None:
        ann.annotation["attributes"] = attributes
    return ann


# A small two-label schema exercising select (L2/L3/L4) and a checkbox.
SCHEMA = [
    {
        "name": "vehicle_civilian", "color": "#37e9cb", "type": "any",
        "attributes": [
            {"name": "L2", "input_type": "select", "mutable": False,
             "values": ["civilian", "other"], "default_value": "civilian"},
            {"name": "L3", "input_type": "select", "mutable": False,
             "values": ["-", "car"], "default_value": "-"},
            {"name": "L4", "input_type": "select", "mutable": False,
             "values": ["-", "suv"], "default_value": "-"},
            {"name": "camouflaged", "input_type": "checkbox", "mutable": False,
             "values": ["false"], "default_value": "false"},
        ],
    },
    {
        "name": "other", "color": "#51b90a", "type": "any",
        "attributes": [
            {"name": "L2", "input_type": "select", "mutable": False,
             "values": ["-", "animal"], "default_value": "-"},
        ],
    },
]

MAP = {
    "vehicle/civilian/car/suv": {"label": "vehicle_civilian",
                                 "L2": "civilian", "L3": "car", "L4": "suv"},
    "other/animal": {"label": "other", "L2": "animal"},
}


def mapper():
    return c.LabelMapper(SCHEMA, MAP)


# ---------------------------------------------------------------------------
# build_labels_from_schema / _attributes_block
# ---------------------------------------------------------------------------

def test_build_labels_from_schema_emits_name_color_type_and_attributes():
    xml = c.build_labels_from_schema(SCHEMA)
    assert "<name>vehicle_civilian</name>" in xml
    assert "<color>#37e9cb</color>" in xml
    assert "<type>any</type>" in xml
    # select values are newline-separated inside a single <values> element
    assert "<values>civilian\nother</values>" in xml
    assert "<input_type>checkbox</input_type>" in xml
    assert "<mutable>False</mutable>" in xml


def test_attributes_block_empty_collapses_to_one_line():
    assert c._attributes_block([]) == "          <attributes></attributes>"


# ---------------------------------------------------------------------------
# colour inheritance
# ---------------------------------------------------------------------------

# The same two labels, but with no colour of their own — so a colour must be
# inherited from the IDAH source when one is available.
SCHEMA_NO_COLOR = [
    {"name": "vehicle_civilian", "type": "any",
     "attributes": [{"name": "L2", "input_type": "select", "mutable": False,
                     "values": ["civilian"], "default_value": "civilian"}]},
    {"name": "other", "type": "any", "attributes": []},
]

# IDAH source colours for the categories the map covers.
LABELING_CONFIG = {"idah-video:bounding-box": {"values": [
    {"id": "vehicle/civilian/car/suv", "color": "#111111"},
    {"id": "other/animal", "color": "#222222"},
]}}


def test_schema_colour_is_used_when_present():
    # SCHEMA defines colours; fallback is ignored even if offered.
    xml = c.build_labels_from_schema(SCHEMA, {"vehicle_civilian": "#000000"})
    assert "<color>#37e9cb</color>" in xml
    assert "#000000" not in xml


def test_missing_colour_inherits_from_fallback():
    xml = c.build_labels_from_schema(
        SCHEMA_NO_COLOR, {"vehicle_civilian": "#111111", "other": "#222222"})
    assert "<color>#111111</color>" in xml
    assert "<color>#222222</color>" in xml


def test_missing_colour_and_no_fallback_stays_empty():
    xml = c.build_labels_from_schema(SCHEMA_NO_COLOR)
    assert "<color></color>" in xml


def test_labels_xml_inherits_idah_colour_for_colourless_label():
    m = c.LabelMapper(SCHEMA_NO_COLOR, MAP)
    xml = m.labels_xml(LABELING_CONFIG)
    assert "<color>#111111</color>" in xml     # vehicle_civilian ← its category
    assert "<color>#222222</color>" in xml     # other ← its category


def test_labels_xml_without_config_leaves_colour_empty():
    m = c.LabelMapper(SCHEMA_NO_COLOR, MAP)
    assert "<color></color>" in m.labels_xml()


# ---------------------------------------------------------------------------
# _checkbox_value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, "false"),            # absent → default
    (True, "true"),
    (False, "false"),
    ("true", "true"),
    ("FALSE", "false"),
    ("1", "true"),
    (["true"], "true"),         # one-element list, like occlusion
    ([], "false"),              # empty list → default
    ("weird", "false"),         # unrecognised → default, never a silent true
])
def test_checkbox_value(raw, expected):
    assert c._checkbox_value(raw, "false") == expected


# ---------------------------------------------------------------------------
# LabelMapper.resolve
# ---------------------------------------------------------------------------

def test_resolve_maps_category_to_label_and_select_attrs():
    label, attrs = mapper().resolve({"category": "vehicle/civilian/car/suv"})
    assert label == "vehicle_civilian"
    assert attrs["L2"] == "civilian"
    assert attrs["L3"] == "car"
    assert attrs["L4"] == "suv"


def test_resolve_defaults_checkbox_when_absent_from_idah():
    _, attrs = mapper().resolve({"category": "vehicle/civilian/car/suv"})
    assert attrs["camouflaged"] == "false"


def test_resolve_reads_checkbox_from_idah_attributes_case_insensitively():
    _, attrs = mapper().resolve({"category": "vehicle/civilian/car/suv",
                                 "attributes": {"Camouflaged": True}})
    assert attrs["camouflaged"] == "true"


def test_resolve_attribute_order_follows_schema():
    _, attrs = mapper().resolve({"category": "vehicle/civilian/car/suv"})
    assert list(attrs) == ["L2", "L3", "L4", "camouflaged"]


def test_resolve_unknown_category_passes_through_with_no_attrs():
    # Left for check_categories to report / CVAT to reject — never dropped.
    label, attrs = mapper().resolve({"category": "vehicle/unmapped"})
    assert label == "vehicle/unmapped"
    assert attrs == {}


def test_declared_is_the_map_keys():
    assert mapper().declared == set(MAP)


# ---------------------------------------------------------------------------
# _validate_label_config
# ---------------------------------------------------------------------------

def test_validate_rejects_label_not_in_schema():
    with pytest.raises(SystemExit, match="not in schema"):
        c._validate_label_config(SCHEMA, {"x": {"label": "ghost"}})


def test_validate_rejects_value_not_in_schema():
    bad = {"x": {"label": "vehicle_civilian", "L3": "boat"}}   # boat not allowed
    with pytest.raises(SystemExit, match="L3='boat'"):
        c._validate_label_config(SCHEMA, bad)


def test_validate_accepts_a_correct_map():
    c._validate_label_config(SCHEMA, MAP)     # does not raise


def test_load_label_mapper_requires_both_paths(tmp_path):
    schema_file = tmp_path / "s.json"
    schema_file.write_text("[]")
    with pytest.raises(SystemExit, match="given together"):
        c.load_label_mapper(str(schema_file), None)


def test_load_label_mapper_none_when_both_absent():
    assert c.load_label_mapper(None, None) is None


# ---------------------------------------------------------------------------
# shape / track attribute emission
# ---------------------------------------------------------------------------

def _bbox_track_args():
    return {"frames": [
        {"frame": 0, "points": [[0.0, 0.0], [0.5, 0.5]]},
        {"frame": 2, "points": [[0.0, 0.0], [0.6, 0.6]]},
    ]}


def test_video_track_carries_mapped_label_and_attributes():
    ann = make_ann("idah-video:bounding-box", _bbox_track_args(),
                   category="vehicle/civilian/car/suv")
    body = c.write_video_body([ann], 100, 100, n_frames=10, mapper=mapper())
    assert 'label="vehicle_civilian"' in body
    assert '<attribute name="L3">car</attribute>' in body
    assert '<attribute name="L4">suv</attribute>' in body
    assert '<attribute name="camouflaged">false</attribute>' in body
    # every box of the track (keyframes, in-betweens, and the outside
    # terminator) repeats the immutable attribute
    assert body.count("<box ") == body.count('<attribute name="L2">civilian</attribute>')


def test_video_body_without_mapper_has_no_attribute_children():
    ann = make_ann("idah-video:bounding-box", _bbox_track_args(),
                   category="vehicle/civilian/car/suv")
    body = c.write_video_body([ann], 100, 100, n_frames=10)
    assert "<attribute" not in body
    assert 'label="vehicle/civilian/car/suv"' in body


def test_image_shape_wraps_with_attributes_when_mapped():
    ann = make_ann("idah-image:bounding-box",
                   {"points": [[0.1, 0.1], [0.5, 0.5]]},
                   category="other/animal")
    body = c.write_image_body([ann], 100, 100, mapper=mapper())
    assert 'label="other"' in body
    assert '<attribute name="L2">animal</attribute>' in body
    assert body.rstrip().endswith("</box>")     # opened a body, not self-closed


def test_image_shape_self_closes_without_mapper():
    ann = make_ann("idah-image:bounding-box",
                   {"points": [[0.1, 0.1], [0.5, 0.5]]},
                   category="other/animal")
    body = c.write_image_body([ann], 100, 100)
    assert body.rstrip().endswith("></box>")    # self-closed, unchanged
    assert "<attribute" not in body
