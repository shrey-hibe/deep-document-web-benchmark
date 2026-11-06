"""The registry a fresh install downloads: fetched once into the user's cache, checked against its sha256, unpacked
safely; a copy that does not match is not used. No network: the fetch is a local copy."""

import hashlib
import io
import shutil
import tarfile

import duckdb
import pytest

from ddwb.registry import snapshot


def a_release(tmp_path):
    folder = tmp_path / "release"
    for view in snapshot.REQUIRED:
        dataset, table = snapshot.VIEWS[view]
        (folder / dataset / table).mkdir(parents=True)
        duckdb.sql(f"COPY (SELECT 1 AS x) TO '{folder / dataset / table / '20261031T1117Z-abc.parquet'}' (FORMAT parquet)")
    archive = tmp_path / "registry.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(folder, arcname=".")
    return archive, {"tag": "registry-test", "url": "https://example.test/registry.tar.gz",
                     "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "mb": 1}


def test_downloaded_once_into_the_cache_and_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("DDWB_CACHE_DIR", str(tmp_path / "cache"))
    archive, release = a_release(tmp_path)
    fetched = []

    def fetch(url, out):
        fetched.append(url)
        shutil.copy(archive, out)

    root = snapshot.downloaded(release, fetch)
    assert root == tmp_path / "cache" / "registry" / "registry-test" and snapshot.has_tables(root)
    assert snapshot.downloaded(release, fetch) == root and fetched == [release["url"]]  # the second time: no download


def test_a_copy_that_does_not_match_its_checksum_is_not_used(tmp_path, monkeypatch):
    monkeypatch.setenv("DDWB_CACHE_DIR", str(tmp_path / "cache"))
    archive, release = a_release(tmp_path)
    with pytest.raises(FileNotFoundError, match="checksum"):
        snapshot.downloaded(release | {"sha256": "0" * 64}, lambda url, out: shutil.copy(archive, out))
    assert not snapshot.has_tables(tmp_path / "cache" / "registry" / "registry-test")


def test_an_archive_reaching_outside_its_folder_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("DDWB_CACHE_DIR", str(tmp_path / "cache"))
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("../../escaped.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))
    release = {"tag": "bad", "url": "https://example.test/bad.tar.gz", "sha256": hashlib.sha256(bad.read_bytes()).hexdigest(), "mb": 1}
    with pytest.raises(tarfile.TarError):
        snapshot.downloaded(release, lambda url, out: shutil.copy(bad, out))
    assert not (tmp_path / "escaped.txt").exists()


def test_a_failed_download_says_how_to_point_at_a_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DDWB_CACHE_DIR", str(tmp_path / "cache"))
    _, release = a_release(tmp_path)

    def down(url, out):
        raise OSError("network is unreachable")
    with pytest.raises(FileNotFoundError, match="DDWB_REGISTRY_DIR"):
        snapshot.downloaded(release, down)
