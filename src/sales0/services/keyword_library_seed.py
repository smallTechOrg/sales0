"""One-time top-up of the suggested phrases for databases seeded before library v2.

``seed_suggested_once`` seeds the first 60 phrases once ever (``keywords_seeded_v1``). Phrases added
later live in ``SUGGESTED_KEYWORDS_V2``; ``seed_new_phrases`` adds them exactly once
(``app_meta.keywords_seeded_v2``) to a database that took the suggested set, never duplicating a row
that matches by suggested key or by text (a user's own phrase) and never re-adding a phrase the
user deleted afterwards (the marker makes it one-shot). Call it right after ``seed_suggested_once``
at startup. Returns how many phrases were added.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from sales0.config import get_settings
from sales0.db.models import AppMeta, Keyword
from sales0.logging import get_logger
from sales0.services.keyword_library import SUGGESTED_KEYWORDS_V2

log = get_logger("sales0.services.keyword_library_seed")

V1_META_KEY = "keywords_seeded_v1"
V2_META_KEY = "keywords_seeded_v2"


def seed_new_phrases(db: Session) -> int:
    if not get_settings().seed_suggested_keywords:
        return 0
    if db.get(AppMeta, V1_META_KEY) is None or db.get(AppMeta, V2_META_KEY) is not None:
        return 0
    keys = {k for k in db.scalars(select(Keyword.suggested_key)).all() if k}
    texts = set(db.scalars(select(Keyword.text_key)).all())
    added = 0
    for item in SUGGESTED_KEYWORDS_V2:
        if item.key in keys or item.text.lower() in texts:
            continue
        db.add(Keyword(text=item.text, active=True, group_name=item.group, suggested_key=item.key))
        keys.add(item.key)
        texts.add(item.text.lower())
        added += 1
    db.add(AppMeta(key=V2_META_KEY, value="1"))
    db.flush()
    log.info("keywords_seeded_v2", added=added)
    return added


__all__ = ["V2_META_KEY", "seed_new_phrases"]
