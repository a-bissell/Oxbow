"""Command line interface.

oxide-triage query "Find promising oxide dielectric candidates ..." --profile conservative
oxide-triage warm-cache            # needs MP_API_KEY; fetches the candidate universe, runs self-check
oxide-triage load-fixtures         # synthetic demo data, clearly flagged in every output
oxide-triage add-material SrHfO3   # pull one compound into the universe (online)
oxide-triage fill-gaps             # try alternative routes for data the warm could not find (online)
oxide-triage selfcheck             # known-answer check on the current cache
oxide-triage report --out report.html    # self-contained HTML report (+ eval checks)
oxide-triage doctor                # what the tool sees: .env, keys (masked), site file, cache, source reachability
oxide-triage config                # effective configuration with the origin of every value (default / profile / site / env)
oxide-triage chat                  # talk to the agent in the terminal (needs a language model provider)
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
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for noisy in ("httpcore", "httpx", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def query(
    request: str = typer.Argument(PI_REQUEST, help="Natural-language request. Defaults to the PI's example."),
    profile: str = typer.Option("default", "--profile", "-p", help="Config profile name."),
    template: str | None = typer.Option(None, "--template", "-t", help="pi_summary | audit | json | html"),
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
    text = render(result, template or result.criteria.output_template or config.output.default_template)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)
    if not result.guard.proceed:
        raise typer.Exit(code=2)


@app.command()
def report(
    request: str = typer.Argument(PI_REQUEST, help="Natural-language request. Defaults to the PI's example."),
    out: Path = typer.Option(Path("triage_report.html"), "--out", "-o"),
    profile: str = typer.Option("default", "--profile", "-p"),
    offline: bool | None = typer.Option(None, "--offline/--online"),
    with_eval: bool = typer.Option(True, "--with-eval/--no-eval", help="Append the evaluation checks."),
    yes: bool = typer.Option(True, "--yes/--ask", help="Skip clarification questions."),
) -> None:
    """Write a self-contained HTML report for a request: shortlist, breakdowns, data-gap map,
    scoring rules, scope statement and (optionally) the evaluation checks."""
    config = load_config(profile)
    result = run_triage(request, config, offline=offline, template="html", confirmed=yes)
    extras: dict[str, object] = {}
    if with_eval and result.guard.proceed and not result.needs_confirmation:
        from oxide_triage.evaluation import run_all

        out.parent.mkdir(parents=True, exist_ok=True)
        cache = Cache(config.cache.path)
        fixture = cache.has_fixture_data
        cache.close()
        run_all(out_dir=out.parent / "eval_output", use_fixtures=fixture)
        summary_path = out.parent / "eval_output" / "summary.json"
        extras["eval_summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
        extras["eval_data_label"] = "synthetic fixture" if fixture else f"live cache {config.cache.path}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(result, "html", **extras), encoding="utf-8")
    typer.echo(f"wrote {out}")


@app.command("warm-cache")
def warm_cache_cmd(
    profile: str = typer.Option("default", "--profile", "-p"),
    record: Path | None = typer.Option(
        None, "--record", help="Also save every raw API response under this directory (replayable in tests)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch the candidate universe and all per-candidate records, then run the self-check."""
    _setup_logging(verbose)
    if record is not None:
        import os

        os.environ["OXIDE_TRIAGE_RECORD_DIR"] = str(record)
        typer.echo(f"Recording raw responses to {record} (no headers/keys are written).")
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
    check = summary["selfcheck"]  # type: ignore[index]
    if check.get("inconclusive"):
        typer.echo(
            "SELF-CHECK INCONCLUSIVE: the cache is too sparsely retrieved to validate ranks. "
            "Re-run `oxide-triage warm-cache` to fill the gaps before trusting a shortlist.",
            err=True,
        )
        raise typer.Exit(code=5)
    if not check["passed"]:
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
    verdict = "INCONCLUSIVE" if result.inconclusive else ("PASSED" if result.passed else "FAILED")
    completeness = (
        "" if result.retrieval_completeness is None else f", {result.retrieval_completeness:.1%} retrieved"
    )
    typer.echo(
        f"self-check {verdict} at {result.checked_at} "
        f"({result.n_candidates} candidates{completeness}{', fixture data' if result.fixture else ''})"
    )
    for d in result.details:
        typer.echo(f"  - {d}")
    # Exit 5 for inconclusive: the cache is too sparse to validate ranks, which is a different
    # operational problem from a ranker that got the known answers wrong (exit 4).
    if result.inconclusive:
        raise typer.Exit(code=5)
    if not result.passed:
        raise typer.Exit(code=4)


@app.command()
def doctor(profile: str = typer.Option("default", "--profile", "-p")) -> None:
    """Show what the tool can see: .env files, keys (masked), the site overrides file, cache, and
    whether each public source is reachable. Run this first when something says a key is missing."""
    from pathlib import Path as _P

    from oxide_triage.config import REPO_ROOT, load_dotenv
    from oxide_triage.doctor import env_status, probe_sources, site_file_status
    from oxide_triage.edges.llm import chat_availability

    loaded = load_dotenv()
    typer.echo("dotenv files:")
    for path in (_P.cwd() / ".env", REPO_ROOT / ".env"):
        typer.echo(f"  {path}: {'found' if path.is_file() else 'absent'}")
    typer.echo(
        f"  keys loaded from .env this run: {', '.join(loaded) or 'none (already set or not present)'}"
    )
    typer.echo("environment:")
    for key, shown in env_status():
        typer.echo(f"  {key}: {shown}")
    config = load_config(profile)
    typer.echo(
        f"config: profile={config.profile_name} cache={config.cache.path} offline={config.cache.offline} llm={config.llm.provider}"
    )
    site = site_file_status(config)
    if site["path"] is None:
        typer.echo("site file: disabled (OXIDE_TRIAGE_SITE_CONFIG=off or in-memory cache)")
    else:
        state = "present" if site["present"] else "absent"
        typer.echo(
            f"site file: {site['path']} ({state}, {'writable' if site['writable'] else 'not writable'}, "
            f"{site['overrides']} override(s) in effect for this profile)"
        )
    chat_ok, chat_why = chat_availability(config.llm)
    typer.echo(f"chat: {'ready (' + chat_why + ')' if chat_ok else 'unavailable (' + chat_why + ')'}")
    cache = Cache(config.cache.path)
    try:
        sc = read_selfcheck(cache)
        typer.echo(
            f"cache: {cache.count()} rows, fixture={cache.has_fixture_data}, "
            f"selfcheck={'not run' if sc is None else ('passed' if sc.passed else 'FAILED')}"
        )
    finally:
        cache.close()
    if config.cache.offline:
        typer.echo("reachability: skipped (offline mode)")
        return
    typer.echo("reachability:")
    for name, note in probe_sources().items():
        typer.echo(f"  {name}: {note}")


@app.command("config")
def config_cmd(
    profile: str = typer.Option("default", "--profile", "-p"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    changed_only: bool = typer.Option(
        False, "--changed-only", help="Only values not from the shipped files."
    ),
) -> None:
    """Print the effective configuration and where each value comes from: default.yaml, the
    profile file, the site overrides file (base or profile section), or an environment variable."""
    from oxide_triage.config import config_layers, flatten_leaves

    layers = config_layers(profile)
    values = flatten_leaves(layers.effective.model_dump())
    rows = []
    for key in sorted(values):
        origin = layers.origin_of(key)
        if changed_only and origin in {"default", "profile"}:
            continue
        rows.append({"key": key, "value": values[key], "origin": origin})
    if json_out:
        typer.echo(
            json.dumps(
                {
                    "profile": layers.profile,
                    "site_file": str(layers.site_path) if layers.site_path else None,
                    "config_hash": layers.effective.config_hash(),
                    "values": rows,
                },
                indent=2,
                default=str,
            )
        )
        return
    typer.echo(f"profile: {layers.profile}   config hash: {layers.effective.config_hash()}")
    typer.echo(f"site file: {layers.site_path or 'disabled'}")
    if not rows:
        typer.echo("no values outside the shipped files" if changed_only else "no values")
        return
    width = max(len(r["key"]) for r in rows)
    for r in rows:
        typer.echo(f"  {r['key']:{width}s}  {r['origin']:13s}  {r['value']}")


@app.command()
def chat(
    profile: str = typer.Option("default", "--profile", "-p", help="Config profile name."),
    offline: bool | None = typer.Option(
        None, "--offline/--online", help="Force cache-only or allow fetches."
    ),
    llm: str | None = typer.Option(None, "--llm", help="Override provider: anthropic | openai_compatible"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Talk to the agent in the terminal. The model drives the same tools the MCP server and the
    Streamlit Agent page use; every number it relays comes out of a tool. Tool calls and any
    numbers the number guard could not verify are printed to stderr. `/new` starts over, `/quit` exits."""
    from oxide_triage.agent import Agent
    from oxide_triage.edges.llm import make_chat_llm
    from oxide_triage.tools import ToolBox, agent_system_prompt

    _setup_logging(verbose)
    overrides: dict = {}
    if llm:
        overrides["llm"] = {"provider": llm}
    if offline is not None:
        overrides["cache"] = {"offline": offline}
    config = load_config(profile, overrides=overrides or None)
    try:
        model = make_chat_llm(config.llm, config.agent)
    except RuntimeError as exc:
        typer.echo(f"chat unavailable: {exc}", err=True)
        raise typer.Exit(code=2) from None
    toolbox = ToolBox(
        config_overrides=overrides or None, tool_result_max_chars=config.agent.tool_result_max_chars
    )
    agent = Agent(
        model, toolbox, agent_system_prompt(profile), config.agent.max_tool_rounds, config.agent.number_guard
    )
    typer.echo(f"oxide-triage chat · {model.name} · profile {profile} · /new, /quit", err=True)

    def on_text(chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def on_tool(ev) -> None:
        status = "error" if ev.outcome.is_error else "ok"
        typer.echo(f"\n[tool] {ev.name}({json.dumps(ev.input)}) -> {status}", err=True)

    interactive = sys.stdin.isatty()
    while True:
        try:
            line = input("\n> " if interactive else "")
        except EOFError:
            break
        text = line.strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            break
        if text == "/new":
            agent.new_conversation()
            typer.echo("[new conversation]", err=True)
            continue
        reply = agent.send(text, on_text=on_text, on_tool=on_tool)
        sys.stdout.write("\n")
        if reply.error:
            typer.echo(f"[error] {reply.error}", err=True)
        if reply.unverified_numbers:
            typer.echo(
                f"[number guard] not found in any tool output: {', '.join(reply.unverified_numbers)}",
                err=True,
            )
        if reply.latest_result_id:
            typer.echo(f"[result] {reply.latest_result_id}", err=True)


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
    from oxide_triage.evaluation import run_all

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
