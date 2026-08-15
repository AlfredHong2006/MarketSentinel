from pathlib import Path

import marketsentinel.constituents as constituent_module
from marketsentinel.constituents import WikipediaConstituentService


class FakeResponse:
    text = """
        <table id="constituents">
          <tr><th>Symbol</th><th>Security</th></tr>
          <tr><td>BRK.B</td><td>Berkshire Hathaway</td></tr>
        </table>
    """

    def raise_for_status(self) -> None:
        return None


def test_constituent_parser_accepts_wikipedia_table_without_thead(monkeypatch) -> None:
    monkeypatch.setattr(
        constituent_module,
        "_SOURCES",
        {"S&P 500": ("https://example.test", "Symbol", "Security", 1)},
    )
    monkeypatch.setattr(constituent_module.httpx, "get", lambda *args, **kwargs: FakeResponse())
    service = WikipediaConstituentService(Path("unused.json"))

    result = service._fetch_market("S&P 500")

    assert result[0].symbol == "BRK.B"
    assert result[0].yahoo_symbol == "BRK-B"
