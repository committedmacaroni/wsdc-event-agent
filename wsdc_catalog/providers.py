"""Scoring providers the agent can read. Each module exposes PROVIDER, PARSER_VERSION and
parse_sheet(html) -> {"event_title", "sections": [...], "errors": [...]}."""
from . import eepro, scoringdance, wdr

PARSERS = {eepro.PROVIDER: eepro, scoringdance.PROVIDER: scoringdance, wdr.PROVIDER: wdr}


def parser_for(provider: str):
    return PARSERS[provider]


def parse_with_context(provider: str, html: str, title: str | None, link_titles: list[str]) -> dict:
    """Some providers (WDR) need the round's title and its sibling rounds to name the round."""
    parser = parser_for(provider)
    if getattr(parser, "WANTS_CONTEXT", False):
        return parser.parse_sheet(html, title=title, link_titles=link_titles)
    return parser.parse_sheet(html)
