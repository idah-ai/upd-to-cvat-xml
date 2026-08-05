"""Specs for the argument parser in :mod:`upd_to_cvat.cli`."""

from __future__ import annotations

import pytest

from upd_to_cvat import cli


def parse(argv):
    return cli.build_parser().parse_args(argv)


def test_defaults():
    args = parse(["--upd", "in.upd"])
    assert args.upd == "in.upd"
    assert args.output == "cvat-export"
    assert args.with_images is False
    assert args.clamp is True                 # clamp on by default
    assert args.dataset_id is None
    assert args.entry_id is None
    assert args.split is None                 # unsplit unless asked


def test_upd_is_required():
    with pytest.raises(SystemExit):
        parse([])


def test_with_images_flag():
    assert parse(["--upd", "x", "--with-images"]).with_images is True


def test_no_clamp_disables_clamping():
    assert parse(["--upd", "x", "--no-clamp"]).clamp is False


def test_output_and_id_filter_overrides():
    args = parse(["--upd", "x", "--output", "out", "--dataset-id", "ds-1",
                  "--entry-id", "e-1"])
    assert args.output == "out"
    assert args.dataset_id == "ds-1"
    assert args.entry_id == "e-1"


def test_split_takes_an_entry_count():
    assert parse(["--upd", "x", "--split", "25"]).split == 25


@pytest.mark.parametrize("value", ["0", "-3", "2.5", "many"])
def test_split_rejects_non_positive_integers(value):
    with pytest.raises(SystemExit):
        parse(["--upd", "x", "--split", value])


def test_main_delegates_to_run(monkeypatch):
    calls = {}

    def fake_run(upd_path, output, *, with_images, dataset_id, entry_id, clamp,
                 split, label_schema, label_map):
        calls.update(upd_path=upd_path, output=output, with_images=with_images,
                     dataset_id=dataset_id, entry_id=entry_id, clamp=clamp,
                     split=split, label_schema=label_schema, label_map=label_map)

    monkeypatch.setattr(cli, "run", fake_run)
    cli.main(["--upd", "in.upd", "--output", "out", "--with-images",
              "--no-clamp", "--dataset-id", "d1", "--split", "10"])
    assert calls == {
        "upd_path": "in.upd", "output": "out", "with_images": True,
        "dataset_id": "d1", "entry_id": None, "clamp": False, "split": 10,
        "label_schema": None, "label_map": None,
    }
