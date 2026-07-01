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
    assert args.dataset is None


def test_upd_is_required():
    with pytest.raises(SystemExit):
        parse([])


def test_with_images_flag():
    assert parse(["--upd", "x", "--with-images"]).with_images is True


def test_no_clamp_disables_clamping():
    assert parse(["--upd", "x", "--no-clamp"]).clamp is False


def test_output_and_dataset_overrides():
    args = parse(["--upd", "x", "--output", "out", "--dataset", "ds-1"])
    assert args.output == "out"
    assert args.dataset == "ds-1"


def test_main_delegates_to_run(monkeypatch):
    calls = {}

    def fake_run(upd_path, output, *, with_images, dataset_filter, clamp):
        calls.update(upd_path=upd_path, output=output, with_images=with_images,
                     dataset_filter=dataset_filter, clamp=clamp)

    monkeypatch.setattr(cli, "run", fake_run)
    cli.main(["--upd", "in.upd", "--output", "out", "--with-images",
              "--no-clamp", "--dataset", "d1"])
    assert calls == {
        "upd_path": "in.upd", "output": "out", "with_images": True,
        "dataset_filter": "d1", "clamp": False,
    }
