"""Templates with variables: substitution only, unknown names stay visible."""

from __future__ import annotations

from app import outreach_templates as tpl


def test_render_substitutes_known_names_and_reports_unknown_ones():
    text, unknown = tpl.render("Hi {{first_name}}, {{ business }} {{nope}} {{nope}} {{also}}",
                               {"first_name": "Dave", "business": "Apex"})
    assert text == "Hi Dave, Apex {{nope}} {{nope}} {{also}}"
    assert unknown == ["nope", "also"]


def test_render_is_substitution_not_a_template_language():
    text, unknown = tpl.render("{{ business.__class__ }} {% if x %}y{% endif %}", {"business": "A"})
    assert "{% if x %}y{% endif %}" in text and "__class__" in text
    assert unknown == []


def test_values_never_guess():
    v = tpl.values_for(ordinal=1, prospect={}, report_url="", findings_doc=None,
                       sender_name="", signature="Relay")
    assert v["first_name"] == "there" and v["business"] == "your business"
    assert v["phone"] == "" and v["domain"] == "" and v["findings"] == ""
    assert v["followup"] == "" and v["finding_1"] == ""


def test_values_carry_the_held_back_finding_for_each_follow_up():
    doc = {"findings": [{"ordinal": i, "what_we_saw": f"saw {i}", "what_it_means": f"means {i}"}
                        for i in range(1, 7)], "selected": [1, 2, 3]}
    two = tpl.values_for(ordinal=2, prospect={}, report_url="u", findings_doc=doc,
                         sender_name="D", signature="S")
    three = tpl.values_for(ordinal=3, prospect={}, report_url="u", findings_doc=doc,
                           sender_name="D", signature="S")
    assert (two["followup"], two["followup_means"]) == ("saw 4", "means 4")
    assert three["followup"] == "saw 5"
    assert two["findings"] == "1. saw 1\n2. saw 2\n3. saw 3"


def test_every_default_uses_only_known_variables_and_clean_copy():
    for n, row in tpl.DEFAULTS.items():
        assert tpl.problems(row["subject"], row["body"]) == [], n


def test_problems_name_what_is_wrong():
    out = tpl.problems("", "Hi {{frist_name}}, your leaky bucket score")
    assert "The subject is empty." in out
    assert any("Unknown variable: {{frist_name}}" in p for p in out)
    assert any("internal vocabulary" in p for p in out)


def test_normalise_reads_the_form_and_fixes_dashes():
    form = {"subject_1": "A — B", "body_1": "x\r\ny", "subject_2": "s", "body_2": "b",
            "subject_3": "s", "body_3": "b", "subject_4": "s", "body_4": "b"}
    out = tpl.normalise(form)
    assert out["1"]["subject"] == "A, B" and out["1"]["body"] == "x\ny"
    assert set(out) == {"1", "2", "3", "4"}


def test_template_for_falls_back_to_the_default_per_field():
    saved = {"2": {"subject": "custom", "body": ""}}
    row = tpl.template_for(2, saved)
    assert row["subject"] == "custom" and row["body"] == tpl.DEFAULTS[2]["body"]
    assert tpl.template_for(1, None) == tpl.DEFAULTS[1]
