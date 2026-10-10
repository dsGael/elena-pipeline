"""Integración del pipeline contra PostgreSQL (requiere ELENA_TEST_DSN, ver conftest.py).

El pipeline confirma sus propias transacciones (un commit por autobús), así que estas pruebas
no pueden apoyarse en el rollback de la fixture ``conn``: usan ``pg`` y limpian al terminar.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest

from elena.pipeline import ejecutar_pipeline
from elena.pipeline_repo import try_lock_autobus
from tests.conftest import IMEI_YARIS, YARIS_CALIBRACION  # noqa: F401
from tests.test_processor import SENSOR

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("America/Hermosillo"))


@pytest.fixture
def pg(_schema: str):
    c = psycopg.connect(_schema)
    c.execute(
        "UPDATE fuel_config SET ventanas_sostenidas = 3 WHERE idautobus = 'YARIS1'"
    )
    c.commit()
    try:
        yield c
    finally:
        c.rollback()
        c.execute(
            "TRUNCATE fuel_events, fuel_processed_result, fuel_estado_confiable, "
            "fuel_windows, gps_records CASCADE"
        )
        c.execute(
            "UPDATE fuel_config SET ventanas_sostenidas = NULL, rendimiento_default = NULL "
            "WHERE idautobus = 'YARIS1'"
        )
        c.commit()
        c.close()


def _ventana(c, i, litros, odo=100_000):
    ini = T0 + timedelta(minutes=5 * i)
    c.execute(
        "INSERT INTO fuel_windows (imei, ventana_inicio, ventana_fin, muestras_totales, "
        "muestras_validas, sensor_mediana, velocidad_promedio, odometro_total, "
        "calidad_ventana) VALUES (%s,%s,%s,15,15,%s,0,%s,'valida')",
        (IMEI_YARIS, ini, ini + timedelta(minutes=5), SENSOR[litros], odo),
    )
    # una muestra GPS dentro de la ventana, para la posición del evento
    c.execute(
        "INSERT INTO gps_records (imei, recorded_at, latitude, longitude) "
        "VALUES (%s,%s,29.07,-110.95)",
        (IMEI_YARIS, ini + timedelta(minutes=1)),
    )


def test_flujo_completo_carga_confirmada_e_idempotencia(pg):
    for i, litros in enumerate([10, 13, 13, 13]):
        _ventana(pg, i, litros)
    pg.commit()

    r = ejecutar_pipeline(pg, idautobus="YARIS1")
    (a,) = r.autobuses
    assert a.resultado == "ok" and a.procesadas == 4

    estados = [
        x[0]
        for x in pg.execute(
            "SELECT estado_resultado FROM fuel_processed_result ORDER BY ventana_inicio"
        ).fetchall()
    ]
    assert estados == [
        "confiable",
        "carga_probable",
        "carga_probable",
        "carga_confirmada",
    ]
    ev = pg.execute(
        "SELECT tipo_evento, estado, litros_evento, latitude FROM fuel_events"
    ).fetchall()
    assert ev == [("carga_confirmada", "confirmado", 3, 29.07)]
    est = pg.execute(
        "SELECT ultimo_litro_confiable, pendiente_tipo, imei_ultimo FROM fuel_estado_confiable"
    ).fetchone()
    assert (float(est[0]), est[1], est[2]) == (13.0, None, IMEI_YARIS)

    # segunda corrida: nada nuevo
    (b,) = ejecutar_pipeline(pg, idautobus="YARIS1").autobuses
    assert b.procesadas == 0
    assert pg.execute("SELECT count(*) FROM fuel_processed_result").fetchone()[0] == 4


def test_lock_advisory_omite_autobus_ocupado(pg, _schema):
    _ventana(pg, 0, 10)
    pg.commit()
    with psycopg.connect(_schema) as otra:
        assert try_lock_autobus(otra, "YARIS1")  # otra instancia lo tiene
        (a,) = ejecutar_pipeline(pg, idautobus="YARIS1").autobuses
        assert a.resultado == "ocupado" and a.procesadas == 0
        otra.rollback()  # libera el lock
    (b,) = ejecutar_pipeline(pg, idautobus="YARIS1").autobuses
    assert b.resultado == "ok" and b.procesadas == 1
