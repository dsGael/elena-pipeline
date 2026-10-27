from dataclasses import replace

import pytest

from elena.quality import (
    M_ANGULO,
    M_ANGULO_INESTABLE,
    M_MUESTRAS,
    M_SENSOR_INESTABLE,
    M_SIN_INCLINACION,
    M_SIN_SENSOR,
    QualityRules,
    WindowQuality,
    WindowStats,
    evaluate_window,
)

REGLAS = QualityRules(fcc_inclinacion=2.6, angulo_max=5.0)


def stats(**kw) -> WindowStats:
    base = WindowStats(
        muestras_totales=15,
        muestras_validas=15,
        sensor_mediana=1374.0,
        sensor_min=1368.0,
        sensor_max=1380.0,
        inclinacion_mediana=2.9,
        muestras_con_angulo=15,
        muestras_angulo_valido=15,
    )
    return replace(base, **kw)


def test_ventana_valida():
    r = evaluate_window(stats(), REGLAS)
    assert r.calidad is WindowQuality.VALIDA
    assert r.motivo is None
    assert r.angulo_corregido == pytest.approx(0.3)
    assert r.porcentaje_angulo_valido == 100.0


@pytest.mark.parametrize(
    "incl, valida",
    [
        (7.6, True),  # x = +5.00 -> en el límite, válida
        (7.61, False),  # +5.01
        (-2.4, True),  # -5.00
        (-2.41, False),  # -5.01
        (7.604, True),  # se decide con el valor redondeado que se guarda (5.00)
    ],
)
def test_limite_del_angulo(incl, valida):
    r = evaluate_window(stats(inclinacion_mediana=incl), REGLAS)
    assert (r.calidad is WindowQuality.VALIDA) is valida
    if not valida:
        assert r.calidad is WindowQuality.DUDOSA
        assert r.motivos == (M_ANGULO,)


def test_angulo_fuera_conserva_el_valor_para_diagnostico():
    r = evaluate_window(
        stats(inclinacion_mediana=12.6, muestras_angulo_valido=2), REGLAS
    )
    assert r.calidad is WindowQuality.DUDOSA
    assert r.angulo_corregido == pytest.approx(10.0)
    assert r.porcentaje_angulo_valido == pytest.approx(13.33)


def test_sin_sensor_descarta():
    r = evaluate_window(
        stats(
            muestras_validas=0, sensor_mediana=None, sensor_min=None, sensor_max=None
        ),
        REGLAS,
    )
    assert r.calidad is WindowQuality.DESCARTADA
    assert M_SIN_SENSOR in r.motivos


def test_descartada_pesa_mas_que_dudosa_y_lista_todos_los_motivos():
    r = evaluate_window(
        stats(muestras_validas=0, sensor_mediana=None, inclinacion_mediana=20.0), REGLAS
    )
    assert r.calidad is WindowQuality.DESCARTADA
    assert r.motivos == (M_SIN_SENSOR, M_ANGULO)
    assert r.motivo == f"{M_SIN_SENSOR}; {M_ANGULO}"


def test_sin_inclinacion_es_dudosa():
    r = evaluate_window(
        stats(
            inclinacion_mediana=None, muestras_con_angulo=0, muestras_angulo_valido=0
        ),
        REGLAS,
    )
    assert r.calidad is WindowQuality.DUDOSA
    assert r.motivos == (M_SIN_INCLINACION,)
    assert r.angulo_corregido is None and r.porcentaje_angulo_valido is None


def test_min_muestras_solo_aplica_si_esta_configurado():
    pocas = stats(muestras_totales=6, muestras_validas=6)
    assert evaluate_window(pocas, REGLAS).calidad is WindowQuality.VALIDA
    r = evaluate_window(pocas, replace(REGLAS, min_muestras=10))
    assert r.calidad is WindowQuality.DUDOSA and r.motivos == (M_MUESTRAS,)
    assert (
        evaluate_window(
            stats(muestras_validas=10), replace(REGLAS, min_muestras=10)
        ).calidad
        is WindowQuality.VALIDA
    )


def test_estabilidad_del_sensor():
    ruido = stats(sensor_min=1250.0, sensor_max=1550.0)
    assert (
        evaluate_window(ruido, REGLAS).calidad is WindowQuality.VALIDA
    )  # regla desactivada
    r = evaluate_window(ruido, replace(REGLAS, max_rango_sensor=100.0))
    assert r.calidad is WindowQuality.DUDOSA and r.motivos == (M_SENSOR_INESTABLE,)


def test_porcentaje_de_angulo_minimo():
    s = stats(muestras_angulo_valido=9)  # 60 %
    assert evaluate_window(s, REGLAS).calidad is WindowQuality.VALIDA
    r = evaluate_window(s, replace(REGLAS, porcentaje_angulo_min=80.0))
    assert r.calidad is WindowQuality.DUDOSA and r.motivos == (M_ANGULO_INESTABLE,)
