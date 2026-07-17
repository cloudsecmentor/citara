from __future__ import annotations

BEMA_SOURCE_TREE_SLUG = "bema"
TEXTINUS_SOURCE_TREE_SLUG = "textinus"

BEMA_ORG_ENTITY = {
    "type": "organization",
    "slug": "bema-discipleship",
    "label": "BEMA Discipleship",
    "role": "publisher",
    "provenance": "source_config",
    "aliases": ["BEMA", "BEMA Podcast", "The BEMA Podcast"],
}

TEXTINUS_ORG_ENTITY = {
    "type": "organization",
    "slug": "text-in-us",
    "label": "Text in Us",
    "role": "publisher",
    "provenance": "source_config",
    "aliases": ["Text in Us", "TextInUs"],
}

ELLE_ENTITY_BASE = {
    "type": "person",
    "slug": "elle-grover-fricks",
    "label": "Elle Grover Fricks",
    "provenance": "source_config",
    "aliases": ["Elle", "Elle Grover", "Elle Fricks"],
}

MARTY_ENTITY = {
    "type": "person",
    "slug": "marty-solomon",
    "label": "Marty Solomon",
    "role": "host",
    "provenance": "source_config",
}

BRENT_ENTITY = {
    "type": "person",
    "slug": "brent-billings",
    "label": "Brent Billings",
    "role": "host",
    "provenance": "source_config",
}


def elle_entity(role: str) -> dict:
    return {**ELLE_ENTITY_BASE, "role": role}


BEMA_ENTITIES = [
    BEMA_ORG_ENTITY,
    MARTY_ENTITY,
    BRENT_ENTITY,
    elle_entity("co_host"),
]

TEXTINUS_ENTITIES = [
    TEXTINUS_ORG_ENTITY,
    elle_entity("co_host"),
]
