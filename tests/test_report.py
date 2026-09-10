"""HTML report: complete, self-contained, and escaping retrieved text."""

from __future__ import annotations

import re

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import load_config
from oxide_triage.edges.render import render
from oxide_triage.pipeline import load_fixtures, run_triage

PI = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer thermodynamically "
    "stable materials, wide band gaps, non-toxic elements, simple compositions, and public evidence. "
    "Return a ranked shortlist with caveats."
)


@pytest.fixture(scope="module")
def cache():
    c = Cache(":memory:")
    load_fixtures(load_config("default", use_env=False), c)
    return c


def test_html_report_has_every_section_and_no_external_assets(cache):
    res = run_triage(PI, load_config("default", use_env=False), cache=cache, offline=True)
    html = render(
        res,
        "html",
        eval_summary={"1. Normal": True, "2. Adversarial": False},
        eval_data_label="synthetic fixture",
    )
    assert html.startswith("<!doctype html>")
    for needle in [
        "Synthetic fixture data",
        "Shortlist",
        "Data-gap map",
        "Excluded by a gate",
        "How the ranking was computed",
        "Read this before running anything",
        res.scope_limitation,
        "Evaluation checks",
        "PASS",
        "FAIL",
        res.config_hash,
        res.cache_fingerprint,
        "HfO2",
        "Ta2O5",
    ]:
        assert needle in html, needle
    # self-contained: no scripts, no external stylesheets/images/fonts
    assert "<script" not in html.lower()
    assert not re.search(r'(src|href)="https?://', html)
    # every shortlisted candidate has a details block with components, gates and provenance
    assert html.count("<details>") == len(res.shortlist)
    assert html.count("Score components") == len(res.shortlist)


def test_html_report_escapes_retrieved_text(cache):
    cfg = load_config("default", use_env=False)
    payload, ts = cache.get("openalex", "formula:HfO2")
    payload["sample_works"][0]["title"] = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    cache.put("openalex", "formula:HfO2", payload, ts)
    try:
        res = run_triage(PI, cfg, cache=cache, offline=True)
        html = render(res, "html")
        assert "<script>alert" not in html and "<img src=x" not in html
        assert "&lt;script&gt;alert" in html and "&lt;img src=x" in html  # displayed as text, not interpreted
        # and the Markdown templates are deliberately NOT escaped (they are Markdown, not HTML)
        assert "<script>alert" in render(res, "audit")
    finally:
        payload["sample_works"][0]["title"] = (
            "Atomic layer deposition of hafnium oxide thin films for gate dielectric applications"
        )
        cache.put("openalex", "formula:HfO2", payload, ts)


def test_html_report_for_declined_and_unconfirmed_requests(cache):
    cfg = load_config("default", use_env=False)
    declined = run_triage("Just give me a number for LaLuO3.", cfg, cache=cache, offline=True)
    html = render(declined, "html")
    assert "Request declined" in html and "Shortlist" not in html.split("Request declined")[1][:200]
    pending = run_triage(PI + " Include lead compounds.", cfg, cache=cache, offline=True, confirmed=False)
    html = render(pending, "html")
    assert "confirmation needed" in html and "hazard block" in html


def test_html_is_a_valid_template_name_everywhere():
    from oxide_triage.schemas import Criteria

    assert Criteria(output_template="html").output_template == "html"
    load_config("default", use_env=False, overrides={"output": {"default_template": "html"}})
