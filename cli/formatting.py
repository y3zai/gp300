from datetime import datetime, timezone, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


console = Console()

RANGES = ["1D", "1W", "1M", "3M", "YTD", "1Y", "MAX"]


def range_to_since(range_str):
    now = datetime.now(timezone.utc)
    match range_str.upper():
        case "1D":
            return (now - timedelta(days=1)).isoformat()
        case "1W":
            return (now - timedelta(weeks=1)).isoformat()
        case "1M":
            return now.replace(month=now.month - 1).isoformat() if now.month > 1 else now.replace(year=now.year - 1, month=12).isoformat()
        case "3M":
            m = now.month - 3
            if m <= 0:
                return now.replace(year=now.year - 1, month=m + 12).isoformat()
            return now.replace(month=m).isoformat()
        case "YTD":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        case "1Y":
            return now.replace(year=now.year - 1).isoformat()
        case "MAX":
            return None
        case _:
            return None


def format_timestamp(iso):
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%b %d, %Y %H:%M UTC")


def format_number(n):
    return f"{n:,.2f}"


def format_volume(v):
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def format_probability(c):
    probs = c.get("probabilities", [])
    num = c.get("num_outcomes", 0)
    if not probs:
        return "—"
    if num == 2:
        return f"{probs[0] * 100:.2f}%"
    top = max(probs)
    return f"{top * 100:.2f}% (top of {num})"


def print_current(data):
    lines = [
        f"  Value:          [bold white]{format_number(data['index_value'])}[/]",
        f"  Constituents:   {data['num_constituents']}",
        f"  Wtd Entropy:    {data['weighted_entropy']:.6f}",
        f"  Updated:        {format_timestamp(data['timestamp'])}",
    ]
    panel = Panel(
        "\n".join(lines),
        title="[bold cyan]GP 300: Geopolitics Uncertainty Index[/]",
        border_style="cyan",
        padding=(1, 2),
    )
    console.print(panel)


def print_history(data, range_label):
    if not data:
        console.print("[yellow]No history data available.[/]")
        return

    values = [d["value"] for d in data]
    high = max(values)
    low = min(values)
    first = values[0]
    last = values[-1]
    change = last - first
    pct = (change / first * 100) if first != 0 else 0
    sign = "+" if change >= 0 else ""
    color = "green" if change >= 0 else "red"

    console.print(f"\n[bold cyan]GP 300 History ({range_label})[/]")
    console.print(
        f"  High: [bold]{format_number(high)}[/]    "
        f"Low: [bold]{format_number(low)}[/]    "
        f"Change: [{color}]{sign}{format_number(change)} ({sign}{pct:.2f}%)[/]\n"
    )

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Timestamp", style="dim")
    table.add_column("Value", justify="right")
    table.add_column("Constituents", justify="right")

    for d in data:
        table.add_row(
            format_timestamp(d["timestamp"]),
            format_number(d["value"]),
            str(d["num_constituents"]),
        )

    console.print(table)


def print_constituents(data, top_n):
    if not data:
        console.print("[yellow]No constituent data available.[/]")
        return

    total = len(data)
    shown = data[:top_n] if top_n else data
    label = f"top {len(shown)} of {total}" if top_n and top_n < total else f"all {total}"

    console.print(f"\n[bold cyan]GP 300 Constituents ({label})[/]\n")

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name")
    table.add_column("Weight", justify="right")
    table.add_column("Probability", justify="right")
    table.add_column("Entropy", justify="right")
    table.add_column("Volume", justify="right")

    for i, c in enumerate(shown, 1):
        table.add_row(
            str(i),
            c["label"][:50],
            f"{c['weight'] * 100:.2f}%",
            format_probability(c),
            f"{c['normalized_entropy']:.4f}",
            format_volume(c["volume_1mo"]),
        )

    console.print(table)
