"""Where inputs are read from and where this repo writes: two roots, each set by the environment.

DDWB_DATA_DIR, the data root (default <repo>/data): what this repo writes for itself, not in git: tables, the
    warehouse, the navigation stores, the provider ledger and cache, truth texts, compiled helpers.
DDWB_WORK_DIR, the work root (default the folder that holds the repo): the work around the repo. Documents downloaded
    while navigating go to data/deep/<body>/ under it and are listed in its data-manifest.tsv; the agencies' registry
    files are read from data/registries/ (DDWB_DOWNLOADS points elsewhere); run notes, truth sets and scores go to
    work/deep-discovery/. Stores keep a document's path relative to this root ("data/deep/cortez-co/<file>.pdf"), so a
    store stays valid when the root moves.
"""

import os
from pathlib import Path


def _root(env: str, default: Path) -> Path:
    value = os.environ.get(env, "").strip()
    return Path(value).expanduser().absolute() if value else default


REPO = Path(__file__).resolve().parents[2]

DATA = _root("DDWB_DATA_DIR", REPO / "data")
TABLES = DATA / "tables"  # data/tables/<dataset>/<table>/<run_id>.parquet
CACHE = DATA / "cache"  # files unpacked from downloads (e.g. a spreadsheet inside a zip)
WAREHOUSE = DATA / "warehouse.duckdb"

WORK = _root("DDWB_WORK_DIR", REPO.parent)
DOCS = WORK / "data" / "deep"  # documents the navigation tools download, one folder per body
MANIFEST = WORK / "data-manifest.tsv"  # every downloaded file: path (relative to WORK), bytes, sha256
DISCOVERY = WORK / "work" / "deep-discovery"  # discovery traces, forks, truth sets, run scores

# The agencies' files as downloaded, one folder per agency, each with a SOURCES.md (URL, date, checksum). Read only.
DOWNLOADS = _root("DDWB_DOWNLOADS", WORK / "data" / "registries")


def stored(path: str | Path) -> Path:
    """A path kept in a store (relative to the work root) as a file on disk; an absolute path stays as it is."""
    return WORK / path


def to_stored(path: Path) -> str:
    """A file under the work root as the path a store keeps."""
    return str(Path(path).relative_to(WORK))
