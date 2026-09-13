"""A profile's figure-of-merit vocabulary reaches the guard and the parser, and the default
profile's vocabulary changes nothing about what the guard decided before it existed."""

from __future__ import annotations

import yaml

from oxide_triage.config import DATA_DIR, load_config, load_hazard_table
from oxide_triage.edges.parse import criterion_words, rule_parse
from oxide_triage.evaluation import ADVERSARIAL, PI
from oxide_triage.guard import ScopeVocabulary, guard_request, scope_vocabulary

TABLE = load_hazard_table()


def _corpus() -> list[str]:
    held = yaml.safe_load((DATA_DIR / "heldout_requests.yaml").read_text(encoding="utf-8")) or {}
    texts = [
        PI,
        PI + " Prioritise the dielectric constant.",
        "Rank oxides for solar cells.",
        "Yes, go ahead.",
    ]
    texts += [v[0] for v in ADVERSARIAL.values()]
    for entry in held.get("requests", held.get("items", [])) or []:
        if isinstance(entry, dict) and entry.get("text"):
            texts.append(str(entry["text"]))
        elif isinstance(entry, str):
            texts.append(entry)
    return texts


def test_default_vocabulary_leaves_every_guard_verdict_unchanged():
    scope = scope_vocabulary(load_config("default", use_env=False))
    corpus = _corpus()
    assert len(corpus) > 20
    for text in corpus:
        plain = guard_request(text, TABLE)
        scoped = guard_request(text, TABLE, scope=scope)
        assert plain.proceed == scoped.proceed, text
        assert [(f.bin, f.code) for f in plain.findings] == [(f.bin, f.code) for f in scoped.findings], text


def test_another_profiles_words_make_its_application_an_in_scope_ask():
    text = "Rank oxides for battery cathodes by gravimetric capacity."
    assert not guard_request(text, TABLE).proceed  # a battery ask is out of scope for the shipped words
    scope = ScopeVocabulary(terms=("capacity",), application="a battery cathode")
    d = guard_request(text, TABLE, scope=scope)
    assert d.proceed and not d.findings
    # A vocabulary never adds a finding: an ask that proceeds without it still proceeds.
    assert guard_request(PI, TABLE, scope=scope).proceed


def test_parser_weights_the_profiles_figure_of_merit_under_its_own_name():
    cfg = load_config(
        "default",
        use_env=False,
        overrides={
            "figure_of_merit": {
                "criterion": "thermal_conductivity",
                "label": "thermal conductivity",
                "vocabulary": ["thermal conductivity", "conductivity", "clarke"],
            }
        },
    )
    words = criterion_words(cfg.figure_of_merit)
    assert "dielectric" not in words and list(words)[2] == "thermal_conductivity"
    c = rule_parse(
        "Rank oxides for thermal barrier coatings; prioritise low thermal conductivity.",
        TABLE,
        words=words,
        vocabulary=cfg.figure_of_merit.vocabulary,
    )
    assert c.weight_overrides == {"thermal_conductivity": 0.4}
    assert c.unhandled == []  # the figure-of-merit clause is the ask itself, not an unread clause
    # The shipped table still reads the dielectric vocabulary.
    assert rule_parse(PI + " Prioritise permittivity.", TABLE).weight_overrides == {"dielectric": 0.4}
