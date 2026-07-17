from __future__ import annotations


def test_transcript_ingestion_attaches_person_and_org_entities(db_session):
    from sqlalchemy import select

    from citara.core.entities import list_source_entities
    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.models import Entity, EntityAlias, SourceEntity

    source = add_transcript_source(
        db_session,
        payload={
            "show_title": "BEMA Podcast",
            "episode_title": "BEMA Entity Test",
            "episode_url": "https://example.com/bema-entity-test",
            "entities": [
                {
                    "type": "organization",
                    "slug": "bema-discipleship",
                    "label": "BEMA Discipleship",
                    "role": "publisher",
                    "aliases": ["BEMA"],
                },
                {"type": "person", "slug": "marty-solomon", "label": "Marty Solomon", "role": "host", "aliases": ["Marty"]},
                {"type": "topic", "slug": "isaiah", "label": "Isaiah", "role": "theme"},
            ],
            "segments": [{"start_ms": 0, "end_ms": 1000, "text": "Marty talks about Isaiah."}],
        },
    )

    entity_rows = db_session.execute(select(Entity).order_by(Entity.entity_type, Entity.slug)).scalars().all()
    assert [(row.entity_type, row.slug, row.name) for row in entity_rows] == [
        ("organization", "bema-discipleship", "BEMA Discipleship"),
        ("person", "marty-solomon", "Marty Solomon"),
    ]
    assert db_session.execute(select(EntityAlias).where(EntityAlias.alias == "BEMA")).scalar_one().entity_id == entity_rows[0].id
    assert db_session.execute(select(SourceEntity)).scalars().all()
    assert [entity["slug"] for entity in list_source_entities(db_session, source.id)] == ["bema-discipleship", "marty-solomon"]


def test_keyword_search_can_filter_by_person_or_org_while_query_remains_text_based(db_session):
    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.retrieval.keyword import search_knowledge

    add_transcript_source(
        db_session,
        payload={
            "show_title": "BEMA Podcast",
            "episode_title": "Marty Sabbath",
            "entities": [{"type": "person", "slug": "marty-solomon", "label": "Marty Solomon", "role": "host"}],
            "segments": [{"start_ms": 0, "text": "Sabbath is resistance and rest."}],
        },
    )
    add_transcript_source(
        db_session,
        payload={
            "show_title": "BibleProject",
            "episode_title": "Tim Sabbath",
            "entities": [
                {"type": "person", "slug": "tim-mackie", "label": "Tim Mackie", "role": "host"},
                {"type": "organization", "slug": "bibleproject", "label": "BibleProject", "role": "publisher"},
            ],
            "segments": [{"start_ms": 0, "text": "Sabbath is resistance and rest."}],
        },
    )

    all_results = search_knowledge(db_session, query="Sabbath resistance", limit=10)
    marty_results = search_knowledge(db_session, query="Sabbath resistance", limit=10, entity_slugs=["person:marty-solomon"])
    bibleproject_results = search_knowledge(db_session, query="Sabbath resistance", limit=10, entity_slugs=["organization:bibleproject"])
    missing_results = search_knowledge(db_session, query="Sabbath resistance", limit=10, entity_slugs=["person:unknown"])

    assert {result.source_title for result in all_results} == {"Marty Sabbath", "Tim Sabbath"}
    assert [result.source_title for result in marty_results] == ["Marty Sabbath"]
    assert [result.source_title for result in bibleproject_results] == ["Tim Sabbath"]
    assert missing_results == []


def test_elle_entity_can_span_bema_and_textinus_while_source_tree_filter_separates_corpora(db_session):
    from citara.core.ingestion.transcript import add_transcript_source
    from citara.core.retrieval.keyword import search_knowledge
    from citara.core.source_taxonomy import BEMA_ENTITIES, TEXTINUS_ENTITIES

    add_transcript_source(
        db_session,
        payload={
            "show_title": "The BEMA Podcast",
            "episode_title": "BEMA Elle Episode",
            "metadata_json": {"source_tree_slug": "bema", "source_tree_type": "podcast"},
            "entities": BEMA_ENTITIES,
            "segments": [{"start_ms": 0, "text": "Elle discusses Leviticus and covenant language."}],
        },
    )
    add_transcript_source(
        db_session,
        payload={
            "show_title": "Text in Us",
            "episode_title": "Text in Us Elle Episode",
            "metadata_json": {"source_tree_slug": "textinus", "source_tree_type": "podcast"},
            "entities": TEXTINUS_ENTITIES,
            "segments": [{"start_ms": 0, "text": "Elle discusses Leviticus and covenant language."}],
        },
    )

    elle_results = search_knowledge(
        db_session,
        query="Leviticus covenant",
        entity_slugs=["person:elle-grover-fricks"],
        limit=10,
    )
    bema_only = search_knowledge(
        db_session,
        query="Leviticus covenant",
        entity_slugs=["person:elle-grover-fricks"],
        source_tree_slug="bema",
        limit=10,
    )
    textinus_only = search_knowledge(
        db_session,
        query="Leviticus covenant",
        entity_slugs=["person:elle-grover-fricks"],
        source_tree_slug="textinus",
        limit=10,
    )

    assert {result.source_title for result in elle_results} == {"BEMA Elle Episode", "Text in Us Elle Episode"}
    assert [result.source_title for result in bema_only] == ["BEMA Elle Episode"]
    assert [result.source_title for result in textinus_only] == ["Text in Us Elle Episode"]
