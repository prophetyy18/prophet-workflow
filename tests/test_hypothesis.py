"""Tests for the falsifiability checks.

These exist because "falsifiable" is the load-bearing property of the whole
workflow. As prose in a prompt it decays; as a check it does not. If
`check_spec` under-rejects, every slice ships without a real test of anything.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.prophet.__main__ import _slot_values, _vague_hits, check_spec  # noqa: E402

GOOD = """# Current hypothesis

**Round:** 2
**Tier:** 1

**Change:** cache parsed feed entries by URL
**Outcome:** a repeat visit to the same feed renders in under 200ms
**Verify by:** `curl -w '%{time_total}' http://localhost:8000/` twice
"""

BAD_OUTCOME = """**Change:** rewrite the dedup rule
**Outcome:** the page gets noticeably better and feels more robust
**Verify by:** looking at it
"""

MISSING_SLOT = """**Change:** add a filter panel
**Outcome:** the panel appears with 3 options
"""


class TestSlotExtraction(unittest.TestCase):
    def test_all_three_slots_found(self) -> None:
        slots = _slot_values(GOOD)
        self.assertIn("cache", slots["Change"] or "")
        self.assertIn("200ms", slots["Outcome"] or "")
        self.assertIn("curl", slots["Verify by"] or "")

    def test_missing_slot_is_none(self) -> None:
        self.assertIsNone(_slot_values(MISSING_SLOT)["Verify by"])

    def test_colon_variant_accepted(self) -> None:
        spec = "**Change:** x\n**Outcome:** y\n**Verify by:** z\n"
        self.assertEqual(_slot_values(spec)["Outcome"], "y")


class TestVagueDetection(unittest.TestCase):
    def test_detects_multiple_vague_terms(self) -> None:
        terms = {t for t, _, _ in _vague_hits("it gets better and more robust")}
        self.assertIn("better", terms)
        self.assertIn("robust", terms)

    def test_ignores_measurable_wording(self) -> None:
        self.assertEqual(_vague_hits("under 200ms, 12 rows instead of 15"), [])

    def test_detects_placeholders(self) -> None:
        self.assertEqual(len(_vague_hits("TBD and to be determined")), 2)

    def test_reports_line_numbers(self) -> None:
        hits = _vague_hits("first line\nsecond line is better\n")
        self.assertEqual(hits[0][1], 2)

    def test_word_boundary_respected(self) -> None:
        """'goods' and 'goodwill' must not match 'good'."""
        self.assertEqual(_vague_hits("the goods are fine"), [])
        self.assertEqual(_vague_hits("goodwill"), [])

    def test_detects_derived_forms(self) -> None:
        """The word-boundary-only version missed exactly these."""
        for word in ("cleaner", "simpler", "improves", "fastest", "robustness"):
            with self.subTest(word=word):
                self.assertTrue(_vague_hits(f"it is {word}"), word)

    def test_does_not_match_inside_unrelated_words(self) -> None:
        """Genuinely unrelated words must not trip the check."""
        self.assertEqual(_vague_hits("cleaning the bathroom"), [])

    def test_measurable_statements_are_never_flagged(self) -> None:
        """The false-positive direction that would actually hurt."""
        for text in (
            "under 200ms on a repeat visit",
            "12 rows instead of 15",
            "curl -w '%{time_total}' against localhost:8000",
            "3 of 5 sources deduplicate",
            "zero errors from `pytest tests/`",
            "the panel shows exactly 3 options",
            "p99 latency drops from 800ms to 240ms",
            "reduces from 900MB to 120MB",
        ):
            with self.subTest(text=text):
                self.assertEqual(_vague_hits(text), [], text)

    def test_qualitative_comparatives_are_flagged(self) -> None:
        """'fewer errors' is as unmeasurable as 'better'."""
        for text in (
            "fewer errors",
            "more rows",
            "higher latency",
            "smaller output",
            "slower on large feeds",
            "it degrades",
        ):
            with self.subTest(text=text):
                self.assertTrue(_vague_hits(text), text)

    def test_a_number_exempts_a_comparative_on_that_line(self) -> None:
        """The number is what makes the comparison measurable."""
        self.assertEqual(_vague_hits("fewer rows: 8 instead of 20"), [])
        self.assertTrue(_vague_hits("fewer rows"))


class TestPlaceholders(unittest.TestCase):
    """A blank starter spec must NOT pass. That is the worst failure mode."""

    STARTER = """# Current hypothesis

**Round:** _(unset)_
**Tier:** _(1)_

## Hypothesis

**Change:** _(the concrete thing you will do)_
**Outcome:** _(the observable result)_
**Verify by:** _(the exact command or page you open)_
"""

    def test_unfilled_starter_fails(self) -> None:
        errors, _ = check_spec(self.STARTER)
        self.assertEqual(len(errors), 1)
        self.assertIn("Change", errors[0])
        self.assertIn("Outcome", errors[0])
        self.assertIn("Verify by", errors[0])

    def test_partially_filled_fails_on_remaining_slots(self) -> None:
        spec = "**Change:** add a filter\n**Outcome:** _(todo)_\n**Verify by:** _(todo)_\n"
        errors, _ = check_spec(spec)
        self.assertEqual(len(errors), 1)
        self.assertIn("Outcome", errors[0])

    def test_angle_bracket_placeholder_is_missing(self) -> None:
        spec = "**Change:** <do the thing>\n**Outcome:** y\n**Verify by:** z\n"
        errors, _ = check_spec(spec)
        self.assertEqual(len(errors), 1)
        self.assertIn("Change", errors[0])

    def test_todo_marker_is_placeholder(self) -> None:
        spec = "**Change:** TODO\n**Outcome:** 12 rows\n**Verify by:** count\n"
        errors, _ = check_spec(spec)
        self.assertEqual(len(errors), 1)

    def test_a_real_value_containing_parentheses_is_kept(self) -> None:
        spec = (
            "**Change:** add filter (client side)\n"
            "**Outcome:** 4 of 20 rows\n"
            "**Verify by:** type web3, count\n"
        )
        errors, _ = check_spec(spec)
        self.assertEqual(errors, [])


class TestCommentStripping(unittest.TestCase):
    def test_template_guidance_does_not_warn(self) -> None:
        """The starter's own instructions mention 'faster' on purpose."""
        spec = (
            "<!-- 'it gets faster' cannot be proved false -->\n"
            "**Change:** add a filter\n"
            "**Outcome:** 4 of 20 rows visible\n"
            "**Verify by:** type web3, count rows\n"
        )
        errors, warnings = check_spec(spec)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_real_vague_prose_still_warns(self) -> None:
        spec = (
            "**Change:** add a filter\n"
            "**Outcome:** 4 of 20 rows visible\n"
            "**Verify by:** type web3, count rows\n"
            "\n## Known risks\n\n- it might be slower on large feeds\n"
        )
        _, warnings = check_spec(spec)
        self.assertTrue(any("slower" in w for w in warnings), warnings)


class TestCheckSpec(unittest.TestCase):
    def test_good_hypothesis_passes(self) -> None:
        errors, _ = check_spec(GOOD)
        self.assertEqual(errors, [], errors)

    def test_vague_outcome_fails(self) -> None:
        errors, _ = check_spec(BAD_OUTCOME)
        self.assertEqual(len(errors), 1)
        self.assertIn("not measurable", errors[0])
        # the message must name the offending word and show a fix
        self.assertIn("better", errors[0])
        self.assertIn("curl", errors[0])

    def test_missing_slot_fails_with_shape_help(self) -> None:
        errors, _ = check_spec(MISSING_SLOT)
        self.assertEqual(len(errors), 1)
        self.assertIn("Verify by", errors[0])
        self.assertIn("**Change:**", errors[0])

    def test_missing_slot_reports_no_vague_errors(self) -> None:
        """A missing slot is one problem, not three."""
        errors, _ = check_spec("nothing here\n")
        self.assertEqual(len(errors), 1)

    def test_vague_terms_elsewhere_are_warnings_not_errors(self) -> None:
        spec = (
            "**Change:** make the loading state cleaner\n"
            "**Outcome:** under 200ms on repeat visit\n"
            "**Verify by:** `curl -w` timing\n"
        )
        errors, warnings = check_spec(spec)
        self.assertEqual(errors, [])
        self.assertTrue(any("cleaner" in w or "clean" in w for w in warnings))

    def test_overly_inclusive_is_the_safe_direction(self) -> None:
        """A false positive costs a rewrite; a false negative ships nothing."""
        errors, _ = check_spec(
            "**Change:** x\n**Outcome:** 3 rows\n**Verify by:** counting\n"
            + "some prose that mentions good and simple\n"
        )
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()


# --- regressions found by using the checker for real ------------------------
#
# These two were not hypothetical. A spec written across three wrapped lines
# had its Outcome truncated to the first line, and the vague-word list was
# English-only, so a spec saying "更健壮" passed every check.


def test_multiline_outcome_is_not_truncated() -> None:
    """A wrapped Outcome is still an Outcome.

    The first line of a wrapped Chinese sentence can read as vague while the
    rest carries the number that makes it measurable. Reading one line lets
    the vagueness hide, or lets a real problem hide behind the wrap.
    """
    spec = """# Current hypothesis

**Round:** R9
**Tier:** 1

## Hypothesis

**Change:** 把常数写成代码
**Outcome:** 探针在两个端点都退出 0，
费用积分与 StateView 读数相差不超过 0.01 USDG
**Verify by:** `python -m robinhood_lp_v2.probe`
"""
    errors, _ = check_spec(spec)
    assert errors == [], f"unexpected: {errors}"

    slots = _slot_values(spec)
    assert slots["Outcome"] is not None
    assert "StateView" in slots["Outcome"], "second line was dropped"
    assert "0.01" in slots["Outcome"], "number on the wrapped line was dropped"


def test_chinese_vague_words_are_rejected() -> None:
    """The vague list was English, so Chinese specs bypassed the check.

    This project writes its specs in Chinese. "更健壮" is the same
    unobservable claim as "more robust", and it used to pass.
    """
    spec = """# Current hypothesis

**Round:** R9
**Tier:** 1

## Hypothesis

**Change:** 重构采集层
**Outcome:** 代码变得更健壮，扫描速度提升
**Verify by:** `pytest`
"""
    errors, _ = check_spec(spec)
    assert errors, "Chinese vague wording passed the falsifiability check"
    assert "更健壮" in errors[0]


def test_chinese_measurement_is_accepted() -> None:
    """A Chinese outcome carrying a number is still falsifiable."""
    spec = """# Current hypothesis

**Round:** R9
**Tier:** 1

## Hypothesis

**Change:** 采集一个池的全部事件
**Outcome:** 在 3 分钟内取到 3739 个事件，官网与 Alchemy 计数一致
**Verify by:** `python -m robinhood_lp_v2.ingest`
"""
    errors, _ = check_spec(spec)
    assert errors == [], f"a measurable Chinese outcome was rejected: {errors}"
