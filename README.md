# upd-to-cvat-xml

Convert IDAH datasets stored in [UPD](https://github.com/idah-ai/upd-sdk-python)
files into [CVAT 1.1](https://opencv.github.io/cvat/docs/manual/advanced/xml_format/)
annotation packages. The output format is selected automatically per dataset
`modality`:

- **`idah-video`** → *CVAT for video 1.1*: one CVAT task folder per video entry,
  with annotation tracks.
- **`idah-image`** → *CVAT for images 1.1*: the whole dataset as a single task,
  with one image per entry.

```
# idah-video
cvat-export/<dataset>/<entry>_<media-id>/annotations.xml
                                         images/frame_000000.PNG   # --with-images

# idah-image
cvat-export/<dataset>/annotations.xml
                      images/<name>.jpg                            # --with-images
```

Supported shapes: bounding box and polygon (video and images); plus ellipse,
circle, and line for images.

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
# annotations only
upd-to-cvat --upd idah-export.upd --output cvat-export

# also extract every video frame as PNG
upd-to-cvat --upd idah-export.upd --output cvat-export --with-images

# limit to a single dataset
upd-to-cvat --upd idah-export.upd --output cvat-export --dataset <dataset-id>
```

| Flag              | Description                                                            |
| ----------------- | --------------------------------------------------------------------- |
| `--upd`           | Input UPD file (required).                                            |
| `--output`        | Output root directory (default `cvat-export`).                        |
| `--with-images`   | Video: extract frames as `images/frame_%06d.PNG`. Images: copy the source images into `images/`. |
| `--no-clamp`      | Keep raw coordinates instead of clamping each shape to the media bounds. By default shapes are clipped to `[0, width] × [0, height]`, since normalised IDAH points can drift slightly outside `[0, 1]`. |
| `--dataset`       | Optional dataset-id filter.                                          |

Equivalent module form: `python -m upd_to_cvat --upd …`.

### Programmatic use

```python
from upd_to_cvat import run

run("idah-export.upd", "cvat-export", with_images=False)
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
