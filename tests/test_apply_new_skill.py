"""apply turns a new-skill proposal into a SKILL.md that Hermes' skill_manage create accepts:
frontmatter with name and a description that fits the skill index, the rule as the body."""

from argus.apply import DESC_LIMIT, new_skill
from argus.jsontypes import as_str, loads


def _front(content: str) -> dict[str, str]:
    assert content.startswith("---\n")
    head = content[4 : content.index("\n---\n", 4)].splitlines()
    out: dict[str, str] = {}
    for line in head:
        key, _, value = line.partition(": ")
        # the description is written as a JSON string, which YAML reads as a double-quoted scalar
        out[key] = (as_str(loads(value)) or "") if value.startswith('"') else value
    return out


def test_short_rule_first_sentence_is_description() -> None:
    c = new_skill("territorial-review", "Consult the procedure first. Then state limits.")
    want = {"name": "territorial-review", "description": "Consult the procedure first."}
    assert _front(c) == want
    assert c.rstrip().endswith("Consult the procedure first. Then state limits.")


def test_long_rule_is_cut_at_a_word_to_the_index_width() -> None:
    rule = (
        "Before assessing synthetic territorial or project data, consult the applicable "
        "procedure and complete available source material."
    )
    desc = _front(new_skill("x", rule))["description"]
    assert len(desc) <= DESC_LIMIT
    assert desc.endswith("\u2026")
    assert rule.startswith(desc[:-1])
    assert not desc[:-1].endswith(" ")


def test_description_with_colons_and_quotes_stays_one_scalar() -> None:
    c = new_skill("x", 'Use "a: b" carefully.')
    assert _front(c)["description"] == 'Use "a: b" carefully.'
    assert c.count("\n---\n") == 1
