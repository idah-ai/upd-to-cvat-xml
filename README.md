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

Equivalent module form: `python -m upd_to_cvat --upd …`.

### Programmatic use

```python
from upd_to_cvat import run

run("idah-export.upd", "cvat-export", with_images=False)     # project level
run("idah-export.upd", "cvat-export", dataset_id="…")        # task level
run("idah-export.upd", "cvat-export", entry_id="…")          # job level
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
