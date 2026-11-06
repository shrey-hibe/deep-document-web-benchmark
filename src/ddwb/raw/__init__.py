"""The raw dataset: each agency's file exactly as published, every value as text, one table per file (or sheet).

Nothing is renamed, cast, mapped or dropped. Each row adds where it came from: the file, the sheet, its row number
in the sheet (1 = first data row), the file's sha256 and the edition.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from .. import catalogue, paths

PROVENANCE = (
    catalogue.Column("_file", "the downloaded file, relative to the downloads folder ('zip!member' for a file inside a zip)", "bookkeeping"),
    catalogue.Column("_sheet", "the spreadsheet tab, or empty for a CSV", "bookkeeping"),
    catalogue.Column("_row", "the row's position among the data rows of its file or tab, from 1", "bookkeeping"),
    catalogue.Column("_file_sha256", "sha256 of the downloaded file, so a row can be traced to the exact bytes", "bookkeeping"),
    catalogue.Column("_edition", "the edition of the file, as the agency names it", "bookkeeping"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def unzip_member(archive: Path, member: str) -> Path:
    """The member of a zip, unpacked once into the cache (keyed by the zip's sha256)."""
    target = paths.CACHE / sha256(archive)[:16] / member
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as z, z.open(member) as src, target.open("wb") as dst:
            dst.write(src.read())
    return target
