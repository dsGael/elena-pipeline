"""Pruebas de reprocess.py con conexión simulada (la integración está al final)."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from elena import reprocess as rp
from elena.pipeline import ejecutar_pipeline
from tests.test_pipeline_pg import (  # noqa: F401  (fixture y helper reutilizados)
    _ventana,
    pg,
)

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("America/Hermosillo"))
DESDE = T0 + timedelta(minutes=30)


class Cur:
    def __init__(self, fila):
        self.fila = fila

    def fetchone(self):
        return self.fila


class ConexionFalsa:
    def __init__(self, *, lock=True, resultados=7, eventos=2, base=None):
        self.lock, self.n_res, self.n_ev, self.base = lock, resultados, eventos, base
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "pg_try_advisory_xact_lock" in s:
            self.sql.append("LOCK")
            return Cur((self.lock,))
        if s.startswith("SELECT count(*) FROM public.fuel_processed_result"):
            return Cur((self.n_res,))
        if s.startswith("SELECT count(*) FROM public.fuel_events"):
            return Cur((self.n_ev,))
        if "FROM public.fuel_processed_result r JOIN" in s:
            return Cur(self.base)
        for clave in (
            "DELETE FROM public.fuel_events",
            "DELETE FROM public.fuel_processed_result",
            "DELETE FROM public.fuel_estado_confiable",
            "INSERT INTO public.fuel_estado_confiable",
        ):
            if s.startswith(clave):
                self.sql.append(
                    clave.split(".")[1].replace("fuel_", "").upper()[:9]
                    + ":"
                    + s.split()[0]
                )
                return Cur(None)
        raise AssertionError(s)

    def commit(self):
        self.sql.append("COMMIT")

    @contextmanager
    def transaction(self):
        self.sql.append("BEGIN")
        yield
        self.sql.append("END")


BASE = ("350000000000001", 10.0, T0, 100_000)


def test_planificar_todo_el_autobus_no_busca_base():
    plan = rp.planificar(ConexionFalsa(), "YARIS1", None)
    assert (plan.resultados, plan.eventos, plan.base) == (7, 2, None)


def test_planificar_con_desde_restaura_el_ultimo_valor_actualizado():
    plan = rp.planificar(ConexionFalsa(base=BASE), "YARIS1", DESDE)
    assert plan.base.ultimo_litro_confiable == 10.0
    assert plan.base.odometro_ultimo_confiable == 100_000.0
    assert plan.base.fecha_ultimo_confiable == T0
    assert plan.base.pendiente_tipo is None  # el pendiente se reinicia
    assert plan.base_imei == "350000000000001"


def test_planificar_con_desde_sin_valor_previo_empieza_de_cero():
    assert rp.planificar(ConexionFalsa(base=None), "YARIS1", DESDE).base is None


def test_reiniciar_completo_borra_en_orden_dentro_de_una_transaccion():
    c = ConexionFalsa()
    plan = rp.reiniciar(c, "YARIS1", None)
    assert plan.resultados == 7
    assert c.sql[:3] == ["COMMIT", "BEGIN", "LOCK"]
    borrados = [x for x in c.sql if ":DELETE" in x]
    assert [b.split(":")[0] for b in borrados] == ["EVENTS", "PROCESSED", "ESTADO_CO"]
    assert c.sql[-1] == "END"


def test_reiniciar_con_base_guarda_el_estado_en_lugar_de_borrarlo():
    c = ConexionFalsa(base=BASE)
    rp.reiniciar(c, "YARIS1", DESDE)
    assert any(x.endswith(":INSERT") for x in c.sql)
    assert not any(x.startswith("ESTADO_CO:DELETE") for x in c.sql)


def test_reiniciar_con_autobus_ocupado_no_borra_nada():
    c = ConexionFalsa(lock=False)
    with pytest.raises(rp.AutobusOcupado):
        rp.reiniciar(c, "YARIS1", None)
    assert not any(":DELETE" in x for x in c.sql)


def test_el_sql_de_borrado_de_eventos_es_un_delete_con_el_mismo_filtro():
    assert rp.BORRAR_EVENTOS_SQL.lstrip().startswith("DELETE FROM public.fuel_events")
    assert "count(*)" not in rp.BORRAR_EVENTOS_SQL
    assert rp.BORRAR_EVENTOS_SQL.count("%(desde)s") == rp.CONTAR_EVENTOS_SQL.count(
        "%(desde)s"
    )


# ---- integración (requiere ELENA_TEST_DSN; no se ha podido ejecutar fuera de tu base) ----


def _snapshot(c):
    res = c.execute(
        "SELECT ventana_inicio, estado_resultado, litros_finales, actualizo_final, motivo "
        "FROM fuel_processed_result ORDER BY ventana_inicio"
    ).fetchall()
    ev = c.execute(
        "SELECT tipo_evento, estado, fecha_inicio, litros_evento FROM fuel_events "
        "ORDER BY fecha_inicio, tipo_evento"
    ).fetchall()
    est = c.execute(
        "SELECT ultimo_litro_confiable, pendiente_tipo FROM fuel_estado_confiable"
    ).fetchall()
    return res, ev, est


@pytest.mark.integration
@pytest.mark.parametrize("desde_ventana", [None, 2])
def test_reprocesar_da_el_mismo_resultado_que_la_primera_vez(pg, desde_ventana):  # noqa: F811
    for i, litros in enumerate([10, 10, 13, 13, 13, 13]):
        _ventana(pg, i, litros)
    pg.commit()
    ejecutar_pipeline(pg, idautobus="YARIS1")
    antes = _snapshot(pg)
    assert antes[1]  # hubo un evento de carga

    desde = None if desde_ventana is None else T0 + timedelta(minutes=5 * desde_ventana)
    rp.reiniciar(pg, "YARIS1", desde)
    ejecutar_pipeline(pg, idautobus="YARIS1")
    assert _snapshot(pg) == antes