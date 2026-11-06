"""Where the repo reads and writes: two roots set by the environment, defaults that keep today's layout, and stored
document paths that resolve against the work root."""

import json
import os
import subprocess
import sys
from pathlib import Path

from ddwb import paths
from ddwb.deep import web

SHOW = ("import json; from ddwb import paths; from ddwb.deep import chain, forks, state, truth; print(json.dumps({"
        "'repo': str(paths.REPO), 'data': str(paths.DATA), 'work': str(paths.WORK), 'docs': str(paths.DOCS), "
        "'manifest': str(paths.MANIFEST), 'discovery': str(paths.DISCOVERY), 'downloads': str(paths.DOWNLOADS), "
        "'nav': str(state.NAV), 'chain': str(chain.DISCOVERY), 'forks': str(forks.FORKS_DIR), 'truth': str(truth.WORK), "
        "'truth_text': str(truth.TEXT)}))")


def layout(**env) -> dict:
    """The paths as a fresh process sees them (importing writes nothing)."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DDWB_")} | env
    out = subprocess.run([sys.executable, "-c", SHOW], env=clean, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_the_defaults_keep_todays_layout():
    p = layout()
    repo = Path(p["repo"])
    assert p["data"] == str(repo / "data") and p["nav"] == str(repo / "data" / "nav")
    assert p["truth_text"] == str(repo / "data" / "truth")
    assert p["work"] == str(repo.parent)
    assert p["docs"] == str(repo.parent / "data" / "deep") and p["manifest"] == str(repo.parent / "data-manifest.tsv")
    assert p["downloads"] == str(repo.parent / "data" / "registries")
    assert p["discovery"] == p["chain"] == str(repo.parent / "work" / "deep-discovery")
    assert p["forks"] == str(repo.parent / "work" / "deep-discovery" / "forks")
    assert p["truth"] == str(repo.parent / "work" / "deep-discovery" / "truth")


def test_the_environment_moves_both_roots(tmp_path):
    p = layout(DDWB_DATA_DIR=str(tmp_path / "d"), DDWB_WORK_DIR=str(tmp_path / "w"))
    assert p["data"] == str(tmp_path / "d") and p["nav"] == str(tmp_path / "d" / "nav")
    assert p["docs"] == str(tmp_path / "w" / "data" / "deep") and p["manifest"] == str(tmp_path / "w" / "data-manifest.tsv")
    assert p["truth"] == str(tmp_path / "w" / "work" / "deep-discovery" / "truth")
    assert p["downloads"] == str(tmp_path / "w" / "data" / "registries")
    assert layout(DDWB_DOWNLOADS=str(tmp_path / "r"))["downloads"] == str(tmp_path / "r")


def test_a_download_is_stored_relative_to_the_work_root_and_found_again():
    rel, sha = web.save_download("cortez-co", "2025-03-11-minutes-ab12cd34.pdf", b"%PDF-1.7 minutes")
    assert rel == "data/deep/cortez-co/2025-03-11-minutes-ab12cd34.pdf"  # the form the nav stores already hold
    assert paths.stored(rel).read_bytes() == b"%PDF-1.7 minutes" and paths.stored(rel).is_relative_to(paths.WORK)
    web.save_download("cortez-co", "2025-03-11-minutes-ab12cd34.pdf", b"%PDF-1.7 minutes")
    lines = paths.MANIFEST.read_text().splitlines()
    assert lines[0] == "path\tbytes\tsha256" and lines[1:] == [f"{rel}\t16\t{sha}"]  # listed once


def test_no_module_builds_a_path_outside_the_two_roots():
    src = Path(paths.__file__).parent
    for f in src.rglob("*.py"):
        if f.name != "paths.py":
            assert "REPO.parent" not in f.read_text(), f
