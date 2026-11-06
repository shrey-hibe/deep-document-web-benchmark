"""Every test is offline and writes nothing under data/ or the work root: the provider layer's ledger and cache, the
navigation stores, downloaded documents, the data manifest and run notes all go to a temporary folder, and any attempt to
reach a vendor API or an MCP server fails the test."""

import pytest


@pytest.fixture(autouse=True)
def _providers_offline(tmp_path_factory, monkeypatch):
    from ddwb.providers import api, core, mcp

    root = tmp_path_factory.mktemp("providers")
    monkeypatch.setattr(core, "LEDGER", root / "ledger.jsonl")
    monkeypatch.setattr(core, "CACHE", root / "cache.sqlite")

    def no_network(*a, **k):
        raise AssertionError("a test tried to reach the network through the provider layer")
    monkeypatch.setattr(api.HttpxTransport, "__call__", no_network)
    monkeypatch.setattr(mcp.SdkConnector, "run", no_network)


@pytest.fixture(autouse=True)
def _work_isolated(tmp_path_factory, monkeypatch):
    from ddwb import paths
    from ddwb.deep import chain, forks, truth, web
    from ddwb.deep import state as nav_state

    work = tmp_path_factory.mktemp("work")
    monkeypatch.setenv("DDWB_BROWSER", "off")  # no test starts a browser unless it says so
    monkeypatch.setattr(paths, "WORK", work)
    monkeypatch.setattr(paths, "DOCS", work / "data" / "deep")
    monkeypatch.setattr(paths, "MANIFEST", work / "data-manifest.tsv")
    monkeypatch.setattr(paths, "DISCOVERY", work / "work" / "deep-discovery")
    monkeypatch.setattr(chain, "DISCOVERY", work / "work" / "deep-discovery")
    monkeypatch.setattr(forks, "FORKS_DIR", work / "work" / "deep-discovery" / "forks")
    monkeypatch.setattr(truth, "WORK", work / "work" / "deep-discovery" / "truth")
    monkeypatch.setattr(truth, "TEXT", work / "data-root" / "truth")
    monkeypatch.setattr(nav_state, "NAV", work / "data-root" / "nav")
    monkeypatch.setattr(web, "CACHE", work / "data-root" / "cache" / "deep")
    return work
