"""Command line interface.

oxide-triage query "Find promising oxide dielectric candidates ..." --profile conservative
oxide-triage warm-cache            # needs MP_API_KEY; fetches the candidate universe
oxide-triage load-fixtures         # synthetic demo data, clearly flagged in every output
oxide-triage profiles
oxide-triage cache-status
oxide-triage eval                  # runs the evaluation suite against the current cache
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer

from oxide_triage.cache import Cache
from oxide_triage.config import list_profiles, load_config
from oxide_triage.edges.render import render
from oxide_triage.pipeline import load_fixtures, run_triage, warm_cache

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)

PI_REQUEST = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


@app.command()
def query(
    request: str = typer.Argument(PI_REQUEST, help="Natural-language request. Defaults to the PI's example."),
    profile: str = typer.Option("default", "--profile", "-p", help="Config profile name."),
    template: str | None = typer.Option(None, "--template", "-t", help="pi_summary | audit | json"),
    offline: bool | None = typer.Option(
        None, "--offline/--online", help="Force cache-only or allow fetches."
    ),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write output to this file."),
    llm: str | None = typer.Option(
        None, "--llm", help="Override provider: none | anthropic | openai_compatible"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run a triage request and print the rendered result."""
    _setup_logging(verbose)
    overrides = {"llm": {"provider": llm}} if llm else None
    config = load_config(profile, overrides=overrides)
    result = run_triage(request, config, offline=offline, template=template)
    text = render(result, template or config.output.default_template)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)
    if not result.guard.proceed:
        raise typer.Exit(code=2)


@app.command("warm-cache")
def warm_cache_cmd(
    profile: str = typer.Option("default", "--profile", "-p"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch the candidate universe and all per-candidate records from the public sources."""
    _setup_logging(verbose)
    config = load_config(profile)
    typer.echo(f"Warming cache at {config.cache.path} (universe params from profile '{profile}')...")
    summary = warm_cache(config)
    typer.echo(json.dumps(summary, indent=2, default=str))
    if summary["fixture_data"]:
        typer.echo(
            "WARNING: this cache also contains synthetic fixture records; outputs will carry the "
            "fixture banner. Use a separate cache path for real runs.",
            err=True,
        )


@app.command("load-fixtures")
def load_fixtures_cmd(profile: str = typer.Option("default", "--profile", "-p")) -> None:
    """Load the synthetic development fixture into the cache (for demos and tests only)."""
    config = load_config(profile)
    n = load_fixtures(config)
    typer.echo(f"Loaded {n} SYNTHETIC fixture materials into {config.cache.path}. All outputs will say so.")


@app.command()
def profiles() -> None:
    """List configuration profiles and what they change."""
    for name in ["default", *list_profiles()]:
        cfg = load_config(name)
        g = cfg.gates
        typer.echo(
            f"{name:24s} hull<={g.max_energy_above_hull_ev_atom:<5g} gap>={g.min_band_gap_ev:<4g} "
            f"elements<={g.max_elements} blocked tiers={cfg.toxicity.blocklist_tiers} "
            f"allow={cfg.toxicity.element_allowlist or '-'} top_k={cfg.output.top_k} "
            f"weights={cfg.weights.model_dump()}"
        )
        typer.echo(f"{'':24s} {cfg.description.strip()}")


@app.command("cache-status")
def cache_status(profile: str = typer.Option("default", "--profile", "-p")) -> None:
    """Show what is in the cache."""
    config = load_config(profile)
    cache = Cache(config.cache.path)
    try:
        typer.echo(f"cache: {config.cache.path}")
        typer.echo(f"fixture data loaded: {cache.has_fixture_data}")
        for source, info in cache.sources_summary().items():
            typer.echo(f"  {source:20s} rows={info['n']:<6d} oldest={info['oldest']} newest={info['newest']}")
    finally:
        cache.close()


@app.command("eval")
def eval_cmd(
    out: Path = typer.Option(Path("eval/output"), "--out", "-o"),
    use_fixtures: bool = typer.Option(
        True, "--fixtures/--live-cache", help="Evaluate on fixture data or the live cache."
    ),
) -> None:
    """Run the evaluation suite (normal, adversarial, known-answer, determinism, missing-data)."""
    from eval.run_eval import run_all  # local import: eval is not part of the installed package

    report = run_all(out_dir=out, use_fixtures=use_fixtures)
    typer.echo(report)


if __name__ == "__main__":  # pragma: no cover
    app()
