from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from elena.calibration import CalibrationTable
from elena.fuel_repo import (
    ConfigError,
    cargar_config,
    cargar_puntos_rendimiento,
    puntos_desde_filas,
)
from tests.conftest import YARIS_CALIBRACION

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("America/Hermosillo"))
CAL = CalibrationTable.from_json(YARIS_CALIBRACION)


def test_puntos_desde_filas_convierte_y_omite_cotas():
    filas = [
        (T0, Decimal(5036), Decimal(100000)),  # 10 L
        (T0 + timedelta(minutes=5), Decimal(5800), Decimal(100100)),  # zona baja
        (
            T0 + timedelta(minutes=10),
            Decimal(4500),
            Decimal(100200),
        ),  # arriba de calibración (cota)
        (T0 + timedelta(minutes=15), Decimal(4953), None),  # 11 L sin odómetro
    ]
    pts = puntos_desde_filas(filas, CAL)
    assert [(p.litros, p.odometro_total) for p in pts] == [
        (10.0, 100000.0),
        (11.0, None),
    ]


@pytest.mark.integration
def test_cargar_config_lee_fuel_config(conn):
    cfg = cargar_config(conn, "YARIS1")
    assert cfg.calibracion.litros_zona_baja == 4
    assert cfg.umbral_carga_confirmada == 3.0 and cfg.umbral_extraccion == 5.0
    assert cfg.rendimiento_default is None and cfg.ventanas_sostenidas is None
    assert cfg.odometro_a_km == 0.001


@pytest.mark.integration
def test_cargar_config_sin_fila_falla(conn):
    with pytest.raises(ConfigError):
        cargar_config(conn, "BUS2")  # autobús sin fuel_config


@pytest.mark.integration
def test_cargar_puntos_rendimiento_solo_validas_del_autobus(conn):
    from tests.conftest import IMEI_YARIS

    def ins(minuto, calidad, sensor, odo):
        ini = T0 + timedelta(minutes=minuto)
        conn.execute(
            "INSERT INTO fuel_windows (imei, ventana_inicio, ventana_fin, muestras_totales, "
            "muestras_validas, sensor_mediana, odometro_total, calidad_ventana) "
            "VALUES (%s,%s,%s,15,15,%s,%s,%s)",
            (IMEI_YARIS, ini, ini + timedelta(minutes=5), sensor, odo, calidad),
        )

    ins(0, "valida", 5036, 100000)
    ins(5, "dudosa", 5036, 100100)  # no entra
    ins(10, "valida", 4953, 112000)
    pts = cargar_puntos_rendimiento(conn, "YARIS1", T0, T0 + timedelta(hours=1), CAL)
    assert [(p.litros, p.odometro_total) for p in pts] == [
        (10.0, 100000.0),
        (11.0, 112000.0),
    ]


def test_formatear_eventos_vacio_y_con_filas():
    from elena.fuel_repo import EventoFila, formatear_eventos

    tz = ZoneInfo("America/Hermosillo")
    assert formatear_eventos([], tz) == ["(sin eventos)"]
    e = EventoFila(
        fecha_inicio=datetime(2026, 10, 8, 17, 5, tzinfo=ZoneInfo("UTC")),
        fecha_fin=None,
        idautobus="1",
        tipo_evento="carga_confirmada",
        estado="confirmado",
        litros_antes=10.0,
        litros_despues=13.0,
        litros_evento=3.0,
        latitude=29.07,
        longitude=-110.95,
        calidad_evento="valida",
        motivo="aumento_valido_sostenido",
    )
    lineas = formatear_eventos([e], tz)
    assert len(lineas) == 2
    # 17:05 UTC = 10:05 en Hermosillo (UTC-7, sin horario de verano)
    assert "2026-10-08 10:05" in lineas[1] and "carga_confirmada" in lineas[1]
    assert "29.07000,-110.95000" in lineas[1] and "3.00" in lineas[1]
    # sin coordenadas se indica claramente
    from dataclasses import replace

    sin = replace(e, latitude=None, longitude=None)
    assert "sin posición" in formatear_eventos([sin], tz)[1]
