"""Pruebas del ciclo con conexión y pasos simulados (sin PostgreSQL)."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from elena import scheduled_worker as sw
from elena.consolidation import ConsolidationSummary, NoStartError
from elena.pipeline import ResumenAutobus, ResumenPipeline

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("America/Hermosillo"))
T1 = datetime(2026, 10, 8, 11, 0, tzinfo=ZoneInfo("America/Hermosillo"))


class Cur:
    def __init__(self, fila):
        self.fila = fila

    def fetchone(self):
        return self.fila


class ConexionFalsa:
    def __init__(self, lock_libre=True):
        self.lock_libre = lock_libre
        self.eventos: list[str] = []

    def execute(self, sql, params=None):
        if "pg_try_advisory_lock" in sql:
            self.eventos.append("lock" if self.lock_libre else "lock_ocupado")
            return Cur((self.lock_libre,))
        if "pg_advisory_unlock" in sql:
            self.eventos.append("unlock")
            return Cur((True,))
        raise AssertionError(sql)

    def commit(self):
        self.eventos.append("commit")

    def rollback(self):
        self.eventos.append("rollback")

    @property
    def pasos(self) -> list[str]:
        """Eventos sin los commit, para comprobar el orden lock -> ... -> unlock."""
        return [e for e in self.eventos if e != "commit"]


@pytest.fixture
def pasos(monkeypatch):
    """Reemplaza plan_range, consolidate y ejecutar_pipeline; registra el orden."""
    estado = {"orden": [], "pipeline": ResumenPipeline([ResumenAutobus("YARIS1")])}

    def plan(conn, **kw):
        estado["orden"].append("plan")
        return T0, T1

    def consolidar(conn, desde, hasta, **kw):
        estado["orden"].append("consolidar")
        return ConsolidationSummary(desde=desde, hasta=hasta, ventanas=12, insertadas=1)

    def pipeline(conn, **kw):
        estado["orden"].append("pipeline")
        return estado["pipeline"]

    monkeypatch.setattr(sw, "plan_range", plan)
    monkeypatch.setattr(sw, "consolidate", consolidar)
    monkeypatch.setattr(sw, "ejecutar_pipeline", pipeline)
    return estado


def test_ciclo_ok_orden_y_commit_entre_consolidar_y_procesar(pasos):
    conn = ConexionFalsa()
    r = sw.ejecutar_ciclo(conn)
    assert r.codigo == sw.EXIT_OK and not r.omitido
    assert pasos["orden"] == ["plan", "consolidar", "pipeline"]
    # lock -> commit(lock) -> commit tras consolidar -> unlock
    assert conn.pasos[0] == "lock" and conn.pasos[-1] == "unlock"
    assert conn.eventos.count("commit") >= 3
    assert r.consolidacion.ventanas == 12


def test_si_otro_ciclo_corre_se_omite_sin_tocar_nada(pasos):
    conn = ConexionFalsa(lock_libre=False)
    r = sw.ejecutar_ciclo(conn)
    assert r.omitido and r.codigo == sw.EXIT_OK
    assert pasos["orden"] == [] and "unlock" not in conn.eventos


def test_sin_punto_de_partida_da_codigo_2_y_libera_el_lock(monkeypatch, pasos):
    def plan(conn, **kw):
        raise NoStartError("fuel_windows está vacía: indica --desde.")

    monkeypatch.setattr(sw, "plan_range", plan)
    conn = ConexionFalsa()
    r = sw.ejecutar_ciclo(conn)
    assert r.codigo == sw.EXIT_SIN_INICIO and "consolidate --desde" in r.mensajes[0]
    assert pasos["orden"] == [] and conn.pasos[-1] == "unlock"


def test_error_en_un_autobus_da_codigo_1(pasos):
    pasos["pipeline"] = ResumenPipeline(
        [ResumenAutobus("A"), ResumenAutobus("B", resultado="error", error="Boom")]
    )
    r = sw.ejecutar_ciclo(ConexionFalsa())
    assert r.codigo == sw.EXIT_ERROR


def test_excepcion_inesperada_da_codigo_1_y_siempre_libera_el_lock(monkeypatch, pasos):
    def consolidar(conn, desde, hasta, **kw):
        raise RuntimeError("se cayó la red")

    monkeypatch.setattr(sw, "consolidate", consolidar)
    conn = ConexionFalsa()
    r = sw.ejecutar_ciclo(conn)
    assert r.codigo == sw.EXIT_ERROR
    assert "pipeline" not in pasos["orden"]
    assert conn.pasos[-1] == "unlock" and "rollback" in conn.eventos


def test_rango_vacio_no_consolida_pero_si_procesa(monkeypatch, pasos):
    monkeypatch.setattr(sw, "plan_range", lambda conn, **kw: (T1, T1))
    r = sw.ejecutar_ciclo(ConexionFalsa())
    assert pasos["orden"] == ["pipeline"] and r.consolidacion is None


def test_configurar_logging_crea_archivo_rotativo(tmp_path):
    sw.configurar_logging(tmp_path / "logs")
    logging.getLogger("elena.worker").info("hola desde la prueba")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hola desde la prueba" in (tmp_path / "logs" / "elena_fuel.log").read_text(
        encoding="utf-8"
    )
    # llamarlo otra vez no duplica manejadores
    sw.configurar_logging(tmp_path / "logs")
    assert len(logging.getLogger().handlers) == 2
    sw.configurar_logging(None)  # deja el estado limpio para otras pruebas


def test_el_ciclo_deja_que_cada_autobus_decida_el_descarte_por_ignicion(
    monkeypatch, pasos
):
    recibido = {}

    def consolidar(conn, desde, hasta, **kw):
        recibido.update(kw)
        return ConsolidationSummary(desde=desde, hasta=hasta)

    monkeypatch.setattr(sw, "consolidate", consolidar)
    sw.ejecutar_ciclo(ConexionFalsa())
    assert (
        recibido["descartar_sin_ignicion"] is None
    )  # lo decide fuel_config por autobús
