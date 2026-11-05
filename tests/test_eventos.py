"""Detección de eventos (pura, sin PostgreSQL)."""

from datetime import UTC, datetime, timedelta

from elena.eventos import (
    HUECO_MAX_RALENTI,
    Evento,
    Muestra,
    Ventana,
    cargas_extracciones,
    encendidos,
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


def test_ralenti_que_sigue_al_final_es_en_curso():
    serie = _serie_ralenti(6)
    fin = serie[-1].recorded_at
    assert ralentis(serie, fin + timedelta(minutes=2)) == [
        Evento("BUS1", T0 + timedelta(minutes=1), "ralenti_en_curso", 6.0)
    ]
    # sin datos hace más de HUECO_MAX_RALENTI: ya terminó
    assert [e.evento for e in ralentis(serie, fin + HUECO_MAX_RALENTI * 2)] == ["ralenti"]
    # cerrado por una muestra en marcha: terminado aunque sea reciente
    serie.append(_m(9, speed=40.0))
    assert [e.evento for e in ralentis(serie, fin + timedelta(minutes=2))] == ["ralenti"]


def test_encendido_y_apagado_en_cada_cambio_de_ignicion():
    serie = [_m(0, ign=False), _m(1, ign=True), _m(2, ign=None), _m(3, ign=True), _m(4, ign=False)]
    assert encendidos(serie) == [
        Evento("BUS1", T0 + timedelta(minutes=1), "encendido", None),
        Evento("BUS1", T0 + timedelta(minutes=4), "apagado", None),
    ]
    # la primera muestra leída no es un cambio
    assert encendidos([_m(0, ign=True), _m(1, ign=True)]) == []


def _s(segundos, ign):
    return _m(segundos / 60, ign=ign)


def test_rebote_de_ignicion_al_apagar_da_un_solo_apagado():
    # unidad 2 el 11-05: encendido, 11:54:02 off, :04 on, :06 off y sigue apagado
    serie = [_s(-120, True), _s(29, True), _s(58, False), _s(60, True), _s(62, False), _s(182, False)]
    assert encendidos(serie) == [Evento("BUS1", T0 + timedelta(seconds=58), "apagado", None)]
    # apagado y encendido en segundos sin seguir apagado: nada
    serie = [_s(-120, True), _s(58, False), _s(60, True), _s(300, True)]
    assert encendidos(serie) == []


def test_cambio_reciente_espera_a_durar_el_rebote():
    serie = [_s(0, True), _s(100, False)]
    assert encendidos(serie, T0 + timedelta(seconds=130)) == []
    assert [e.evento for e in encendidos(serie, T0 + timedelta(seconds=200))] == ["apagado"]


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


def test_subida_de_golpe_en_marcha_es_carga():
    # el 10-30 del autobús 2: el nivel nuevo aparece ya a 418 m de la ventana anterior
    serie = [
        _v(0, 17.13, odo=0.0),
        _v(1, 16.85, odo=1649.0),
        _v(2, 38.56, odo=2067.0),
        _v(3, 38.05, odo=2412.0),
    ]
    assert cargas_extracciones(serie) == [
        Evento("BUS1", T0 + timedelta(minutes=10), "carga", 21.71)
    ]
    # una subida chica en marcha es ruido
    serie[2:] = [_v(2, 19.0, odo=2067.0), _v(3, 18.8, odo=2412.0)]
    assert cargas_extracciones(serie) == []
