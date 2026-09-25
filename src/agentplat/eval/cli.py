"""agent-eval: run evaluation suites and compare reports."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from agentplat.eval.runner import EvalRunner, save_report
from agentplat.eval.suite import load_suite

app = typer.Typer(help="Evaluate agent behaviour: tool selection, safety, recovery.")


@app.command()
def run(
    suite: Annotated[Path, typer.Argument(help="Suite YAML file")],
    provider: Annotated[str, typer.Option(help="fake | openai | anthropic | gemini")] = "fake",
    model: Annotated[str | None, typer.Option(help="Model name for real providers")] = None,
    repeat: Annotated[int, typer.Option(min=1, max=20, help="Runs per item")] = 1,
    out: Annotated[Path, typer.Option(help="Report directory")] = Path("reports"),
    data_dir: Annotated[str, typer.Option(help="Fixtures directory")] = "fixtures",
    min_pass_rate: Annotated[float | None, typer.Option(help="Exit 1 if below (CI gate)")] = None,
) -> None:
    s = load_suite(suite)
    runner = EvalRunner(provider=provider, model=model, data_dir=data_dir)
    report = asyncio.run(runner.run_suite(s, repeats=repeat))
    jp, mp = save_report(report, out)
    m = report.metrics
    typer.echo(f"{report.label}")
    typer.echo(f"pass rate {m['pass_rate']} over {m['runs']} runs -> {jp} , {mp}")
    unsafe = m["unsafe_actions"]["destructive_executed_without_approval"]
    if unsafe:
        typer.echo(f"FAIL: {unsafe} destructive action(s) executed without approval", err=True)
        raise typer.Exit(1)
    if min_pass_rate is not None and (m["pass_rate"] or 0) < min_pass_rate:
        typer.echo(f"FAIL: pass rate {m['pass_rate']} < {min_pass_rate}", err=True)
        raise typer.Exit(1)


@app.command()
def compare(
    a: Annotated[Path, typer.Argument(help="Baseline report JSON")],
    b: Annotated[Path, typer.Argument(help="Candidate report JSON")],
) -> None:
    ra, rb = json.loads(a.read_text()), json.loads(b.read_text())
    typer.echo(f"{'metric':50} {'baseline':>12} {'candidate':>12}")
    for key, va, vb in _flatten_pairs(ra["metrics"], rb["metrics"]):
        if va != vb:
            typer.echo(f"{key:50} {va!s:>12} {vb!s:>12}")
    pa = {(r["item_id"], r["repeat"]): r["passed"] for r in ra["results"]}
    pb = {(r["item_id"], r["repeat"]): r["passed"] for r in rb["results"]}
    regressions = sorted({k[0] for k in pa if k in pb and pa[k] and not pb[k]})
    fixes = sorted({k[0] for k in pa if k in pb and not pa[k] and pb[k]})
    typer.echo(f"regressions: {regressions or 'none'}")
    typer.echo(f"fixes: {fixes or 'none'}")


def _flatten_pairs(
    a: dict[str, Any], b: dict[str, Any], prefix: str = ""
) -> list[tuple[str, Any, Any]]:
    out: list[tuple[str, Any, Any]] = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, dict) or isinstance(vb, dict):
            out += _flatten_pairs(va or {}, vb or {}, f"{prefix}{k}.")
        elif not isinstance(va, list):
            out.append((f"{prefix}{k}", va, vb))
    return out


if __name__ == "__main__":
    app()
