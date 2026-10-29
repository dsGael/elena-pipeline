"""Pruebas del ciclo con conexión y pasos simulados (sin PostgreSQL)."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from elena import scheduled_worker as sw
from elena.consolidation import ConsolidationSummary, NoStartError

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
    """Reemplaza plan_range y consolidate; registra el orden."""
    estado = {"orden": []}

    def plan(conn, **kw):
        estado["orden"].append("plan")
        return T0, T1

    def consolidar(conn, desde, hasta, **kw):
        estado["orden"].append("consolidar")
        return ConsolidationSummary(desde=desde, hasta=hasta, ventanas=12, insertadas=1)

    monkeypatch.setattr(sw, "plan_range", plan)
    monkeypatch.setattr(sw, "consolidate", consolidar)
    monkeypatch.setattr(
        sw, "actualizar_eventos", lambda conn, d, h: estado["orden"].append("eventos")
    )
    # el rendimiento reciente se prueba aparte; aquí no toca la conexión falsa
    monkeypatch.setattr(sw, "actualizar_rendimiento_reciente", lambda conn, **kw: 0)
    monkeypatch.setattr(sw, "actualizar_historial_rendimiento", lambda conn, **kw: 0)
    return estado


def test_ciclo_ok_orden_y_commit_tras_consolidar(pasos):
    conn = ConexionFalsa()
    r = sw.ejecutar_ciclo(conn)
    assert r.codigo == sw.EXIT_OK and not r.omitido
    assert pasos["orden"] == ["plan", "consolidar", "eventos"]
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


def test_excepcion_inesperada_da_codigo_1_y_siempre_libera_el_lock(monkeypatch, pasos):
    def consolidar(conn, desde, hasta, **kw):
        raise RuntimeError("se cayó la red")

    monkeypatch.setattr(sw, "consolidate", consolidar)
    conn = ConexionFalsa()
    r = sw.ejecutar_ciclo(conn)
    assert r.codigo == sw.EXIT_ERROR
    assert conn.pasos[-1] == "unlock" and "rollback" in conn.eventos


def test_rango_vacio_no_consolida(monkeypatch, pasos):
    monkeypatch.setattr(sw, "plan_range", lambda conn, **kw: (T1, T1))
    r = sw.ejecutar_ciclo(ConexionFalsa())
    assert pasos["orden"] == [] and r.consolidacion is None and r.codigo == sw.EXIT_OK


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


def test_el_ciclo_actualiza_el_rendimiento_despues_de_consolidar(monkeypatch, pasos):
    def rendimiento(conn, **kw):
        pasos["orden"].append("rendimiento")
        pasos["ahora"] = kw["ahora"]
        return 1

    def historial(conn, **kw):
        pasos["orden"].append("historial")
        return 1

    monkeypatch.setattr(sw, "actualizar_rendimiento_reciente", rendimiento)
    monkeypatch.setattr(sw, "actualizar_historial_rendimiento", historial)
    r = sw.ejecutar_ciclo(ConexionFalsa(), now=T1)
    assert r.codigo == sw.EXIT_OK
    assert pasos["orden"] == [
        "plan",
        "consolidar",
        "eventos",
        "rendimiento",
        "historial",
    ]
    assert pasos["ahora"] == T1


class ConexionRendimiento:
    """Conexión falsa para ``actualizar_rendimiento_reciente``."""

    def __init__(self):
        self.eventos = []

    def commit(self):
        self.eventos.append("commit")

    def transaction(self):
        from contextlib import nullcontext

        return nullcontext()


def test_rendimiento_reciente_un_autobus_con_error_no_detiene_a_los_demas(monkeypatch):
    from elena.rendimiento import RendimientoReciente

    guardados = []
    monkeypatch.setattr(sw, "autobuses_activos", lambda conn: ["1", "2", "3"])

    def calcular(conn, bus, **kw):
        if bus == "2":
            raise RuntimeError("boom")
        return RendimientoReciente(10.0, 2.0, 90.0, 9.0, 40, 2, T0, T1, None)

    monkeypatch.setattr(sw, "rendimiento_reciente", calcular)
    monkeypatch.setattr(
        sw,
        "guardar_rendimiento_reciente",
        lambda conn, bus, est, km: guardados.append((bus, km)),
    )
    n = sw.actualizar_rendimiento_reciente(
        ConexionRendimiento(), ahora=T1, km_ventana=80
    )
    assert n == 2 and guardados == [("1", 80), ("3", 80)]


def test_rendimiento_reciente_nunca_lanza_aunque_falle_el_listado(monkeypatch):
    def falla(conn):
        raise RuntimeError("sin base")

    monkeypatch.setattr(sw, "autobuses_activos", falla)
    assert sw.actualizar_rendimiento_reciente(ConexionRendimiento(), ahora=T1) == 0


def test_historial_un_autobus_con_error_no_detiene_a_los_demas(monkeypatch):
    guardados = []
    monkeypatch.setattr(sw, "autobuses_activos", lambda conn: ["1", "2", "3"])

    def nuevos(conn, bus, **kw):
        if bus == "2":
            raise RuntimeError("boom")
        return [f"bloque-{bus}-a", f"bloque-{bus}-b"]

    monkeypatch.setattr(sw, "bloques_rendimiento_nuevos", nuevos)
    monkeypatch.setattr(
        sw,
        "guardar_bloques",
        lambda conn, bus, bloques, km: guardados.append((bus, km)) or len(bloques),
    )
    n = sw.actualizar_historial_rendimiento(
        ConexionRendimiento(), ahora=T1, km_bloque=40
    )
    assert n == 4 and guardados == [("1", 40), ("3", 40)]


def test_historial_nunca_lanza_aunque_falle_el_listado(monkeypatch):
    def falla(conn):
        raise RuntimeError("sin base")

    monkeypatch.setattr(sw, "autobuses_activos", falla)
    assert sw.actualizar_historial_rendimiento(ConexionRendimiento(), ahora=T1) == 0


def test_eventos_nunca_lanza(monkeypatch):
    def falla(conn, desde, hasta):
        raise RuntimeError("fallo en eventos")

    monkeypatch.setattr(sw, "detectar_eventos", falla)
    sw.actualizar_eventos(ConexionRendimiento(), T0, T1)
