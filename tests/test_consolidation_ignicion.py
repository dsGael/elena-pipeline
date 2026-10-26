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
    assert "sensor_uso" in AGGREGATE_SQL
    for agregado in (
        "count(s.sensor_uso)",
        "ORDER BY s.sensor_uso",
        "avg(s.sensor_uso)",
    ):
        assert agregado in AGGREGATE_SQL
    assert "s.s_analogo)" not in AGGREGATE_SQL  # ninguna estadística usa el crudo
    # el comentario del SQL no puede contener marcadores %(...)s extra
    assert AGGREGATE_SQL.count("%(descartar_sin_ignicion)s") == 1


@pytest.mark.parametrize("valor", [True, False, None])
def test_fetch_aggregates_pasa_el_parametro(valor):
    conn = _Conn()
    fetch_aggregates(conn, T0, T1, descartar_sin_ignicion=valor)
    assert conn.llamadas[0][1]["descartar_sin_ignicion"] is valor


def test_por_defecto_cada_autobus_decide_con_su_configuracion():
    conn = _Conn()
    fetch_aggregates(conn, T0, T1)
    assert conn.llamadas[0][1]["descartar_sin_ignicion"] is None


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


@pytest.mark.integration
def test_sensor_fuel_conserva_lecturas_con_motor_apagado(conn):
    conn.execute("UPDATE fuel_config SET columna_sensor = 'fuel' WHERE idautobus = 'YARIS1'")
    for s in range(0, 40, 2):
        conn.execute(
            "INSERT INTO gps_records (imei, recorded_at, latitude, longitude, fuel, "
            "inclinacion_vertical, ignicion, odometro_total) VALUES (%s,%s,0,0,%s,2.6,false,1000)",
            (IMEI_YARIS, T0.replace(second=s), 420),
        )
    consolidate(conn, T0, T1, descartar_sin_ignicion=True)
    fila = conn.execute(
        "SELECT calidad_ventana, sensor_mediana, ignicion_dominante FROM fuel_windows"
    ).fetchone()
    assert fila[0] != "descartada" and float(fila[1]) == 420.0 and fila[2] is False


def test_el_sql_elige_la_columna_del_sensor_por_autobus():
    assert "c.columna_sensor" in AGGREGATE_SQL
    assert "WHEN 'fuel' THEN CASE WHEN g.fuel >= 0 THEN g.fuel END" in AGGREGATE_SQL
    assert "ELSE g.s_analogo END" in AGGREGATE_SQL
    assert "c.descartar_sin_ignicion" in AGGREGATE_SQL
    assert "AND cfg.columna_sensor IS DISTINCT FROM 'fuel'" in AGGREGATE_SQL
    assert (
        "COALESCE(%(descartar_sin_ignicion)s::boolean, cfg.descartar_sin_ignicion)"
        in AGGREGATE_SQL
    )
    assert AGGREGATE_SQL.count("g.s_analogo") == 1  # ninguna estadística usa el crudo
    assert AGGREGATE_SQL.count("%(descartar_sin_ignicion)s") == 1
