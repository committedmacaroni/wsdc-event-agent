"""Scoring providers the agent can read. Each module exposes PROVIDER, PARSER_VERSION and
parse_sheet(html) -> {"event_title", "sections": [...], "errors": [...]}."""
from . import eepro, scoringdance

PARSERS = {eepro.PROVIDER: eepro, scoringdance.PROVIDER: scoringdance}


def parser_for(provider: str):
    return PARSERS[provider]
