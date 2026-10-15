import itertools
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from elena.rendimiento import (
    CIERRE_CARGA,
    CIERRE_KM,
    CIERRE_ODOMETRO,
    ESTADO_OK,
    ESTADO_POCOS_PUNTOS,
    ESTADO_SIN_CONSUMO,
    M_HUECO,
    M_ODOMETRO,
    M_POCOS_KM,
    M_POCOS_PUNTOS,
    M_SALTO,
    M_SEGMENTO_CORTO,
    M_SIN_CONSUMO,
    M_SIN_ODOMETRO,
    MOTIVO_SIN_CONSUMO,
    MOTIVO_SIN_DATOS,
    MOTIVO_SIN_RECORRIDO,
    PuntoRendimiento,
    calcular_bloques,
    estimar_rendimiento,
    estimar_rendimiento_reciente,
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


# --------------------------------------------------------------------------------------
# Rendimiento reciente
# --------------------------------------------------------------------------------------


def con_paradas(
    bloques,
    *,
    kmpl=12.0,
    sigma=0.0,
    seed=1,
    litros0=30.0,
    odo0=100_000.0,
    km_por_ventana=1.0,
    cambios=None,
):
    """Bloques de ventanas separados por paradas con la ignición apagada.

    ``bloques`` = [(n_ventanas, minutos_de_parada_despues), ...]. En la parada no hay
    lecturas y ni el odómetro ni el nivel cambian. ``cambios`` = {índice_global: delta_litros}
    (cargas o extracciones, también las que ocurren durante una parada).
    """
    rnd = random.Random(seed)
    t, litros, odo, pts, i = T0, litros0, odo0, [], 0
    for n, parada_min in bloques:
        for _ in range(n):
            litros += (cambios or {}).get(i, 0.0)
            ruido = rnd.gauss(0, sigma) if sigma else 0.0
            pts.append(PuntoRendimiento(t, round(litros + ruido, 2), odo))
            litros -= km_por_ventana / kmpl
            odo += km_por_ventana * 1000
            t += timedelta(minutes=5)
            i += 1
        t += timedelta(minutes=parada_min)
    return pts


def test_reciente_sin_ruido_recupera_el_rendimiento_exacto():
    r = estimar_rendimiento_reciente(serie(60))
    assert r.rendimiento_km_l == pytest.approx(12.0, abs=0.05)
    assert r.km == pytest.approx(59.0) and r.puntos == 60 and r.segmentos == 1
    assert r.motivo is None and r.error_pct is not None and r.error_pct < 1.0
    assert r.litros == pytest.approx(59.0 / 12.0, abs=0.05)


def test_reciente_usa_solo_los_ultimos_km():
    pts = serie(300)
    r = estimar_rendimiento_reciente(pts)  # km_ventana = 100
    assert r.km == pytest.approx(100.0) and r.puntos == 101
    assert r.hasta == pts[-1].ventana_inicio and r.desde == pts[-101].ventana_inicio
    assert estimar_rendimiento_reciente(pts, km_ventana=40).km == pytest.approx(40.0)


def test_reciente_con_ruido_del_sensor_sigue_cerca_del_real():
    for seed in range(8):
        r = estimar_rendimiento_reciente(serie(300, sigma=0.8, seed=seed))
        assert 11.0 <= r.rendimiento_km_l <= 13.0, (seed, r.rendimiento_km_l)
        assert 1.0 < r.error_pct < 8.0, (seed, r.error_pct)


def test_reciente_las_paradas_no_cortan_la_serie():
    # 10 recorridos de 8 ventanas (7 km) con paradas de 2 h. Con la ignición apagada no hay
    # lecturas, así que el método por tramos continuos nunca junta 10 km y no estima nada.
    pts = con_paradas([(8, 120)] * 10, sigma=0.3, seed=2)
    assert estimar_rendimiento(pts, min_litros=1).rendimiento_km_l is None
    r = estimar_rendimiento_reciente(pts)
    assert abs(r.rendimiento_km_l - 12.0) < 1.0
    assert r.segmentos == 1 and not r.descartes


def test_reciente_una_carga_en_medio_corta_y_no_contamina():
    r = estimar_rendimiento_reciente(
        serie(200, saltos={100: +10.0}, litros0=15.0), km_ventana=1000
    )
    assert r.descartes[M_SALTO] == 1 and r.segmentos == 2
    assert abs(r.rendimiento_km_l - 12.0) < 0.2


def test_reciente_una_extraccion_en_medio_tampoco_contamina():
    r = estimar_rendimiento_reciente(
        serie(200, saltos={100: -8.0}, litros0=30.0), km_ventana=1000
    )
    assert r.descartes[M_SALTO] == 1
    assert abs(r.rendimiento_km_l - 12.0) < 0.2


def test_reciente_carga_durante_una_parada_se_detecta_a_traves_del_hueco():
    pts = con_paradas([(30, 180), (30, 0)], cambios={30: +10.0}, litros0=20.0)
    r = estimar_rendimiento_reciente(pts)
    assert r.descartes[M_SALTO] == 1 and r.segmentos == 2
    assert abs(r.rendimiento_km_l - 12.0) < 0.2


def test_reciente_km_sin_datos_no_se_confunden_con_una_extraccion():
    pts = serie(80)
    sin_datos = pts[:30] + pts[60:]  # 30 km recorridos sin lecturas: ~2.5 L menos
    # sin saber el rendimiento esperado, esa bajada parece una extracción y corta
    assert estimar_rendimiento_reciente(sin_datos).descartes[M_SALTO] == 1
    # sabiéndolo se descuenta el consumo normal y la serie sigue entera
    r = estimar_rendimiento_reciente(sin_datos, rendimiento_esperado=12.0)
    assert r.descartes[M_SALTO] == 0 and r.segmentos == 1
    assert abs(r.rendimiento_km_l - 12.0) < 0.1


def test_reciente_lo_reciente_manda():
    a = serie(100, kmpl=12.0, litros0=40.0)
    b = serie(100, kmpl=6.0, litros0=40.0 - 100 / 12, inicio=100, odo0=200_000.0)
    pts = a + b
    assert estimar_rendimiento_reciente(pts, km_ventana=50).rendimiento_km_l == (
        pytest.approx(6.0, abs=0.1)
    )
    todo = estimar_rendimiento_reciente(pts, km_ventana=1000)
    assert 7.5 < todo.rendimiento_km_l < 8.5  # 199 km / (8.3 L + 16.5 L)


def test_reciente_odometro_que_retrocede_corta_pero_sigue_estimando():
    a = serie(40)
    b = serie(40, inicio=40, odo0=0.0, litros0=30.0 - 40 / 12)
    r = estimar_rendimiento_reciente(a + b)
    assert r.descartes[M_ODOMETRO] == 1 and r.segmentos == 2
    assert abs(r.rendimiento_km_l - 12.0) < 0.1


def test_reciente_ventana_sin_odometro_se_ignora_sin_cortar():
    pts = serie(40)
    pts[20] = PuntoRendimiento(pts[20].ventana_inicio, pts[20].litros, None)
    r = estimar_rendimiento_reciente(pts)
    assert r.descartes[M_SIN_ODOMETRO] == 1 and r.segmentos == 1
    assert abs(r.rendimiento_km_l - 12.0) < 0.1


def test_reciente_nivel_que_no_baja_no_da_estimacion():
    r = estimar_rendimiento_reciente(serie(60, kmpl=-12.0))
    assert r.rendimiento_km_l is None and r.motivo == MOTIVO_SIN_CONSUMO
    assert r.km > 50 and r.error_pct is None


def test_reciente_sin_datos_o_sin_recorrido():
    assert estimar_rendimiento_reciente([]).motivo == MOTIVO_SIN_DATOS
    poco = estimar_rendimiento_reciente(serie(2))
    assert poco.motivo == MOTIVO_SIN_DATOS and poco.descartes[M_SEGMENTO_CORTO] == 1
    quieto = estimar_rendimiento_reciente(serie(10, km_por_ventana=0.0))
    assert quieto.motivo == MOTIVO_SIN_RECORRIDO and quieto.rendimiento_km_l is None


def test_reciente_segmento_corto_tras_una_carga_se_salta_y_usa_el_anterior():
    pts = serie(61, saltos={57: +10.0})  # solo 4 lecturas después de la carga
    r = estimar_rendimiento_reciente(pts)
    assert r.descartes[M_SEGMENTO_CORTO] >= 1
    assert abs(r.rendimiento_km_l - 12.0) < 0.2
    assert r.hasta < pts[-1].ventana_inicio  # el dato más nuevo aún no cuenta


def test_reciente_el_error_crece_con_el_ruido():
    poco = estimar_rendimiento_reciente(serie(150, sigma=0.2, seed=3))
    mucho = estimar_rendimiento_reciente(serie(150, sigma=1.0, seed=3))
    assert mucho.error_pct > 2 * poco.error_pct


def test_reciente_desordenado_se_ordena_por_fecha():
    pts = serie(60)
    assert estimar_rendimiento_reciente(pts[::-1]).rendimiento_km_l == (
        pytest.approx(12.0, abs=0.05)
    )


# --------------------------------------------------------------------------------------
# Historial por bloques
# --------------------------------------------------------------------------------------


def test_bloques_consecutivos_comparten_extremo_y_dan_el_rendimiento_exacto():
    bs = calcular_bloques(serie(230))
    assert len(bs) == 4  # 50, 100, 150 y 200 km; el resto sigue abierto
    for b in bs:
        assert b.estado == ESTADO_OK and b.completo and b.cierre == CIERRE_KM
        assert b.km == pytest.approx(50.0) and b.puntos == 51
        assert b.rendimiento_km_l == pytest.approx(12.0, abs=0.05)
        assert b.litros_consumidos == pytest.approx(50 / 12, abs=0.05)
        assert b.litros_inicio - b.litros_fin == pytest.approx(
            b.litros_consumidos, abs=0.02
        )
    for a, b in itertools.pairwise(bs):
        assert a.fin == b.inicio and a.odometro_fin_km == b.odometro_inicio_km


def test_bloques_no_sale_un_bloque_que_aun_puede_cambiar():
    # el bloque 0..50 km cierra en el punto 50; hace falta margen (k_salto + recorte + 1)
    assert calcular_bloques(serie(57)) == []
    assert len(calcular_bloques(serie(58))) == 1


def test_bloques_una_carga_nunca_queda_dentro_de_un_bloque():
    pts = serie(200, saltos={100: +10.0}, litros0=15.0)
    bs = calcular_bloques(pts)
    t_carga = pts[100].ventana_inicio
    assert not any(b.inicio < t_carga < b.fin for b in bs)
    assert all(b.estado == ESTADO_OK for b in bs)
    assert all(abs(b.rendimiento_km_l - 12.0) < 0.3 for b in bs)
    incompletos = [b for b in bs if not b.completo]
    assert len(incompletos) == 1 and incompletos[0].cierre == CIERRE_CARGA
    assert incompletos[0].km == pytest.approx(46.0)  # lo que quedó antes de la carga


def test_bloques_trozo_corto_antes_de_una_carga_no_se_guarda():
    pts = serie(120, saltos={20: +10.0}, litros0=15.0)  # solo ~16 km antes de la carga
    assert calcular_bloques(pts, min_km_parcial=20) != []  # los de después sí
    assert not any(b.cierre == CIERRE_CARGA for b in calcular_bloques(pts, min_km_parcial=20))
    assert any(b.cierre == CIERRE_CARGA for b in calcular_bloques(pts, min_km_parcial=5))


def test_bloques_odometro_que_retrocede_cierra_el_bloque():
    a = serie(70)
    b = serie(70, inicio=70, odo0=0.0, litros0=30.0 - 70 / 12)
    bs = calcular_bloques(a + b)
    assert [x.cierre for x in bs].count(CIERRE_ODOMETRO) == 1
    assert all(abs(x.rendimiento_km_l - 12.0) < 0.1 for x in bs)


def test_bloques_las_paradas_no_cortan_un_bloque():
    pts = con_paradas([(8, 120)] * 20, sigma=0.3, seed=2)  # ~7 km por recorrido
    bs = calcular_bloques(pts)
    assert bs and all(b.completo for b in bs)
    assert all(abs(b.rendimiento_km_l - 12.0) < 2.0 for b in bs)


def test_bloques_estados_sin_consumo_y_pocos_puntos():
    sin = calcular_bloques(serie(80, kmpl=-12.0))
    assert sin and sin[0].estado == ESTADO_SIN_CONSUMO
    assert sin[0].rendimiento_km_l is None and sin[0].litros_consumidos is None
    assert sin[0].litros_inicio is not None  # el nivel ajustado sí se guarda
    # dos lecturas separadas 60 km: se guardan los km, pero sin rendimiento
    raros = calcular_bloques(
        serie(12, km_por_ventana=60.0, kmpl=60.0), rendimiento_esperado=60.0
    )
    assert raros and raros[0].estado == ESTADO_POCOS_PUNTOS and raros[0].km == 60.0
    assert raros[0].rendimiento_km_l is None


def _por_ciclos(pts, paso, **kw):
    """Simula el run-cycle: cada ciclo ve más datos y retoma en el fin del último bloque."""
    guardados = {}
    contexto = 2 * 5 + 1 + 1  # lo que carga la base antes del ancla
    for corte in [*range(paso, len(pts), paso), len(pts)]:
        ancla = max((b.fin for b in guardados.values()), default=None)
        if ancla is None:
            visibles = pts[:corte]
        else:
            i = next(k for k, p in enumerate(pts) if p.ventana_inicio >= ancla)
            visibles = pts[max(0, i - contexto) : corte]
        for b in calcular_bloques(visibles, desde=ancla, **kw):
            guardados.setdefault(b.inicio, b)  # ON CONFLICT DO NOTHING
    return [guardados[k] for k in sorted(guardados)]


def test_bloques_por_ciclos_da_lo_mismo_que_calcular_todo():
    escenarios = [
        con_paradas([(15, 90)] * 30, sigma=0.4, seed=5),
        con_paradas(
            [(12, 60)] * 12 + [(12, 240)] * 12,
            sigma=0.5,
            seed=9,
            cambios={60: +12.0, 150: -6.0},
            litros0=25.0,
        ),
        serie(400, sigma=0.6, seed=4, saltos={130: +9.0, 300: +9.0}, litros0=18.0),
    ]
    for pts in escenarios:
        completo = calcular_bloques(pts)
        assert len(completo) >= 3
        for paso in (1, 2, 3, 7, 25, 90):
            assert _por_ciclos(pts, paso) == completo, f"paso={paso}"


def test_bloques_sumar_km_y_litros_da_el_rendimiento_global():
    pts = con_paradas([(15, 90)] * 40, kmpl=9.0, sigma=0.4, seed=11)
    bs = [b for b in calcular_bloques(pts) if b.estado == ESTADO_OK]
    assert len(bs) >= 5
    global_ = sum(b.km for b in bs) / sum(b.litros_consumidos for b in bs)
    assert abs(global_ - 9.0) < 0.5
