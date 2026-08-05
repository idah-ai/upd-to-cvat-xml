# Label configs — remapping IDAH categories to CVAT labels

By default the exporter writes each IDAH tree-path (`vehicle/civilian/car/suv`)
straight through as a **flat** CVAT label name with no attributes. Some clients
instead want a small set of **top-level labels** whose sub-classes are carried as
`L2` / `L3` / `L4` `select` attributes (plus the odd checkbox):

```
IDAH:  vehicle/civilian/car/suv
CVAT:  label = vehicle_civilian,  L2 = civilian,  L3 = car,  L4 = suv
```

That taxonomy is **client-defined and cannot be derived from the paths** — the
same IDAH branch can regroup under a different label, value spellings differ
(`heavytrailer` → `heavyTrailer`), and CVAT can add attributes IDAH never had. So
it is supplied as two JSON files per project, and the converter is told to use
them with `--label-schema` / `--label-map`.

This folder holds those files. The real ones are **git-ignored** (see
`.gitignore`) because they encode a client's taxonomy and are regenerated per
project. What *is* tracked: this README, the `.gitignore`, and a committed example
pair you can copy from —
[`label_schema.example.json`](label_schema.example.json) and
[`label_map.example.json`](label_map.example.json). Regenerate the real files with
the steps below.

## Layout

One folder per project, named `<program>_<dataset>`:

```
configs/
  README.md                    # this file (tracked)
  label_schema.example.json    # committed example (tracked)
  label_map.example.json       # committed example (tracked)
  <program>_<dataset>/         # one folder per project (git-ignored)
    label_schema.json          # the client's CVAT label set (authoritative)
    label_map.json             # IDAH category -> {label, L2, L3, L4}
```

## The two files

Full, runnable versions of both live in
[`label_schema.example.json`](label_schema.example.json) and
[`label_map.example.json`](label_map.example.json). Copy them into a project
folder and edit, or just read them alongside the descriptions below.

### `label_schema.json` — the client's CVAT label set

This **is** CVAT's own label-config JSON (the format you get from a CVAT project's
*Labels → Raw* tab). The client provides it. One entry per top-level label —
abridged from [`label_schema.example.json`](label_schema.example.json):

```json
[
  {
    "name": "vehicle_civilian",
    "color": "#37e9cb",
    "type": "any",
    "attributes": [
      { "name": "L2", "input_type": "select", "mutable": false,
        "values": ["civilian", "other"], "default_value": "civilian" },
      { "name": "L3", "input_type": "select", "mutable": false,
        "values": ["-", "car", "truck"], "default_value": "-" },
      { "name": "camouflaged", "input_type": "checkbox", "mutable": false,
        "values": ["false"], "default_value": "false" }
    ]
  }
]
```

- `color` is optional. If a label omits it, the exporter inherits the colour of
  the first IDAH source category that maps into that label. (The example's
  `vehicle_construction` label leaves `color` out to show this.)
- Attributes may be `select` (L2/L3/L4) or `checkbox` (camouflaged, tarped, …).
- A `"id"` field (server-assigned, e.g. `8285`) is harmless here but should be
  **removed before pasting into CVAT's Raw editor for a new project**.

### `label_map.json` — IDAH category → target

Every distinct IDAH category (tree-path id) maps to its label and attribute
values. `-` is the "not applicable" placeholder the schema uses. From
[`label_map.example.json`](label_map.example.json):

```json
{
  "vehicle/civilian/car/suv":    { "label": "vehicle_civilian", "L2": "civilian", "L3": "car", "L4": "suv" },
  "vehicle/civilian/car/pickup": { "label": "vehicle_civilian", "L2": "civilian", "L3": "car", "L4": "pickUp" },
  "vehicle/civilian/crane":      { "label": "vehicle_construction", "L2": "civilian", "L3": "crane", "L4": "-" }
}
```

Two things the example shows: `car/pickup` maps its value to the schema's
camelCase `pickUp` (spellings need not match the IDAH segment), and the
`vehicle/civilian/crane` branch routes to a *different* label
(`vehicle_construction`) than its `vehicle/civilian/...` siblings — a regrouping
the paths alone don't imply.

Checkbox attributes are **not** listed here — they come from the IDAH annotation
at export time (falling back to the schema's default), so only `L2`/`L3`/`L4` go
in the map.

## Building the configs for a new dataset

### 1. Get the client schema
Drop the client's CVAT label JSON in as `label_schema.json`. It is authoritative
for the `<labels>` block — do not hand-edit its labels/values. To start from the
committed example instead:

```bash
mkdir -p configs/<program>_<dataset>
cp configs/label_schema.example.json configs/<program>_<dataset>/label_schema.json
cp configs/label_map.example.json    configs/<program>_<dataset>/label_map.json
```

### 2. List the distinct IDAH categories
From an existing export (or the UPD directly):

```bash
grep -oE '<name>[^<]+</name>' path/to/annotations.xml \
  | sed -E 's/<\/?name>//g' | grep '/' | sort -u
```

### 3. Write the map, routing each category
Assign each category a `label` + `L2/L3/L4`. The routing is **client-specific** —
different datasets have needed different rules. Patterns to watch for:

- **Regrouping** — a subtree can route to a *different* label than its siblings
  (e.g. some `x/civilian/…` branches split off into their own label), so the same
  path prefix does not always mean the same label.
- **Which level holds the discriminator** — one label may put the meaningful type
  in `L2`, another in `L3`/`L4`; it is not a fixed positional slice of the path.
- **Value spelling** — the schema often camelCases (`heavytrailer` →
  `heavyTrailer`); map to the schema's exact value, not the raw IDAH segment.
- **Placeholder segments** — literal `-` / `--` in a path collapse to the
  schema's `-`.

A positional first pass (`label = level0_level1`, `L2 = level1`, `L3 = level2`,
`L4 = level3`, `-` where absent) is a useful scaffold — then correct it by hand
against the schema (value casing, regroupings, splits).

### 4. Validate
The converter validates on load and **fails loudly** if the map references a
label or value the schema does not declare, so a typo never produces a package
CVAT rejects on upload. To check without exporting:

```python
import json
from upd_to_cvat.converter import _validate_label_config
_validate_label_config(json.load(open("label_schema.json")),
                       json.load(open("label_map.json")))   # raises on any problem
```

## Running the export

Convert **one dataset at a time** (one taxonomy per run):

```bash
python3 -m upd_to_cvat \
  --upd <idah-export>.upd \
  --dataset-id <dataset-id> \
  --output cvat-export \
  --label-schema configs/<program>_<dataset>/label_schema.json \
  --label-map    configs/<program>_<dataset>/label_map.json
```

Omit both flags to fall back to the default flat-label behaviour.

## Importing into CVAT — read this

CVAT does **not** build the label schema from an uploaded annotation file. The
`<labels>` block in the XML is descriptive; on annotation import CVAT only matches
shapes/attributes **by name** against the labels already configured on the
project. If a label/attribute isn't pre-configured, CVAT auto-creates it as a
plain `text` attribute with empty values — which is why imported select
attributes can come back as empty `text`.

So the order matters:

1. Create the CVAT project.
2. Configure its labels from `label_schema.json` (*Labels → Raw*; strip the
   numeric `id`s first).
3. **Then** import the `annotations.xml`.

Because the exporter writes each value onto every shape
(`<attribute name="L2">civilian</attribute>`), the values bind to the
pre-configured `select` attributes and come through correctly.
