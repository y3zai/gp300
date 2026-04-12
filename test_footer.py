from pathlib import Path


def test_footer_includes_github_repo_link_after_cli():
    html = Path("public/index.html").read_text()

    cli_label = "CLI:"
    github_label = "Source Code at "
    github_link = 'href="https://github.com/y3zai/gp300"'
    polymarket_label = "Data from "

    cli_index = html.index(cli_label)
    github_index = html.index(github_label)
    polymarket_index = html.index(polymarket_label)

    assert cli_index < github_index < polymarket_index
    assert github_link in html
