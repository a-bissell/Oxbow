"""Command line interface.

oxide-triage query "Find promising oxide dielectric candidates ..." --profile conservative
oxide-triage warm-cache            # needs MP_API_KEY; fetches the candidate universe, runs self-check
oxide-triage load-fixtures         # synthetic demo data, clearly flagged in every output
oxide-triage add-material SrHfO3   # pull one compound into the universe (online)
oxide-triage fill-gaps             # try alternative routes for data the warm could not find (online)
oxide-triage selfcheck             # known-answer check on the current cache
oxide-triage profiles
oxide-triage cache-status
oxide-triage eval                  # runs the evaluation suite
oxide-triage mcp [--transport http]  # MCP server for Claude Desktop / Cowork / Cursor
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
from oxide_triage.pipeline import add_material, load_fixtures, run_acquisition, run_triage, warm_cache
from oxide_triage.selfcheck import read_selfcheck, run_selfcheck

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
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip clarification questions and run."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run a triage request and print the rendered result."""
    _setup_logging(verbose)
    overrides = {"llm": {"provider": llm}} if llm else None
    config = load_config(profile, overrides=overrides)
    result = run_triage(request, config, offline=offline, template=template, confirmed=yes)
    if result.needs_confirmation:
        typer.echo("Before running, please confirm:", err=True)
        for q in result.clarifications:
            typer.echo(f"  - {q}", err=True)
        if not sys.stdin.isatty() or not typer.confirm("Proceed?", default=False):
            typer.echo("Not run. Re-run with --yes to skip the questions.", err=True)
            raise typer.Exit(code=3)
        result = run_triage(request, config, offline=offline, template=template, confirmed=True)
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
    """Fetch the candidate universe and all per-candidate records, then run the self-check."""
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
    if not summary["selfcheck"]["passed"]:  # type: ignore[index]
        typer.echo(
            "SELF-CHECK FAILED. See details above; triage runs will be blocked until it passes.", err=True
        )
        raise typer.Exit(code=4)


@app.command("load-fixtures")
def load_fixtures_cmd(profile: str = typer.Option("default", "--profile", "-p")) -> None:
    """Load the synthetic development fixture into the cache (for demos and tests only)."""
    config = load_config(profile)
    n = load_fixtures(config)
    typer.echo(f"Loaded {n} SYNTHETIC fixture materials into {config.cache.path}. All outputs will say so.")


@app.command("add-material")
def add_material_cmd(
    formula: str = typer.Argument(..., help="Reduced formula, e.g. SrHfO3"),
    profile: str = typer.Option("default", "--profile", "-p"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Pull one compound from the public sources into the candidate universe (online only)."""
    _setup_logging(verbose)
    config = load_config(profile)
    result = add_material(formula, config)
    typer.echo(json.dumps(result, indent=2, default=str))
    if result.get("error"):
        raise typer.Exit(code=1)


@app.command("fill-gaps")
def fill_gaps_cmd(
    profile: str = typer.Option("default", "--profile", "-p"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Adaptive acquisition pass: alternative read-only routes for every gap in the cache (online)."""
    _setup_logging(verbose)
    config = load_config(profile)
    cache = Cache(config.cache.path)
    try:
        report = run_acquisition(config, cache)
        if report is None:
            typer.echo("Acquisition did not run (disabled in config, or cache is offline).", err=True)
            raise typer.Exit(code=1)
        typer.echo(json.dumps(report.summary(), indent=2))
        for a in report.attempts:
            typer.echo(f"  {a.formula:10s} {a.kind:12s} {a.route:22s} {a.outcome:12s} {a.note}")
        for g in report.unfillable:
            typer.echo(f"  {g.formula:10s} {g.kind:12s} {'(no public route)':22s} unfillable   {g.detail}")
        check = run_selfcheck(config, cache)
        typer.echo(f"self-check after acquisition: {'PASSED' if check.passed else 'FAILED'}")
    finally:
        cache.close()


@app.command()
def selfcheck(profile: str = typer.Option("default", "--profile", "-p")) -> None:
    """Run the known-answer self-check on the current cache and store the outcome."""
    config = load_config(profile)
    cache = Cache(config.cache.path)
    try:
        result = run_selfcheck(config, cache)
    finally:
        cache.close()
    typer.echo(
        f"self-check {'PASSED' if result.passed else 'FAILED'} at {result.checked_at} "
        f"({result.n_candidates} candidates{', fixture data' if result.fixture else ''})"
    )
    for d in result.details:
        typer.echo(f"  - {d}")
    if not result.passed:
        raise typer.Exit(code=4)


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
        sc = read_selfcheck(cache)
        typer.echo(
            "self-check: "
            + ("not run" if sc is None else f"{'passed' if sc.passed else 'FAILED'} at {sc.checked_at}")
        )
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


@app.command()
def mcp(
    transport: str = typer.Option("stdio", "--transport", help="stdio (desktop apps) | http (container)"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
) -> None:
    """Serve the triage tools over the Model Context Protocol."""
    from oxide_triage.mcp_server import server

    if transport == "http":
        server.run(transport="streamable-http", host=host, port=port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    app()
