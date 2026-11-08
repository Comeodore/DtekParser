"""Pages that came without their schedule are kept, to see what DTEK showed."""
import os

from app.dtek.client import DtekSite


class FakePage:
    def __init__(self, html):
        self.html = html

    async def content(self):
        return self.html


async def test_snapshots_keep_the_newest(tmp_path, monkeypatch):
    site = DtekSite(None, "krem", "https://x", "s", None, state_path=str(tmp_path / "state-krem.json"))
    monkeypatch.setattr(DtekSite, "SNAPSHOTS_KEPT", 2)
    stamps = iter(["20261108-220001", "20261108-220101", "20261108-220201"])
    monkeypatch.setattr("app.dtek.client.time.strftime", lambda fmt: next(stamps))
    for n in range(3):
        await site._snapshot(FakePage(f"<html>{n}</html>"))
    folder = tmp_path / "snapshots"
    assert sorted(os.listdir(folder)) == ["krem-20261108-220101.html", "krem-20261108-220201.html"]
    assert (folder / "krem-20261108-220201.html").read_text(encoding="utf-8") == "<html>2</html>"
