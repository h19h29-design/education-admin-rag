import typer

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run education administration corpus commands."""


@app.command()
def version() -> None:
    typer.echo("education-admin-rag 0.1.0")


if __name__ == "__main__":
    app()
