"""The latest snapshot of each registry table, read straight from Parquet with DuckDB (no warehouse file).

A registry folder has the layout of data/tables: <dataset>/<table>/<run_id>.parquet (or <run_id>__part-NNNNN.parquet).
The latest run of a table is its greatest run_id, as tables.latest() takes it; here it is read off the file names
(tables.write names each file after its run), so only the latest run's files are opened.

Which folder: the one DDWB_REGISTRY_DIR names; else data/tables when it holds the registry (a build of this repo);
else the published release (ddwb.registry.export_release, attached to a GitHub release of this repo), downloaded once on
first use into the user's cache (DDWB_CACHE_DIR, else XDG_CACHE_HOME/ddwb, else ~/.cache/ddwb), its sha256 checked.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import duckdb
import httpx

from .. import paths

ENV = "DDWB_REGISTRY_DIR"
VIEWS = {  # view name -> (dataset, table)
    "bodies": ("entities", "bodies"), "body_records": ("entities", "body_records"), "identifiers": ("entities", "identifiers"),
    "websites": ("entities", "websites"), "relations": ("entities", "relations"), "records": ("standard", "records"),
    "code_states": ("standard", "code_states"), "name_keys": ("registry", "name_keys"),
}
REQUIRED = ("bodies", "body_records", "identifiers", "websites", "records", "code_states")
RUN_COLUMNS = ("run_id", "written_at")


RELEASE = {  # the registry a fresh install downloads (python -m ddwb.registry --export, packed as one .tar.gz)
    "tag": "registry-20261106",
    "url": "https://github.com/shrey-hibe/deep-document-web-benchmark/releases/download/registry-20261106/registry.tar.gz",
    "sha256": "ef03339871605fb1e4016ca10713638b1fe54270d228949cc78aa864f337d168",
    "mb": 34,
}


def has_tables(root: Path) -> bool:
    return all(latest(root, *VIEWS[v]) for v in REQUIRED)


def cache_dir() -> Path:
    base = os.environ.get("DDWB_CACHE_DIR") or (Path(os.environ["XDG_CACHE_HOME"]) / "ddwb" if os.environ.get("XDG_CACHE_HOME")
                                                else Path.home() / ".cache" / "ddwb")
    return Path(base).expanduser()


def default_root() -> Path:
    if os.environ.get(ENV):
        return Path(os.environ[ENV])
    if has_tables(paths.TABLES):
        return paths.TABLES
    return downloaded()


def _fetch(url: str, out: Path) -> None:
    from ..providers.safety import USER_AGENT
    with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=120.0) as r:
        r.raise_for_status()
        with out.open("wb") as handle:
            for chunk in r.iter_bytes():
                handle.write(chunk)


def downloaded(release: dict | None = None, fetch=None) -> Path:
    """The published registry in the user's cache, downloaded and unpacked the first time (sha256 checked; a file
    that does not match is deleted and nothing is unpacked)."""
    release, fetch = release or RELEASE, fetch or _fetch
    dest = cache_dir() / "registry" / release["tag"]
    if has_tables(dest):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading the body registry ({release['mb']} MB, once) from {release['url']}", file=sys.stderr)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        archive = Path(tmp) / "registry.tar.gz"
        try:
            fetch(release["url"], archive)
        except Exception as error:
            raise FileNotFoundError(f"no registry here and the download failed ({str(error)[:160]}): set {ENV} to a "
                                    "registry folder, or build data/tables") from error
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != release["sha256"]:
            raise FileNotFoundError(f"the downloaded registry does not match its checksum (got {digest[:12]}, expected "
                                    f"{release['sha256'][:12]}): not used")
        unpacked = Path(tmp) / "unpacked"
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter="data")  # no paths outside the folder, no links out of it
        if not has_tables(unpacked):
            raise FileNotFoundError("the downloaded registry lacks its tables: not used")
        if not has_tables(dest):  # another process may have finished first
            if dest.exists():  # a half-unpacked folder of ours
                shutil.rmtree(dest)
            unpacked.replace(dest)
    return dest


def run_of(file: Path) -> str:
    return file.name.removesuffix(".parquet").split("__part-")[0]


def latest(root: Path, dataset: str, table: str) -> tuple[str, list[Path]] | None:
    """(run_id, files) of the latest run of a table, or None when the table has no files."""
    files = [f for f in (root / dataset / table).glob("*.parquet") if not f.name.startswith(".")]
    if not files:
        return None
    run = max(run_of(f) for f in files)
    return run, sorted(f for f in files if run_of(f) == run)


def scan(files: list[Path]) -> str:
    return f"read_parquet({[str(f) for f in files]!r}, union_by_name = true)"


def open_views(root: Path) -> tuple[duckdb.DuckDBPyConnection, dict[str, str]]:
    """An in-memory DuckDB connection with one view per registry table (its latest run); returns (con, {view: run_id})."""
    con = duckdb.connect()
    runs = {}
    for view, (dataset, table) in VIEWS.items():
        found = latest(root, dataset, table)
        if not found:
            continue
        run, files = found
        columns = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {scan(files)}").fetchall()]
        drop = [c for c in RUN_COLUMNS if c in columns]  # written_at would need pytz to come back to Python
        exclude = f"EXCLUDE ({', '.join(drop)})" if drop else ""
        con.execute(f"CREATE VIEW {view} AS SELECT * {exclude} FROM {scan(files)}")
        runs[view] = run
    missing = [v for v in REQUIRED if v not in runs]
    if missing:
        raise FileNotFoundError(f"registry folder {root} lacks {', '.join('/'.join(VIEWS[v]) for v in missing)} "
                                f"(set {ENV} to a release folder, or build data/tables)")
    return con, runs
