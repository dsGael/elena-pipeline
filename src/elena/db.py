from psycopg import Connection

from .settings import settings


def get_connection() -> Connection:
    return Connection.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )