"""Fixtures compartidas.

Las pruebas de integración usan una base PostgreSQL de PRUEBAS y se omiten si no hay una:

    set ELENA_TEST_DSN=postgresql://usuario:clave@host:5432/elena_test      (cmd)
    $env:ELENA_TEST_DSN="postgresql://usuario:clave@host:5432/elena_test"   (PowerShell)

Seguridad: el nombre de la base DEBE terminar en ``_test``. La fixture borra y recrea el
esquema ``public`` de esa base, así que nunca la apuntes a datos reales.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "sql" / "11_schema_elena_v2.sql"

CALIBRACION = (
    '[{"litros":0,"sensor":2},{"litros":1,"sensor":151},{"litros":2,"sensor":289},'
    '{"litros":3,"sensor":425},{"litros":4,"sensor":560},{"litros":5,"sensor":700},'
    '{"litros":6,"sensor":832},{"litros":7,"sensor":967},{"litros":8,"sensor":1103},'
    '{"litros":9,"sensor":1232},{"litros":10,"sensor":1374},{"litros":11,"sensor":1511},'
    '{"litros":12,"sensor":1651},{"litros":13,"sensor":1777},{"litros":14,"sensor":1918}]'
)

IMEI_BUS = "350000000000001"
IMEI_SIN_CONFIG = "350000000000002"  # dispositivo de un autobús sin fuel_config
IMEI_SIN_ASIGNAR = "350000000000003"  # dispositivo sin autobús


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: requiere ELENA_TEST_DSN (base de pruebas)"
    )


@pytest.fixture(scope="session")
def _test_dsn() -> str:
    dsn = os.environ.get("ELENA_TEST_DSN")
    if not dsn:
        pytest.skip("ELENA_TEST_DSN no definida: pruebas de integración omitidas")
    return dsn


@pytest.fixture(scope="session")
def _schema(_test_dsn: str) -> str:
    """Recrea el esquema completo y siembra los datos base. Se hace una vez por sesión."""
    with psycopg.connect(_test_dsn, autocommit=True) as conn:
        dbname = conn.info.dbname
        if not dbname.endswith("_test"):
            pytest.fail(
                f"Se niega a usar la base '{dbname}': el nombre debe terminar en _test."
            )
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO clientes (idcliente, nombre) VALUES ('C1', 'Cliente 1')"
        )
        conn.execute(
            "INSERT INTO autobuses (idautobus, numero_economico, idcliente) "
            "VALUES ('BUS1','B-01','C1'), ('BUS2','B-02','C1')"
        )
        conn.execute(
            "INSERT INTO dispositivos (iddispositivo, imei, idautobus) VALUES "
            "('D1', %s, 'BUS1'), ('D2', %s, 'BUS2'), ('D3', %s, NULL)",
            (IMEI_BUS, IMEI_SIN_CONFIG, IMEI_SIN_ASIGNAR),
        )
        conn.execute(
            "INSERT INTO fuel_config (idautobus, fcc_inclinacion, angulo_max, calibracion) "
            "VALUES ('BUS1', 2.6, 5, %s::jsonb)",
            (CALIBRACION,),
        )
    return _test_dsn


@pytest.fixture
def conn(_schema: str):
    """Conexión por prueba. Todo lo que escriba la prueba se revierte al terminar."""
    with psycopg.connect(_schema) as c:
        try:
            yield c
        finally:
            c.rollback()
