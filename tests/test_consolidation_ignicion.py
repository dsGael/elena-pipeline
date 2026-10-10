"""Descarte de lecturas con ignición apagada en la consolidación."""

from datetime import UTC, datetime

import pytest

from elena.consolidation import (
    AGGREGATE_SQL,
    consolidate,
    fetch_aggregates,
)
from tests.conftest import IMEI_YARIS

T0 = datetime(2026, 10, 8, 17, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 8, 17, 5, tzinfo=UTC)


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.conn.llamadas.append((sql, params))

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.llamadas = []

    def cursor(self):
        return _Cur(self)


def test_el_sql_usa_la_columna_filtrada_para_las_estadisticas_del_sensor():
    assert "s_analogo_uso" in AGGREGATE_SQL
    for agregado in (
        "count(s.s_analogo_uso)",
        "ORDER BY s.s_analogo_uso",
        "avg(s.s_analogo_uso)",
    ):
        assert agregado in AGGREGATE_SQL
    assert "s.s_analogo)" not in AGGREGATE_SQL  # ninguna estadística usa el crudo
    # el comentario del SQL no puede contener marcadores %(...)s extra
    assert AGGREGATE_SQL.count("%(descartar_sin_ignicion)s") == 1


@pytest.mark.parametrize("valor", [True, False])
def test_fetch_aggregates_pasa_el_parametro(valor):
    conn = _Conn()
    fetch_aggregates(conn, T0, T1, descartar_sin_ignicion=valor)
    assert conn.llamadas[0][1]["descartar_sin_ignicion"] is valor


def test_por_defecto_no_cambia_el_comportamiento_anterior():
    conn = _Conn()
    fetch_aggregates(conn, T0, T1)
    assert conn.llamadas[0][1]["descartar_sin_ignicion"] is False


@pytest.mark.integration
def test_ventana_con_motor_apagado_queda_descartada(conn):
    def muestra(seg, sensor, ign):
        conn.execute(
            "INSERT INTO gps_records (imei, recorded_at, latitude, longitude, s_analogo, "
            "inclinacion_vertical, ignicion, odometro_total) VALUES (%s,%s,0,0,%s,2.6,%s,1000)",
            (IMEI_YARIS, T0.replace(second=seg), sensor, ign),
        )

    for s in range(0, 40, 2):
        muestra(s, 130, False)  # motor apagado: el sensor lee ~130
    consolidate(conn, T0, T1, descartar_sin_ignicion=True)
    fila = conn.execute(
        "SELECT calidad_ventana, motivo_calidad, sensor_mediana FROM fuel_windows"
    ).fetchone()
    assert (
        fila[0] == "descartada" and "sin_lectura_sensor" in fila[1] and fila[2] is None
    )


@pytest.mark.integration
def test_ventana_mixta_usa_solo_las_muestras_con_motor_encendido(conn):
    def muestra(seg, sensor, ign):
        conn.execute(
            "INSERT INTO gps_records (imei, recorded_at, latitude, longitude, s_analogo, "
            "inclinacion_vertical, ignicion, odometro_total) VALUES (%s,%s,0,0,%s,2.6,%s,1000)",
            (IMEI_YARIS, T0.replace(second=seg), sensor, ign),
        )

    for s in range(0, 20, 2):
        muestra(s, 130, False)
    for s in range(20, 60, 2):
        muestra(s, 5036, True)
    consolidate(conn, T0, T1, descartar_sin_ignicion=True)
    mediana = conn.execute("SELECT sensor_mediana FROM fuel_windows").fetchone()[0]
    assert float(mediana) == 5036.0