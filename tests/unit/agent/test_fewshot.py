"""select_examples: overrides first, alternating fill, truncation, chronological order."""

from __future__ import annotations

from sales0.agent.fewshot import select_examples, truncate_words
from tests.unit.agent.conftest import example


def _corpus():
    long_text = "word " * 400  # 2000 chars
    overrides = [example(i, "accept", override=True, minutes=i, text=long_text) for i in (1, 2, 3)]
    accepts = [example(10 + i, "accept", minutes=10 + i, text=long_text) for i in range(5)]
    discards = [example(20 + i, "discard", minutes=20 + i, text=long_text) for i in range(5)]
    return overrides, accepts, discards


def test_three_overrides_then_alternating_discard_accept_discard_in_chronological_order() -> None:
    overrides, accepts, discards = _corpus()
    picked = select_examples(overrides + accepts + discards, max_n=6, text_chars=600)

    assert len(picked) == 6
    assert sum(e.is_override for e in picked) == 3
    ids = [e.enquiry_id for e in picked]
    # newest discard (24), newest accept (14), next discard (23); overrides 1,2,3
    assert sorted(ids) == [1, 2, 3, 14, 23, 24]
    # Pick order was discard(24) → accept(14) → discard(23); output is oldest first.
    assert ids == [1, 2, 3, 14, 23, 24]
    assert [e.decided_at for e in picked] == sorted(e.decided_at for e in picked)
    assert [e.decision for e in picked if not e.is_override] == ["accept", "discard", "discard"]
    assert all(len(e.text) <= 600 for e in picked)
    assert all(e.text.endswith("…") for e in picked)


def test_override_cap_is_half_rounded_up() -> None:
    overrides = [example(i, "accept", override=True, minutes=i) for i in range(10)]
    picked = select_examples(overrides, max_n=5, text_chars=600)
    # ceil(5/2) = 3 overrides, then the remaining overrides fill as ordinary accepts.
    assert len(picked) == 5
    assert [e.enquiry_id for e in picked] == [5, 6, 7, 8, 9]


def test_fill_starts_with_the_less_represented_kind() -> None:
    overrides = [example(1, "discard", override=True, minutes=1)]
    accepts = [example(10 + i, "accept", minutes=10 + i) for i in range(3)]
    discards = [example(20 + i, "discard", minutes=20 + i) for i in range(3)]
    picked = select_examples(overrides + accepts + discards, max_n=4, text_chars=600)
    fill_newest_first = sorted([e for e in picked if not e.is_override], key=lambda e: e.decided_at, reverse=True)
    # one discard already picked → accept first, then discard, then accept
    assert {e.enquiry_id for e in fill_newest_first} == {12, 11, 22}
    assert [e.decision for e in picked].count("accept") == 2


def test_when_one_kind_runs_out_the_other_fills() -> None:
    accepts = [example(10 + i, "accept", minutes=10 + i) for i in range(5)]
    discards = [example(20, "discard", minutes=20)]
    picked = select_examples(accepts + discards, max_n=6, text_chars=600)
    assert len(picked) == 6
    assert [e.decision for e in picked].count("discard") == 1


def test_cold_start_and_zero_cap_return_nothing() -> None:
    assert select_examples([], max_n=6, text_chars=600) == []
    assert select_examples([example(1, "accept")], max_n=0, text_chars=600) == []


def test_fewer_candidates_than_slots_returns_all_of_them() -> None:
    picked = select_examples([example(2, "discard", minutes=2), example(1, "accept", minutes=1)], 6, 600)
    assert [e.enquiry_id for e in picked] == [1, 2]


def test_truncate_words_cuts_on_a_word_boundary_with_ellipsis() -> None:
    out = truncate_words("alpha beta gamma delta", 13)
    assert out == "alpha beta…"
    assert len(out) <= 13
    assert truncate_words("short", 600) == "short"


def test_truncate_words_hard_cuts_a_single_huge_word() -> None:
    out = truncate_words("x" * 1000, 600)
    assert len(out) == 600 and out.endswith("…")


def test_select_examples_does_not_mutate_the_candidates() -> None:
    cand = example(1, "accept", text="word " * 300)
    select_examples([cand], 6, 100)
    assert len(cand.text) == len("word " * 300)
