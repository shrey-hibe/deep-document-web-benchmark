"""The portal adapter interface and registry, offline."""

from datetime import date

from ddwb.portals import base, registry


def test_an_adapter_registers_detects_and_lists(monkeypatch):
    monkeypatch.setattr(registry, "_FACTORIES", {})
    monkeypatch.setattr(registry, "ADAPTERS", ())  # only the fake here, whatever adapters the registry lists

    @registry.register
    class Fake:
        name, transport = "fakeportal", "http"

        def __init__(self, http):
            self.http = http

        def detect(self, url, html=None):
            return "acme" if "fakeportal.example" in url or (html and "fakeportal.example/acme" in html) else None

        def meetings(self, client, since, until):
            return base.Listing([base.Meeting("1", date(2025, 3, 4), "Council", kind="regular", governing=True),
                                 base.Meeting("2", date(2026, 1, 6), "Council")], notes=["page 3 not read: HTTP 500"])

        def documents(self, client, meeting):
            return [base.Document("https://fakeportal.example/m.pdf", "minutes", meeting_id=meeting.id)]

        def items(self, client, meeting):
            return []

    assert registry.names() == ["fakeportal"]
    assert registry.detect("https://fakeportal.example/acme") == [("fakeportal", "acme")]
    assert registry.detect("https://city.gov/", html='<a href="https://fakeportal.example/acme">Agendas</a>') == [("fakeportal", "acme")]
    a = registry.adapter("fakeportal", http=object())
    assert isinstance(a, base.Portal)
    listing = a.meetings("acme", date(2025, 1, 1), date(2026, 12, 31))
    assert listing.per_year == {2025: 1, 2026: 1} and listing.notes
