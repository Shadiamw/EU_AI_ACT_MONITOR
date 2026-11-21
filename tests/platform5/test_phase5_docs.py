"""Phase 5 documents that Part 4 owns (audit fix round: F-01, F-03, F-04, F-10, F-12).

- docs/decisions.md holds the spec's decision records D1 to D20 as numbers 234 to 253, under
  their own heading, between the Phase 4 integration block and the Part 1 block (spec 12).
- docs/contract/phase5/delta.md lists the as-built contract changes outside spec 9.2 (audit
  section 8; decisions 287 and 300).
- Decision 319 and README section 17 record that the LIM-03 advisory slot is not built.
- The Model 1 diagram draws the ERASED marker (spec 8.2).
- Manual check M2 covers the report, submission and health panels.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECISIONS = ROOT / "docs" / "decisions.md"
DELTA = ROOT / "docs" / "contract" / "phase5" / "delta.md"
MODELS = ROOT / "docs" / "models" / "phase5_models.md"
MANUAL = ROOT / "docs" / "manual_checks_phase5.md"
README = ROOT / "README.md"

SPEC_HEADING = "## Phase 5 specification decisions (numbers 234 to 253)"
AS_BUILT_HEADING = "## As-built additions (not in spec 9.2), proposed"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _numbers(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"^(\d{3})\. \*\*", text, re.MULTILINE)]


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = text.find("\n## ", start + len(heading))
    return text[start : following if following != -1 else len(text)]


def test_spec_decisions_d1_to_d20_are_numbers_234_to_253_in_their_own_block() -> None:
    text = _read(DECISIONS)
    assert text.count(SPEC_HEADING) == 1
    headings = re.findall(r"^## .*$", text, re.MULTILINE)
    position = headings.index(SPEC_HEADING)
    assert "numbers 214 to 233" in headings[position - 1]
    assert "numbers 254 to 273" in headings[position + 1]
    block = _section(text, SPEC_HEADING)
    assert _numbers(block) == list(range(234, 254))
    for k in range(1, 21):
        assert re.search(rf"^{233 + k}\. \*\*D{k}\. ", block, re.MULTILINE), f"D{k}"
    counts = Counter(_numbers(text))
    assert [n for n, c in counts.items() if c > 1] == []
    assert all(counts[n] == 1 for n in range(234, 254))


def test_the_delta_lists_the_as_built_additions_as_proposed() -> None:
    text = _read(DELTA)
    assert "X-Report-Sha256" in text
    section = _section(text, AS_BUILT_HEADING)
    for token in (
        "version",
        "last_verification",
        "last_verification_age_seconds",
        "reset_count",
        "503",
        "POST /v1/metrics",
        "9999-07-01",
        "pd1:",
        "DISPOSITION_RECORDED",
        "DISPOSITION_REVISED",
        "REPORT_ASSEMBLED",
        "REPORT_SUBMITTED",
        "RECORD_ERASED",
        "GET /v1/events/{event_id}",
        "POST /v1/test/reset",
        "decision 300",
        "ALREADY_ERASED",
        "POST /v1/events/{event_id}/disposition",
        "decision 287",
        "no invisible lock cycle",
    ):
        assert token in section, token
    assert "| A5 |" in section
    assert section.lower().count("proposed") >= 1
    assert not (DELTA.parent.parent / "pod_contract_phase5.md").exists()


def test_the_lim03_advisory_slot_is_recorded_as_not_built() -> None:
    decisions = _read(DECISIONS)
    start = decisions.index("319. **")
    entry = decisions[start : decisions.index("\n320. **", start)]
    assert "Audit fix round" in entry
    for token in ("LIM-03", "advisory slot", "not built", "docs/operations.md", "1.2", "10.3"):
        assert token in entry, token
    readme = _section(_read(README), "## 17. Phase 5 Part 4: platform")
    assert "LIM-03" in readme and "advisory slot" in readme


def test_model_1_draws_the_erased_marker() -> None:
    text = _read(MODELS)
    model1 = _section(text, "## 8.2 Model 1 (state machine)")
    diagram = model1[
        model1.index("```mermaid") : model1.index("```", model1.index("```mermaid") + 3)
    ]
    assert "ERASED" in diagram
    assert "marker" in diagram.lower()


def test_manual_check_m2_covers_the_phase5_panels() -> None:
    text = _read(MANUAL)
    [row] = [line for line in text.splitlines() if line.startswith("| M2 |")]
    for panel in ("Report and submission", "Ledger operations and health", "30 s"):
        assert panel in row, panel
    assert "| M8 |" not in text
