from __future__ import annotations

from sales0.worker import protocol as p
from sales0.worker.runner import MAX_BATCH_BYTES, batches, post_payload
from tests.integration.services.helpers import post


def payloads(n: int, text: str = "x") -> list[p.PostPayload]:
    return [post_payload(post(i % 90, text + str(i))) for i in range(n)]


def test_batches_respect_the_post_count() -> None:
    out = batches(payloads(53), 25)
    assert [len(b) for b in out] == [25, 25, 3]


def test_batches_respect_the_byte_budget() -> None:
    big = payloads(20, "y" * 55_000)  # ~55 KB each (the wire cap is 60 KB of text) -> at most a handful per request
    out = batches(big, 200)
    assert sum(len(b) for b in out) == 20 and len(out) > 1
    assert all(sum(len(x.model_dump_json()) for x in b) <= MAX_BATCH_BYTES + 60_000 for b in out)
    assert all(len(b) >= 1 for b in out)


def test_no_posts_means_no_batches() -> None:
    assert batches([], 25) == []


def test_post_payload_round_trips_the_scraped_post() -> None:
    src = post(7, "Acme wants a workshop")
    wire = post_payload(src)
    assert (wire.source_ref, wire.text, wire.posted_date, wire.link_kind) == (src.source_ref, src.text, src.posted_date, "post")
    assert p.PostPayload.model_validate_json(wire.model_dump_json()) == wire


def test_overlong_values_are_clipped_not_rejected() -> None:
    src = post(7, "z" * 200_000)
    wire = post_payload(src)
    assert len(wire.text) == 60_000
