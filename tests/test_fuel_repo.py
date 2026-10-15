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


def test_rendimiento_reciente_usa_la_config_del_autobus(monkeypatch):
    from types import SimpleNamespace

    from elena import fuel_repo
    from elena.rendimiento import PuntoRendimiento

    visto = {}
    cfg = SimpleNamespace(
        calibracion=CAL,
        umbral_carga_candidata=2.5,
        rendimiento_default=12.0,
        odometro_a_km=0.001,
    )
    monkeypatch.setattr(fuel_repo, "cargar_config", lambda conn, bus: cfg)

    def puntos(conn, bus, desde, hasta, calibracion):
        visto.update(bus=bus, desde=desde, hasta=hasta, calibracion=calibracion)
        return [
            PuntoRendimiento(
                T0 + timedelta(minutes=5 * i), 30.0 - i / 12, 1e5 + i * 1000
            )
            for i in range(60)
        ]

    monkeypatch.setattr(fuel_repo, "cargar_puntos_rendimiento", puntos)
    r = fuel_repo.rendimiento_reciente(None, "2", ahora=T0, km_ventana=100, dias=30)
    assert r.rendimiento_km_l == pytest.approx(12.0, abs=0.05)
    assert visto["bus"] == "2" and visto["hasta"] == T0
    assert visto["desde"] == T0 - timedelta(days=30) and visto["calibracion"] is CAL


def test_rendimiento_reciente_sin_config_propaga_el_error(monkeypatch):
    from elena import fuel_repo

    def sin_config(conn, bus):
        raise ConfigError("sin fila")

    monkeypatch.setattr(fuel_repo, "cargar_config", sin_config)
    with pytest.raises(ConfigError):
        fuel_repo.rendimiento_reciente(None, "9", ahora=T0)


def test_guardar_rendimiento_reciente_con_y_sin_estimacion():
    from elena.pipeline_repo import guardar_rendimiento_reciente
    from elena.rendimiento import RendimientoReciente

    class Conn:
        def __init__(self):
            self.llamadas = []

        def execute(self, sql, params=None):
            self.llamadas.append((sql, params))

    ok = RendimientoReciente(8.35, 3.1, 97.4, 11.66, 74, 3, T0, T0, None)
    c = Conn()
    guardar_rendimiento_reciente(c, "2", ok, 100.0)
    sql, p = c.llamadas[0]
    assert "ON CONFLICT (idautobus) DO UPDATE" in sql and "rendimiento_km_l" in sql
    assert p["bus"] == "2" and p["rend"] == 8.35 and p["error"] == 3.1
    assert p["km_ventana"] == 100.0 and p["motivo"] is None

    # sin estimación NO pisa el último valor bueno: solo cambia el motivo
    sin = RendimientoReciente(None, None, 0.0, 0.0, 0, 0, None, None, "sin_datos")
    c = Conn()
    guardar_rendimiento_reciente(c, "2", sin, 100.0)
    sql, p = c.llamadas[0]
    assert "rendimiento_km_l" not in sql.split("DO UPDATE")[1]
    assert p["motivo"] == "sin_datos"


def _config_falsa():
    from types import SimpleNamespace

    return SimpleNamespace(
        calibracion=CAL,
        umbral_carga_candidata=2.5,
        rendimiento_default=12.0,
        odometro_a_km=0.001,
    )


def _serie_km(i0, i1):
    from elena.rendimiento import PuntoRendimiento

    return [
        PuntoRendimiento(
            T0 + timedelta(minutes=5 * i), 40.0 - i / 12, 1e5 + i * 1000.0
        )
        for i in range(i0, i1)
    ]


def test_bloques_nuevos_sin_historial_empieza_dias_inicial_atras(monkeypatch):
    from elena import fuel_repo

    visto = {}
    monkeypatch.setattr(fuel_repo, "cargar_config", lambda conn, bus: _config_falsa())
    monkeypatch.setattr(fuel_repo, "ultimo_fin_historial", lambda conn, bus: None)

    def puntos(conn, bus, desde, hasta, calibracion):
        visto.update(desde=desde, hasta=hasta)
        return _serie_km(0, 130)

    def previos(*a, **k):
        raise AssertionError("sin historial no hace falta contexto previo")

    monkeypatch.setattr(fuel_repo, "cargar_puntos_rendimiento", puntos)
    monkeypatch.setattr(fuel_repo, "cargar_puntos_previos", previos)
    bs = fuel_repo.bloques_rendimiento_nuevos(
        None, "2", ahora=T0, km_bloque=50, dias_inicial=10
    )
    assert visto == {"desde": T0 - timedelta(days=10), "hasta": T0}
    assert len(bs) == 2 and all(b.rendimiento_km_l == 12.0 for b in bs)


def test_bloques_nuevos_con_historial_retoma_en_el_ultimo_fin(monkeypatch):
    from elena import fuel_repo
    from elena.rendimiento import calcular_bloques

    todo = _serie_km(0, 200)
    ancla = todo[50].ventana_inicio  # el primer bloque ya está guardado
    visto = {}
    monkeypatch.setattr(fuel_repo, "cargar_config", lambda conn, bus: _config_falsa())
    monkeypatch.setattr(fuel_repo, "ultimo_fin_historial", lambda conn, bus: ancla)

    def puntos(conn, bus, desde, hasta, calibracion):
        visto["desde"] = desde
        return [p for p in todo if p.ventana_inicio >= desde]

    def previos(conn, bus, antes, n, calibracion):
        visto["previos"] = (antes, n)
        return [p for p in todo if p.ventana_inicio < antes][-n:]

    monkeypatch.setattr(fuel_repo, "cargar_puntos_rendimiento", puntos)
    monkeypatch.setattr(fuel_repo, "cargar_puntos_previos", previos)
    nuevos = fuel_repo.bloques_rendimiento_nuevos(None, "2", ahora=T0, km_bloque=50)
    assert visto["desde"] == ancla and visto["previos"][0] == ancla
    # el resultado es el de calcular todo junto, menos lo que ya estaba guardado
    esperado = [
        b
        for b in calcular_bloques(
            todo, km_bloque=50, salto_litros=2.5, rendimiento_esperado=12.0
        )
        if b.inicio >= ancla
    ]
    assert nuevos == esperado and nuevos[0].inicio == ancla


def test_guardar_bloques_cuenta_solo_los_nuevos():
    from types import SimpleNamespace

    from elena.pipeline_repo import guardar_bloques
    from elena.rendimiento import calcular_bloques

    bloques = calcular_bloques(_serie_km(0, 130), km_bloque=50)
    assert len(bloques) == 2

    class Conn:
        def __init__(self):
            self.llamadas = []

        def execute(self, sql, params=None):
            self.llamadas.append((sql, params))
            # el segundo bloque "ya existía": ON CONFLICT DO NOTHING no inserta
            return SimpleNamespace(rowcount=0 if len(self.llamadas) == 2 else 1)

    c = Conn()
    assert guardar_bloques(c, "2", bloques, 50.0) == 1
    sql, p = c.llamadas[0]
    assert "ON CONFLICT (idautobus, fecha_inicio) DO NOTHING" in sql
    assert p["bus"] == "2" and p["km_bloque"] == 50.0 and p["completo"] is True
    assert p["inicio"] == bloques[0].inicio and p["rend"] == bloques[0].rendimiento_km_l


def test_guardar_bloques_toma_las_coordenadas_de_gps_records():
    from elena.pipeline_repo import (
        GUARDAR_RENDIMIENTO_RECIENTE_SQL,
        INSERTAR_BLOQUE_SQL,
    )

    for sql in (INSERTAR_BLOQUE_SQL, GUARDAR_RENDIMIENTO_RECIENTE_SQL):
        assert "public.gps_records" in sql and "coordenadas_inicio" in sql
        assert "coordenadas_fin" in sql and "latitud_" not in sql
        assert "recorded_at" in sql and "created_at" not in sql
        assert "{t}" not in sql  # las plantillas quedaron resueltas
