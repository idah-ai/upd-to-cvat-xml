# upd-to-cvat-xml

Convert IDAH datasets stored in [UPD](https://github.com/idah-ai/upd-sdk-python)
files into [CVAT 1.1](https://opencv.github.io/cvat/docs/manual/advanced/xml_format/)
annotation packages. The output format is selected automatically per dataset
`modality`:

- **`idah-video`** → *CVAT for video 1.1*: one video per entry, with annotation
  tracks.
  - Supported shapes: bounding box and polygon.
- **`idah-image`** → *CVAT for images 1.1*: the whole dataset as a single task,
  with one image per entry.
  - Supported shapes: bounding box, polygon, ellipse, circle, and line.

### Labels

CVAT label `<name>`s are the IDAH tree-path **ids** (`human/military/inwater/armed`),
not the leaf names — IDAH labels are a tree, so the same leaf name recurs under
different branches and only the id is unique.

The Labeling-Configuration is keyed by *shape type*, and the same label id is
routinely declared under several of them, so those declarations get merged into
the one label CVAT allows per name:

| | |
| --- | --- |
| `<type>` | The CVAT type of the shape the label is declared for: `bounding-box` → `rectangle`, `polygon` → `polygon`, `line` → `polyline`, `ellipse`/`circle` → `ellipse`. A label declared under **several** shape types can't be constrained to one and becomes `any`. |
| `<color>` | From the first shape type in sorted order that supplies one. |

The colour rule matters because IDAH often gives one label a *different* colour
per shape type — `car` is `#FFEC16` as a box but `#00A6F5` as a polygon. The
source is genuinely ambiguous there, so the rule only has to be deterministic;
it previously fell out of dict ordering and effectively picked at random.

**Remapping to a client label schema.** The flat tree-path labels above are the
default. Passing `--label-schema` / `--label-map` instead emits a small set of
client-defined top-level labels, with the sub-classes carried as `L2`/`L3`/`L4`
`select` attributes (plus optional checkboxes) on every shape. See
[`configs/README.md`](configs/README.md) for the two-file format, how to build a
mapping for a new dataset, and the CVAT import steps that make the attributes
come through.

### Occlusion

IDAH stores occlusion on the annotation as `attributes.occlusion`, which maps
onto CVAT's built-in `occluded` flag:

| IDAH `occlusion` | CVAT |
| ---------------- | ---- |
| `"Partial"`, `"FULL"` (also `["Partial"]`, any case) | `occluded="1"` |
| `""`, `[""]`, attribute absent | `occluded="0"` |

Two things are worth knowing about this mapping:

- **It is per annotation row**, i.e. per *segment* of a track (see Tracks
  below), not per frame. IDAH's per-frame records hold only
  `frame`/`points`/`angle`, so every shape a row produces gets that row's value —
  though a track assembled from several rows can change occlusion between them.
- **`FULL` does not become `outside="1"`.** "Outside" means the object is absent
  from the frame and drops its geometry, whereas a fully-occluded IDAH
  annotation still carries the box the annotator drew.

The Partial/FULL distinction is not preserved — CVAT's `occluded` is a boolean.

### Tracks, `Group-Id` and `outside`

A CVAT `<track>` is the identity of one object over time. IDAH splits that
identity across several annotation rows tied together by **`Group-Id`**, so the
rows are reassembled into a single track, ordered by start frame:

```
IDAH   Group-Id G:  9..13 │ 14..28 │ 29..33 │ 34..50     4 rows, one category
CVAT   <track id="9" label="being/undefined/undefined">  1 track, frames 9..50
```

The grouping is **leader/follower**, not a shared token: the object's first row
carries no `Group-Id` at all, and every later row carries the leader's own
annotation `id` as its `Group-Id`. A row's group is therefore its `Group-Id`
when it has one and its own `id` when it does not — so the leader joins the
group it heads instead of becoming a track of its own.

```
row 019f460c…  (no Group-Id)              0..8    ┐ one object,
row 019f4649…  Group-Id: 019f460c…        9..50   ┘ one track 0..50
```

A `Group-Id` naming a row that is not in the entry (a deleted leader) stands
alone as its own track.

A group is split by **category**, though. An IDAH group can carry rows of more
than one category — an object annotated as one thing, then reclassified as
another — and a CVAT track holds exactly one label, so each category in the
group becomes its own track:

```
IDAH   Group-Id G:  24..28 tobedefined │ 29..50 transported   1 group, 2 categories
CVAT   <track label="…/tobedefined/-">  frames 24..28  +  <track label="…/transported/-">  frames 29..50
```

A track's shapes **persist forward** until the next one overrides them, so a
track needs an explicit `outside="1"` marker to say "the object is not here from
this frame on" — otherwise its last box stays painted on every remaining frame.
Two cases produce one:

- **A gap between rows.** When the object leaves and comes back (`5..9`, then
  `23..25`), `outside="1"` is emitted at frame 10 and the next row reopens the
  track at 23 with a fresh keyframe. Nothing is emitted in between.
- **The end of the track**, one frame past its last — skipped when the track
  already runs to the final frame of the video, where there is no frame to put
  the marker on.

The `outside` shape repeats the previous shape's geometry (CVAT wants
coordinates but never draws them) and is always `keyframe="1"` so import keeps
it. Gaps and terminators are resolved per track, i.e. per category within a
split group.

## Export levels

CVAT dumps annotations at three levels — **project**, **task** and **job** —
which differ in their `<meta>` block and, at project level, in how frames are
numbered. IDAH has only two levels (dataset → entry), so they map like this:

| IDAH | CVAT (`idah-video`) | CVAT (`idah-image`) |
| ---- | ------------------- | ------------------- |
| dataset | project | project *and* its single task |
| entry | task, with one job | one `<image>` |

The level follows the filter — the deeper you filter, the lower the level:

| Filter | Level | Output |
| ------ | ----- | ------ |
| *(none)* | project | one `annotations.xml` per dataset, all entries merged |
| `--dataset-id` | task | one per entry (video) / one per dataset (image) |
| `--entry-id` | job | one `annotations.xml` for that entry |

```
# project (default)
cvat-export/project_<dataset>/annotations.xml
                              images/<entry>/frame_000000.PNG      # --with-images

# task — --dataset-id
cvat-export/<dataset>/<entry>_<media-id>/annotations.xml           # idah-video
                                         images/frame_000000.PNG   # --with-images
cvat-export/<dataset>/annotations.xml                              # idah-image
                      images/<name>.jpg                            # --with-images

# job — --entry-id
cvat-export/<dataset>/job_<entry>_<media-id>/annotations.xml
                                             images/frame_000000.PNG
```

### Subsets at project level

At **project** level each entry gets its own **subset**, and its frames live in
`images/<subset>/`. This matters on import: **CVAT creates one task per subset**,
and it ignores the `<meta><project><tasks>` list when reconstructing tasks. A
project whose tasks all sit in one subset therefore collapses back into a single
task on import — which is what CVAT's *own* project export produces, so a real
CVAT project dump does not round-trip either. Giving each entry a distinct subset
is what makes an *N*-entry dataset come back as *N* tasks.

Because each subset is an independent task, frames are numbered per task
(starting at 0), not project-wide. Track ids stay unique across the whole
project, which also makes them unique within each task.

> When zipping the export for CVAT, use `zip -r -X out.zip <dir>` rather than
> Finder — Finder adds `__MACOSX/._*` resource-fork entries that double the file
> count.

### Splitting a project into parts

A whole dataset in one project package is a single upload — with `--with-images`
a real one runs to ~13 GB, which takes far too long to push to CVAT in one go.
`--split N` caps how many entries go into one package, so the dataset comes out
as several packages that are zipped and uploaded one at a time:

```bash
upd-to-cvat --upd idah-export.upd --output cvat-export --with-images --split 20
```

```
cvat-export/project_<dataset>_part1of3/annotations.xml
                                       images/<entry>/frame_000000.PNG
cvat-export/project_<dataset>_part2of3/…
cvat-export/project_<dataset>_part3of3/…
```

Each part is a **complete, independent CVAT project** — its own `<labels>`,
its own tasks numbered from 0, its own subsets and track ids — so it imports on
its own and the parts arrive in CVAT as separate projects. The part index is
zero-padded to the width of the total (`_part03of12`) so the folders sort in
order, and it is appended to the project `<name>` as well, so an imported
project says which slice it is. Entries keep their original order and each
lands in exactly one part; the last part holds the remainder.

`--split` only applies at **project** level, since that is the only level whose
package holds every entry at once — task and job level already write one package
per entry. Passing it with `--dataset-id`/`--entry-id` is reported and ignored.
A dataset with no more entries than `N` is written unsplit, with no part suffix.

Because subsets are the only thing a dataset import can split tasks on, CVAT
groups the imported task list under one heading per subset. There is no way to
get *N* tasks that all sit in a single subset from a dataset import — subset and
task are the same knob. Projects with a flat task list were built the other way,
by creating each task in CVAT and importing its annotations at task level.

---

## Requirements

- **Python ≥ 3.14**

## Dependencies

All are installed automatically by `pip install .` (see below).

- [**upd-sdk-python**](https://github.com/idah-ai/upd-sdk-python) — the IDAH SDK used to read
  the input UPD files. Not on PyPI; pulled from its public GitHub repo over HTTPS.
- [**PyAV**](https://pyav.org) (`av`) — probes media dimensions and frame counts
  (CVAT XML uses absolute pixel coordinates) and extracts video frames for
  `--with-images`. Its wheel bundles the ffmpeg libraries, so no system ffmpeg is
  required.
- [**NumPy**](https://numpy.org) (`numpy`) — array maths underpinning coordinate
  and shape processing.
- [**pyflubber**](https://pypi.org/project/pyflubber/) — flubber-equivalent
  polygon morphing used to interpolate polygon shapes between keyframes.

## Installation

```bash
python3.14 -m venv .venv      # or your Python 3.14 interpreter
source .venv/bin/activate
pip install .                 # installs this package + the dependencies
```

## Usage

```bash
# annotations only — project level, every dataset
upd-to-cvat --upd idah-export.upd --output cvat-export

# also extract every video frame as PNG
upd-to-cvat --upd idah-export.upd --output cvat-export --with-images

# split the project into parts of 20 entries — several smaller uploads
upd-to-cvat --upd idah-export.upd --output cvat-export --with-images --split 20

# task level: one dataset, one annotations.xml per entry
upd-to-cvat --upd idah-export.upd --output cvat-export --dataset-id <dataset-id>

# job level: a single entry
upd-to-cvat --upd idah-export.upd --output cvat-export --entry-id <entry-id>
```

| Flag              | Description                                                            |
| ----------------- | --------------------------------------------------------------------- |
| `--upd`           | Input UPD file (required).                                            |
| `--output`        | Output root directory (default `cvat-export`).                        |
| `--with-images`   | Video: extract frames as `images/frame_%06d.PNG`. Images: copy the source images into `images/`. |
| `--no-clamp`      | Keep raw coordinates instead of clamping each shape to the media bounds. By default shapes are clipped to `[0, width] × [0, height]`, since normalised IDAH points can drift slightly outside `[0, 1]`. |
| `--dataset-id`    | Export only this dataset, at **task** level.                          |
| `--entry-id`      | Export only this entry, at **job** level.                             |
| `--split N`       | Split a **project**-level export into packages of at most `N` entries (`project_<dataset>_partNNofMM`), so a large dataset uploads as several smaller batches. Ignored at task/job level. |
| `--label-schema` / `--label-map` | Remap IDAH tree-path labels to a client CVAT label schema with `L2`/`L3`/`L4` attributes. Given together; see [`configs/README.md`](configs/README.md). |

Equivalent module form: `python -m upd_to_cvat --upd …`.

### Programmatic use

```python
from upd_to_cvat import run

run("idah-export.upd", "cvat-export", with_images=False)     # project level
run("idah-export.upd", "cvat-export", dataset_id="…")        # task level
run("idah-export.upd", "cvat-export", entry_id="…")          # job level
run("idah-export.upd", "cvat-export", split=20)              # project, in parts
```

## Tests

The suite in [`tests/`](tests/) covers the pure conversion logic — interpolation,
coordinate clamping / polygon clipping, and the CVAT XML builders (media probing
and frame extraction, which need PyAV and real blobs, are out of scope).

```bash
pip install ".[dev]"          # installs pytest
pytest
```

---

## Viewer

The [`viewer/`](viewer/) directory contains a small web application for quickly
previewing the generated CVAT exports before delivery. It provides a
frame-by-frame player that overlays the annotations on the exported images.

```bash
cd viewer
npm install
npm run dev          # opens at http://localhost:5180/
```

See [`viewer/README.md`](viewer/README.md) for full details.
