import typer

from .db import get_connection

app = typer.Typer()


@app.command()
def hello() -> None:
    print("ELENA Fuel Processor OK")


@app.command()
def db_test() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            version = cur.fetchone()[0]

    print("Conexión PostgreSQL OK")
    print(version)


if __name__ == "__main__":
    app()