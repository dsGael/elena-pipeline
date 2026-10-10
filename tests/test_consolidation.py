from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from elena.consolidation import (
    NoStartError,
    WindowAggregate,
    align_down,
    build_window,
    closed_window_limit,
    consolidate,
    plan_range,
)
from elena.quality import QualityRules

from .conftest import IMEI_SIN_ASIGNAR, IMEI_SIN_CONFIG, IMEI_YARIS

HMO = ZoneInfo("America/Hermosillo")


# --------------------------------------------------------------------------------------
# Tiempo (sin base de datos)
# --------------------------------------------------------------------------------------


def test_align_down_en_utc_y_en_hora_local():
    assert align_down(datetime(2026, 10, 8, 17, 7, 42, 123, tzinfo=UTC)) == datetime(
        2026, 10, 8, 17, 5, tzinfo=UTC
    )
    assert align_down(datetime(2026, 10, 8, 10, 4, 59, tzinfo=HMO)) == datetime(
        2026, 10, 8, 10, 0, tzinfo=HMO
    )
    # 10:00 local de Hermosillo = 17:00 UTC: la rejilla coincide con las marcas locales
    local = align_down(datetime(2026, 10, 8, 10, 9, tzinfo=HMO))
    assert local.astimezone(UTC) == datetime(2026, 10, 8, 17, 5, tzinfo=UTC)


def test_align_down_exige_zona_horaria_y_minutos_validos():
    with pytest.raises(ValueError):
        align_down(datetime(2026, 10, 8, 10, 0))  # noqa: DTZ001 (naive a propósito)
    with pytest.raises(ValueError):
        align_down(datetime(2026, 10, 8, 10, 0, tzinfo=UTC), minutes=7)


def test_closed_window_limit_deja_colchon():
    now = datetime(2026, 10, 8, 10, 6, 30, tzinfo=HMO)
    # con 2 min de colchón: 10:04:30 -> la ventana 10:00-10:05 aún no está cerrada
    assert closed_window_limit(now, lag_minutes=2) == datetime(
        2026, 10, 8, 10, 0, tzinfo=HMO
    )
    now2 = datetime(2026, 10, 8, 10, 7, 1, tzinfo=HMO)
    assert closed_window_limit(now2, lag_minutes=2) == datetime(
        2026, 10, 8, 10, 5, tzinfo=HMO
    )


# --------------------------------------------------------------------------------------
# build_window (puro)
# --------------------------------------------------------------------------------------


def _agg(**kw) -> WindowAggregate:
    base = dict(
        imei=IMEI_YARIS,
        idautobus="YARIS1",
        ventana_inicio=datetime(2026, 10, 8, 17, 0, tzinfo=UTC),
        fcc_inclinacion=2.6,
        angulo_max=5.0,
        min_muestras=None,
        muestras_totales=15,
        muestras_validas=15,
        sensor_mediana=5036.004,
        sensor_promedio=5036.5678,
        sensor_min=5030.0,
        sensor_max=5042.0,
        inclinacion_mediana=2.9,
        muestras_con_angulo=15,
        muestras_angulo_valido=15,
        velocidad_promedio=0.0,
        muestras_con_ignicion=15,
        muestras_ignicion_on=15,
        odometro_total=1000.0,
    )
    base.update(kw)
    return WindowAggregate(**base)


def test_build_window_redondea_y_calcula_fin():
    w = build_window(_agg())
    assert w.ventana_fin == datetime(2026, 10, 8, 17, 5, tzinfo=UTC)
    assert w.sensor_mediana == 5036.0 and w.sensor_promedio == 5036.57
    assert w.angulo_corregido == pytest.approx(0.3)
    assert w.calidad_ventana == "valida" and w.motivo_calidad is None


def test_build_window_usa_fcc_y_umbral_del_vehiculo():
    # mismo ángulo, otro FCC: 2.9 - 10 = -7.1 -> fuera de rango
    w = build_window(_agg(fcc_inclinacion=10.0))
    assert w.calidad_ventana == "dudosa" and w.motivo_calidad == "angulo_fuera_de_rango"
    # umbral propio más amplio lo vuelve válido
    assert (
        build_window(_agg(fcc_inclinacion=10.0, angulo_max=8.0)).calidad_ventana
        == "valida"
    )


def test_build_window_min_muestras_viene_de_la_config_y_reglas_extra_de_base_rules():
    assert (
        build_window(_agg(muestras_validas=8, min_muestras=10)).motivo_calidad
        == "muestras_insuficientes"
    )
    extra = QualityRules(fcc_inclinacion=0.0, max_rango_sensor=5.0)
    assert build_window(_agg(), base_rules=extra).motivo_calidad == "sensor_inestable"


# --------------------------------------------------------------------------------------
# Integración con PostgreSQL de pruebas
# --------------------------------------------------------------------------------------

T0 = datetime(2026, 10, 8, 17, 0, tzinfo=UTC)  # 10:00 hora de Hermosillo


def _insert_samples(
    conn,
    imei: str,
    start: datetime,
    n: int = 15,
    *,
    sensor: float | None = 5036.0,
    incl: float | None = 2.9,
    speed: float = 0.0,
    ign: bool | None = True,
    odo: float = 1000.0,
    step: int = 20,
    created_delay: timedelta = timedelta(seconds=5),
) -> None:
    rows = []
    for i in range(n):
        ts = start + timedelta(seconds=step * i)
        # ruido determinista alrededor de la mediana
        s = None if sensor is None else sensor + (i % 3 - 1)
        rows.append(
            (imei, ts, 29.07, -110.95, speed, s, incl, ign, odo + i, ts + created_delay)
        )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO gps_records (imei, recorded_at, latitude, longitude, speed, s_analogo, "
            "inclinacion_vertical, ignicion, odometro_total, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )


def _windows(conn):
    cur = conn.execute(
        "SELECT imei, ventana_inicio, ventana_fin, muestras_totales, muestras_validas, "
        "sensor_mediana, sensor_promedio, sensor_min, sensor_max, inclinacion_mediana, "
        "angulo_corregido, porcentaje_angulo_valido, velocidad_promedio, ignicion_dominante, "
        "odometro_total, calidad_ventana, motivo_calidad, id "
        "FROM fuel_windows ORDER BY ventana_inicio, imei"
    )
    names = [c.name for c in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


@pytest.mark.integration
def test_tres_ventanas_valida_dudosa_descartada(conn):
    _insert_samples(conn, IMEI_YARIS, T0)  # 10:00 plana, válida
    _insert_samples(
        conn, IMEI_YARIS, T0 + timedelta(minutes=5), incl=9.0
    )  # 10:05 inclinada
    _insert_samples(
        conn, IMEI_YARIS, T0 + timedelta(minutes=10), sensor=None
    )  # 10:10 sin sensor

    s = consolidate(conn, T0, T0 + timedelta(minutes=15))
    assert (s.ventanas, s.insertadas, s.actualizadas) == (3, 3, 0)
    assert dict(s.por_calidad) == {"valida": 1, "dudosa": 1, "descartada": 1}

    w = _windows(conn)
    assert [x["calidad_ventana"] for x in w] == ["valida", "dudosa", "descartada"]

    v = w[0]
    assert v["muestras_totales"] == v["muestras_validas"] == 15
    assert float(v["sensor_mediana"]) == 5036.0
    assert float(v["sensor_min"]) == 5035.0 and float(v["sensor_max"]) == 5037.0
    assert float(v["inclinacion_mediana"]) == 2.9
    assert float(v["angulo_corregido"]) == pytest.approx(0.3)
    assert float(v["porcentaje_angulo_valido"]) == 100.0
    assert v["ignicion_dominante"] is True
    assert v["motivo_calidad"] is None
    assert v["ventana_fin"] == v["ventana_inicio"] + timedelta(minutes=5)

    d = w[1]
    assert d["motivo_calidad"] == "angulo_fuera_de_rango"
    assert float(d["angulo_corregido"]) == pytest.approx(6.4)
    assert (
        float(d["porcentaje_angulo_valido"]) == 0.0
    )  # los datos siguen ahí para diagnosticar
    assert (
        float(d["sensor_mediana"]) == 5036.0
    )  # el sensor NO se descarta por el ángulo

    x = w[2]
    assert x["muestras_validas"] == 0 and x["muestras_totales"] == 15
    assert x["sensor_mediana"] is None
    assert x["motivo_calidad"] == "sin_lectura_sensor"


@pytest.mark.integration
def test_usa_recorded_at_y_no_created_at(conn):
    # el equipo registró a las 10:00-10:05 pero el servidor lo recibió dos horas después
    _insert_samples(conn, IMEI_YARIS, T0, created_delay=timedelta(hours=2))
    s = consolidate(conn, T0, T0 + timedelta(minutes=5))
    assert s.ventanas == 1
    assert _windows(conn)[0]["ventana_inicio"] == T0
    # y no aparece en la ventana donde se recibió
    assert (
        consolidate(
            conn, T0 + timedelta(hours=2), T0 + timedelta(hours=2, minutes=5)
        ).ventanas
        == 0
    )


@pytest.mark.integration
def test_idempotente_y_recoge_datos_tardios(conn):
    _insert_samples(conn, IMEI_YARIS, T0, n=10)
    consolidate(conn, T0, T0 + timedelta(minutes=5))
    antes = _windows(conn)

    s2 = consolidate(conn, T0, T0 + timedelta(minutes=5))
    assert (s2.insertadas, s2.actualizadas) == (0, 1)
    assert _windows(conn) == antes  # nada cambia al repetir

    # llegan 5 muestras tardías de la misma ventana
    _insert_samples(conn, IMEI_YARIS, T0 + timedelta(seconds=200), n=5)
    s3 = consolidate(conn, T0, T0 + timedelta(minutes=5))
    assert (s3.insertadas, s3.actualizadas) == (0, 1)
    despues = _windows(conn)
    assert len(despues) == 1
    assert (
        despues[0]["id"] == antes[0]["id"]
    )  # misma fila: lo ligado a su id sigue válido
    assert despues[0]["muestras_totales"] == 15


@pytest.mark.integration
def test_deduplica_muestras_repetidas(conn):
    _insert_samples(conn, IMEI_YARIS, T0, n=15)
    _insert_samples(
        conn, IMEI_YARIS, T0, n=15, created_delay=timedelta(seconds=9)
    )  # reenvío exacto
    consolidate(conn, T0, T0 + timedelta(minutes=5))
    assert _windows(conn)[0]["muestras_totales"] == 15


@pytest.mark.integration
def test_solo_consolida_imei_con_autobus_y_configuracion(conn):
    _insert_samples(conn, IMEI_YARIS, T0)
    _insert_samples(conn, IMEI_SIN_CONFIG, T0)
    _insert_samples(conn, IMEI_SIN_ASIGNAR, T0)
    _insert_samples(
        conn, "999999999999999", T0
    )  # IMEI que ni siquiera está en dispositivos
    s = consolidate(conn, T0, T0 + timedelta(minutes=5))
    assert s.imeis == {IMEI_YARIS} and s.ventanas == 1


@pytest.mark.integration
def test_limites_de_ventana_y_rango_exclusivo(conn):
    # una muestra justo en 10:05:00 pertenece a la ventana de las 10:05, no a la de las 10:00
    _insert_samples(
        conn, IMEI_YARIS, T0 + timedelta(minutes=4, seconds=40), n=2
    )  # 10:04:40 y 10:05:00
    consolidate(conn, T0, T0 + timedelta(minutes=10))
    w = _windows(conn)
    assert [(x["ventana_inicio"], x["muestras_totales"]) for x in w] == [
        (T0, 1),
        (T0 + timedelta(minutes=5), 1),
    ]
    # consolidar solo hasta las 10:05 deja fuera la muestra de las 10:05:00
    conn.execute("DELETE FROM fuel_windows")
    s = consolidate(conn, T0, T0 + timedelta(minutes=5))
    assert s.ventanas == 1 and _windows(conn)[0]["muestras_totales"] == 1


@pytest.mark.integration
def test_dry_run_no_escribe(conn):
    _insert_samples(conn, IMEI_YARIS, T0)
    s = consolidate(conn, T0, T0 + timedelta(minutes=5), dry_run=True, keep_sample=5)
    assert s.ventanas == 1 and s.insertadas == 0 and len(s.muestra) == 1
    assert _windows(conn) == []


@pytest.mark.integration
def test_chunks_dan_el_mismo_resultado(conn):
    for k in range(4):
        _insert_samples(conn, IMEI_YARIS, T0 + timedelta(minutes=5 * k))
    s = consolidate(conn, T0, T0 + timedelta(minutes=20), chunk_hours=1)
    conn.execute("DELETE FROM fuel_windows")
    s_grande = consolidate(conn, T0, T0 + timedelta(minutes=20), chunk_hours=24)
    assert s.ventanas == s_grande.ventanas == 4


@pytest.mark.integration
def test_advertencia_por_umbral_pendiente(conn):
    _insert_samples(conn, IMEI_YARIS, T0)
    s = consolidate(conn, T0, T0 + timedelta(minutes=5), dry_run=True)
    assert any("min_muestras_ventana" in a for a in s.advertencias)
    conn.execute(
        "UPDATE fuel_config SET min_muestras_ventana = 10 WHERE idautobus = 'YARIS1'"
    )
    s = consolidate(conn, T0, T0 + timedelta(minutes=5), dry_run=True)
    assert s.advertencias == []
    conn.execute(
        "UPDATE fuel_config SET min_muestras_ventana = 20 WHERE idautobus = 'YARIS1'"
    )
    consolidate(conn, T0, T0 + timedelta(minutes=5))
    assert _windows(conn)[0]["motivo_calidad"] == "muestras_insuficientes"


@pytest.mark.integration
def test_plan_range(conn):
    now = datetime(2026, 10, 8, 10, 22, tzinfo=HMO)  # 10:22 local
    with pytest.raises(NoStartError):
        plan_range(
            conn, now=now, desde=None, hasta=None, lag_minutes=2, overlap_minutes=60
        )

    # con --desde explícito no hace falta historia
    d, h = plan_range(
        conn,
        now=now,
        desde=datetime(2026, 10, 8, 9, 58, tzinfo=HMO),
        hasta=None,
        lag_minutes=2,
        overlap_minutes=60,
    )
    assert d == datetime(2026, 10, 8, 9, 55, tzinfo=HMO)
    assert h == datetime(2026, 10, 8, 10, 20, tzinfo=HMO)

    # con ventanas guardadas retoma desde la última menos el traslape
    _insert_samples(conn, IMEI_YARIS, T0)
    consolidate(conn, T0, T0 + timedelta(minutes=5))
    d, _ = plan_range(
        conn, now=now, desde=None, hasta=None, lag_minutes=2, overlap_minutes=15
    )
    assert d == T0 + timedelta(minutes=5) - timedelta(minutes=15)
