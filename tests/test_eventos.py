"""Detección de eventos (pura, sin PostgreSQL)."""

from datetime import UTC, datetime, timedelta

from elena.eventos import (
    HUECO_MAX_RALENTI,
    Evento,
    Muestra,
    Ventana,
    cargas_extracciones,
    maniobras,
    ralentis,
)

T0 = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def _m(minuto, *, ign=True, speed=0.0, mov=None, man=None, umbral=5):
    return Muestra(
        "I1", "BUS1", T0 + timedelta(minutes=minuto), ign, speed, mov, man, umbral
    )


def _serie_ralenti(minutos, **kw):
    """Una muestra en marcha y luego una quieta por minuto durante ``minutos``."""
    return [_m(0, speed=40.0)] + [_m(1 + k, **kw) for k in range(minutos + 1)]


def test_ralenti_que_llega_al_umbral_da_un_evento_con_sus_minutos():
    assert ralentis(_serie_ralenti(6)) == [
        Evento("BUS1", T0 + timedelta(minutes=1), "ralenti", 6.0)
    ]


def test_ralenti_corto_no_cuenta():
    assert ralentis(_serie_ralenti(4)) == []


def test_ralenti_con_ignicion_apagada_no_cuenta():
    assert ralentis(_serie_ralenti(10, ign=False)) == []
    assert ralentis(_serie_ralenti(10, ign=None)) == []


def test_ralenti_movimiento_o_velocidad_bastan_para_estar_quieto():
    # el GPS deriva a 5 km/h pero el equipo dice que no se mueve
    assert len(ralentis(_serie_ralenti(6, speed=5.0, mov=0))) == 1
    # el equipo marca movimiento (vibración / fuente = ignición) pero la velocidad es 0
    assert len(ralentis(_serie_ralenti(6, speed=0.0, mov=1))) == 1
    # en marcha de verdad
    assert ralentis(_serie_ralenti(6, speed=20.0, mov=1)) == []


def test_un_hueco_largo_corta_el_ralenti():
    hueco = HUECO_MAX_RALENTI.total_seconds() / 60 + 1
    serie = [_m(0, speed=40.0), _m(1), _m(4), _m(4 + hueco), _m(7 + hueco)]
    assert ralentis(serie) == []


def test_ralenti_que_empieza_en_la_primera_muestra_leida_se_descarta():
    assert ralentis([_m(k) for k in range(10)]) == []


def test_maniobras_1_a_3_y_otros_valores_se_ignoran():
    serie = [_m(0, man=1), _m(1, man=2), _m(2, man=3), _m(3, man=0), _m(4, man=None)]
    assert [e.evento for e in maniobras(serie)] == [
        "aceleracion",
        "frenado",
        "giro_brusco",
    ]
    assert all(e.cantidad is None for e in maniobras(serie))


def _v(k, litros, odo=1000.0, umbral=5.0):
    return Ventana("I1", "BUS1", T0 + timedelta(minutes=5 * k), odo, litros, umbral)


def test_carga_estacionado_suma_la_racha_y_toma_la_primera_ventana_que_cambio():
    serie = [_v(0, 10.0, odo=0.0)] + [
        _v(k + 1, lt) for k, lt in enumerate([10.0, 10.0, 22.0, 34.0, 34.0])
    ]
    assert cargas_extracciones(serie) == [
        Evento("BUS1", T0 + timedelta(minutes=15), "carga", 24.0)
    ]


def test_extraccion_es_positiva():
    serie = [_v(0, 30.0, odo=0.0), _v(1, 30.0), _v(2, 22.0), _v(3, 22.0)]
    assert cargas_extracciones(serie) == [
        Evento("BUS1", T0 + timedelta(minutes=10), "extraccion", 8.0)
    ]


def test_bajo_el_umbral_o_en_marcha_no_hay_evento():
    chico = [_v(0, 30.0, odo=0.0), _v(1, 30.0), _v(2, 27.0), _v(3, 27.0)]
    assert cargas_extracciones(chico) == []
    marcha = [_v(k, 50.0 - 3 * k, odo=1000.0 * k) for k in range(6)]
    assert cargas_extracciones(marcha) == []
