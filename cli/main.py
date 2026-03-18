import json

import click

from cli.api import GP300Client, DEFAULT_BASE_URL
from cli.formatting import (
    RANGES,
    print_current,
    print_history,
    print_constituents,
    range_to_since,
)


def _use_json(ctx):
    return ctx.obj.get("use_json") or ctx.params.get("use_json", False)


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
@click.option("--url", envvar="GP300_API_URL", default=DEFAULT_BASE_URL, help="API base URL")
@click.pass_context
def cli(ctx, use_json, url):
    """GP 300: Geopolitics Uncertainty Index CLI"""
    ctx.ensure_object(dict)
    ctx.obj["client"] = GP300Client(base_url=url)
    ctx.obj["use_json"] = use_json
    if ctx.invoked_subcommand is None:
        ctx.invoke(current)


@cli.command()
@click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def current(ctx, use_json):
    """Show the current index value."""
    client = ctx.obj["client"]
    data = client.get_current()
    if _use_json(ctx):
        click.echo(json.dumps(data, indent=2))
    else:
        print_current(data)


@cli.command()
@click.argument("range_str", default="1D", type=click.Choice(RANGES, case_sensitive=False))
@click.option("--limit", "-n", default=0, help="Limit to N most recent rows (0 = all)")
@click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def history(ctx, range_str, limit, use_json):
    """Show historical index data. RANGE: 1D, 1W, 1M, 3M, YTD, 1Y, MAX"""
    client = ctx.obj["client"]
    since = range_to_since(range_str.upper())
    data = client.get_history(since=since)
    if limit > 0:
        data = data[-limit:]
    if _use_json(ctx):
        click.echo(json.dumps(data, indent=2))
    else:
        print_history(data, range_str.upper())


@cli.command()
@click.option("--top", "-t", default=20, help="Show top N constituents (0 = all)")
@click.option("--sort", "-s", "sort_by", default="weight",
              type=click.Choice(["weight", "entropy", "volume", "name"], case_sensitive=False),
              help="Sort field")
@click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def constituents(ctx, top, sort_by, use_json):
    """List index constituents."""
    client = ctx.obj["client"]
    data = client.get_constituents()

    sort_keys = {
        "weight": lambda c: c["weight"],
        "entropy": lambda c: c["normalized_entropy"],
        "volume": lambda c: c["volume_1mo"],
        "name": lambda c: c["label"].lower(),
    }
    reverse = sort_by != "name"
    data.sort(key=sort_keys[sort_by], reverse=reverse)

    if _use_json(ctx):
        out = data[:top] if top > 0 else data
        click.echo(json.dumps(out, indent=2))
    else:
        print_constituents(data, top if top > 0 else 0)
