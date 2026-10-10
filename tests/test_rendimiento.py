import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from elena.rendimiento import (
    M_HUECO,
    M_POCOS_KM,
    M_POCOS_PUNTOS,
    M_SALTO,
    M_SIN_CONSUMO,
    PuntoRendimiento,
    estimar_rendimiento,
)

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("America/Hermosillo"))


def serie(
    n=60,
    *,
    kmpl=12.0,
    km_por_ventana=1.0,
    sigma=0.0,
    seed=1,
    litros0=30.0,
    saltos=None,
    inicio=0,
    odo0=100_000.0,
):
    """n ventanas de 5 min; odómetro en METROS; ``saltos`` = {indice: delta_litros}."""
    rnd = random.Random(seed)
    litros, odo, pts = litros0, odo0, []
    for i in range(n):
        litros += (saltos or {}).get(i, 0.0)
        ruido = rnd.gauss(0, sigma) if sigma else 0.0
        pts.append(
            PuntoRendimiento(
                T0 + timedelta(minutes=5 * (inicio + i)),
                round(litros + ruido, 2),
                odo,
            )
        )
        litros -= km_por_ventana / kmpl
        odo += km_por_ventana * 1000
    return pts


def test_sin_ruido_recupera_el_rendimiento_exacto():
    e = estimar_rendimiento(serie(60), min_litros=1)
    assert e.rendimiento_km_l == pytest.approx(12.0, abs=0.05)
    assert e.tramos_usados == 1 and e.puntos_usados == 60 and not e.descartes


def test_con_ruido_del_sensor_sigue_cerca_del_real():
    # Con ruido de 0.8 L el método de pares sesgaba 12 km/L hacia ~2; la regresión no.
    for seed in range(5):
        e = estimar_rendimiento(serie(300, sigma=0.8, seed=seed))
        assert 11.0 <= e.rendimiento_km_l <= 13.0, (seed, e.rendimiento_km_l)


def test_una_carga_en_medio_corta_el_tramo_y_no_contamina():
    e = estimar_rendimiento(serie(200, saltos={100: +10.0}, litros0=15.0))
    assert e.descartes[M_SALTO] == 1
    assert e.tramos_usados == 2
    assert abs(e.rendimiento_km_l - 12.0) < 0.2


def test_una_extraccion_en_medio_tampoco_contamina():
    e = estimar_rendimiento(serie(200, saltos={100: -8.0}, litros0=30.0))
    assert e.descartes[M_SALTO] == 1
    assert abs(e.rendimiento_km_l - 12.0) < 0.2


def test_carga_con_ruido_se_detecta_y_se_excluye():
    e = estimar_rendimiento(serie(300, sigma=0.5, saltos={150: +12.0}, litros0=18.0))
    assert e.descartes[M_SALTO] >= 1
    assert 11.0 <= e.rendimiento_km_l <= 13.0


def test_hueco_largo_parte_en_dos_tramos():
    a = serie(40)
    b = serie(40, inicio=40 + 20, odo0=140_000.0, litros0=30.0 - 40 / 12)
    e = estimar_rendimiento(a + b, min_litros=1)
    assert e.descartes[M_HUECO] == 1 and e.tramos_usados == 2
    assert abs(e.rendimiento_km_l - 12.0) < 0.1


def test_tramo_con_pocos_puntos_o_pocos_km_se_descarta():
    assert estimar_rendimiento(serie(4)).descartes[M_POCOS_PUNTOS] == 1
    # 20 ventanas pero casi sin avanzar (0.2 km cada una = 4 km)
    corto = estimar_rendimiento(serie(20, km_por_ventana=0.2))
    assert corto.descartes[M_POCOS_KM] == 1 and corto.rendimiento_km_l is None


def test_odometro_faltante_o_que_retrocede_corta_tramos():
    pts = serie(40)
    pts[20] = PuntoRendimiento(pts[20].ventana_inicio, pts[20].litros, None)
    e = estimar_rendimiento(pts, min_litros=1)
    assert e.descartes[M_HUECO] >= 1
    atras = serie(40)
    atras[25] = PuntoRendimiento(atras[25].ventana_inicio, atras[25].litros, 0.0)
    assert estimar_rendimiento(atras, min_litros=1).descartes[M_HUECO] >= 1


def test_nivel_que_no_baja_no_aporta_consumo():
    e = estimar_rendimiento(serie(60, km_por_ventana=1.0, kmpl=-12.0))
    assert e.descartes[M_SIN_CONSUMO] == 1 and e.rendimiento_km_l is None


def test_pocos_litros_acumulados_no_da_estimacion():
    e = estimar_rendimiento(serie(15))  # 15 km, 1.25 L
    assert e.rendimiento_km_l is None and 1.0 < e.litros < 1.5


def test_desordenado_se_ordena_por_fecha():
    pts = serie(60)
    assert estimar_rendimiento(
        pts[::-1], min_litros=1
    ).rendimiento_km_l == pytest.approx(12.0, abs=0.05)