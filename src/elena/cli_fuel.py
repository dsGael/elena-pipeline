"""Comandos de CLI del procesamiento de combustible.

Se registran en el CLI principal (cli.py) con dos líneas:

    from .cli_fuel import app as fuel_app
    app.add_typer(fuel_app)

Comandos:
    elena-fuel audit-schema
    elena-fuel consolidate [--desde ...] [--hasta ...] [--imei ...] [--dry-run] [--show N]
    elena-fuel estimar-rendimiento --idautobus X [--desde ...] [--hasta ...]
    elena-fuel process [--idautobus X] [--max-ventanas N]
    elena-fuel run-cycle [--log-dir logs]      (lo que ejecuta el Task Scheduler)
    elena-fuel reprocess --idautobus X [--desde ...] [--aplicar]
    elena-fuel events [--idautobus X] [--desde ...] [--estado ...] [--tipo ...]
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from .audit import run_audit
from .consolidation import (
    WINDOW_MINUTES,
    ConsolidationSummary,
    NoStartError,
    consolidate,
    plan_range,
)
from .db import get_connection
from .fuel_repo import (
    ConfigError,
    cargar_config,
    cargar_puntos_rendimiento,
    formatear_eventos,
    listar_eventos,
)
from .pipeline import ejecutar_pipeline
from .quality import QualityRules
from .rendimiento import estimar_rendimiento
from .reprocess import AutobusOcupado, planificar, reiniciar
from .scheduled_worker import configurar_logging, ejecutar_ciclo
from .settings import settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = logging.getLogger("elena.cli")


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.app_timezone)


def _parse_local(texto: str | None, option: str) -> datetime | None:
    """'2026-10-08 10:00' se interpreta en la zona local (settings.app_timezone)."""
    if texto is None:
        return None
    try:
        dt = datetime.fromisoformat(texto)
    except ValueError as exc:
        raise typer.BadParameter(
            f"{option}: usa el formato 'AAAA-MM-DD HH:MM'."
        ) from exc
    return dt.replace(tzinfo=_tz()) if dt.tzinfo is None else dt


def _fmt(ts: datetime) -> str:
    return ts.astimezone(_tz()).strftime("%Y-%m-%d %H:%M")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command("audit-schema")
def audit_schema() -> None:
    """Revisa (solo lectura) las tablas fuel_*, índices, configuración e IMEI mapeados."""
    with get_connection() as conn:
        report = run_audit(conn)
    typer.echo(report.render())
    raise typer.Exit(code=0 if report.ok else 1)


def _print_summary(s: ConsolidationSummary, show: int) -> None:
    typer.echo(
        f"Rango:        {_fmt(s.desde)} -> {_fmt(s.hasta)} ({settings.app_timezone}, fin exclusivo)"
    )
    typer.echo(f"Dispositivos: {len(s.imeis)}")
    typer.echo(
        f"Ventanas:     {s.ventanas}  (nuevas {s.insertadas}, actualizadas {s.actualizadas})"
    )
    if s.por_calidad:
        typer.echo(
            "Calidad:      "
            + "  ".join(f"{k}={v}" for k, v in sorted(s.por_calidad.items()))
        )
    if s.por_motivo:
        typer.echo(
            "Motivos:      "
            + "  ".join(f"{k}={v}" for k, v in s.por_motivo.most_common())
        )
    for a in s.advertencias:
        typer.echo(f"ADVERTENCIA:  {a}")
    if s.muestra and show > 0:
        typer.echo("")
        typer.echo(
            f"{'inicio (local)':<17} {'imei':<16} {'n/val':>7} {'sensor':>8} {'ang.corr':>9} {'%ang':>6} {'ign':>4}  calidad"
        )
        for w in s.muestra[:show]:
            ang = "-" if w.angulo_corregido is None else f"{w.angulo_corregido:.2f}"
            pct = (
                "-"
                if w.porcentaje_angulo_valido is None
                else f"{w.porcentaje_angulo_valido:.0f}"
            )
            sensor = "-" if w.sensor_mediana is None else f"{w.sensor_mediana:.0f}"
            ign = {True: "si", False: "no", None: "-"}[w.ignicion_dominante]
            motivo = f" ({w.motivo_calidad})" if w.motivo_calidad else ""
            typer.echo(
                f"{_fmt(w.ventana_inicio):<17} {w.imei:<16} "
                f"{w.muestras_totales:>3}/{w.muestras_validas:<3} {sensor:>8} {ang:>9} {pct:>6} {ign:>4}  "
                f"{w.calidad_ventana}{motivo}"
            )


@app.command("consolidate")
def consolidate_cmd(
    desde: Annotated[
        str | None,
        typer.Option(
            help="Inicio local 'AAAA-MM-DD HH:MM'. Sin él, retoma desde la última ventana guardada."
        ),
    ] = None,
    hasta: Annotated[
        str | None,
        typer.Option(help="Fin local exclusivo. Sin él, la última ventana ya cerrada."),
    ] = None,
    imei: Annotated[str | None, typer.Option(help="Procesa solo este IMEI.")] = None,
    lag: Annotated[
        int, typer.Option(help="Colchón en minutos para datos tardíos.")
    ] = 2,
    overlap: Annotated[
        int, typer.Option(help="Minutos que se reprocesan hacia atrás al retomar.")
    ] = 60,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Calcula y muestra, sin escribir en fuel_windows."
        ),
    ] = False,
    show: Annotated[
        int, typer.Option(help="Muestra las primeras N ventanas calculadas.")
    ] = 0,
    max_rango_sensor: Annotated[
        float | None,
        typer.Option(help="Regla opcional: máx. (max-min) del sensor en la ventana."),
    ] = None,
    porcentaje_angulo_min: Annotated[
        float | None,
        typer.Option(help="Regla opcional: % mínimo de muestras con ángulo válido."),
    ] = None,
    ignicion_apagada_invalida: Annotated[
        bool,
        typer.Option(
            "--ignicion-apagada-invalida",
            help="Marca dudosas las ventanas con ignición apagada.",
        ),
    ] = False,
    descartar_sin_ignicion: Annotated[
        bool | None,
        typer.Option(
            "--descartar-sin-ignicion/--conservar-sin-ignicion",
            help="Fuerza descartar (o conservar) las lecturas con el motor apagado en todos "
            "los autobuses. Sin la opción, cada autobús usa fuel_config.descartar_sin_ignicion.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Agrupa gps_records en ventanas de 5 min y las guarda (o simula) en fuel_windows."""
    _setup_logging(verbose)
    reglas = QualityRules(
        fcc_inclinacion=0.0,  # el FCC real viene de fuel_config por vehículo
        max_rango_sensor=max_rango_sensor,
        porcentaje_angulo_min=porcentaje_angulo_min,
        ignicion_apagada_invalida=ignicion_apagada_invalida,
    )
    now = datetime.now(UTC)
    with get_connection() as conn:
        try:
            d, h = plan_range(
                conn,
                now=now,
                desde=_parse_local(desde, "--desde"),
                hasta=_parse_local(hasta, "--hasta"),
                lag_minutes=lag,
                overlap_minutes=overlap,
                imei=imei,
            )
        except NoStartError as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        if d >= h:
            typer.echo(f"Nada que consolidar: {_fmt(d)} >= {_fmt(h)}.")
            return
        summary = consolidate(
            conn,
            d,
            h,
            imei=imei,
            minutes=WINDOW_MINUTES,
            base_rules=reglas,
            dry_run=dry_run,
            keep_sample=show,
            descartar_sin_ignicion=descartar_sin_ignicion,
        )
        # el bloque 'with' confirma la transacción al salir sin errores; con --dry-run no hay nada que confirmar
    _print_summary(summary, show)
    if dry_run:
        typer.echo("\n(dry-run: no se escribió nada)")


@app.command("estimar-rendimiento")
def estimar_rendimiento_cmd(
    idautobus: Annotated[str, typer.Option(help="Autobús (idautobus) a analizar.")],
    desde: Annotated[
        str | None,
        typer.Option(
            help="Inicio local 'AAAA-MM-DD HH:MM'. Por defecto, 14 días atrás."
        ),
    ] = None,
    hasta: Annotated[
        str | None, typer.Option(help="Fin local exclusivo. Por defecto, ahora.")
    ] = None,
    max_hueco: Annotated[
        float, typer.Option(help="Minutos máximos entre ventanas para usar el par.")
    ] = 15.0,
    min_litros: Annotated[
        float, typer.Option(help="Consumo acumulado mínimo para dar una estimación.")
    ] = 10.0,
    odometro_a_km: Annotated[
        float,
        typer.Option(help="Factor odometro_total -> km (0.001 si está en metros)."),
    ] = 0.001,
) -> None:
    """Estima el rendimiento (km/L) con ventanas válidas ya consolidadas. Solo lectura."""
    ahora = datetime.now(UTC)
    d = _parse_local(desde, "--desde") or ahora - timedelta(days=14)
    h = _parse_local(hasta, "--hasta") or ahora
    with get_connection() as conn:
        try:
            cfg = cargar_config(conn, idautobus)
        except ConfigError as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        puntos = cargar_puntos_rendimiento(conn, idautobus, d, h, cfg.calibracion)
    est = estimar_rendimiento(
        puntos,
        odometro_a_km=odometro_a_km,
        salto_litros=cfg.umbral_carga_candidata,
        max_hueco_minutos=max_hueco,
        min_litros=min_litros,
    )
    typer.echo(f"Autobús:      {idautobus}")
    typer.echo(f"Rango:        {_fmt(d)} -> {_fmt(h)} ({settings.app_timezone})")
    typer.echo(f"Ventanas:     {est.puntos} válidas con litros calculables")
    typer.echo(
        f"Tramos:       {est.tramos_usados} usados ({est.puntos_usados} ventanas)"
    )
    if est.descartes:
        typer.echo(
            "Descartes:    "
            + "  ".join(f"{k}={v}" for k, v in est.descartes.most_common())
        )
    typer.echo(f"Recorrido:    {est.km:.2f} km (solo tramos usados)")
    typer.echo(f"Consumo:      {est.litros:.2f} L (estimado por regresión)")
    if est.rendimiento_km_l is None:
        typer.echo(
            f"\nSin estimación: se necesitan al menos {min_litros:g} L de consumo "
            "acumulado en tramos limpios (con al menos 10 km cada uno). "
            "Amplía el rango o espera más datos."
        )
        raise typer.Exit(code=1)
    typer.echo(f"Rendimiento:  {est.rendimiento_km_l:.2f} km/L")
    typer.echo(
        "\nPara usarlo (no se aplica automáticamente):\n"
        f"  UPDATE fuel_config SET rendimiento_default = {est.rendimiento_km_l:.2f}\n"
        f"   WHERE idautobus = '{idautobus}';"
    )


@app.command("process")
def process_cmd(
    idautobus: Annotated[
        str | None, typer.Option(help="Procesa solo este autobús (por defecto, todos).")
    ] = None,
    max_ventanas: Annotated[
        int | None,
        typer.Option(help="Máximo de ventanas por autobús en esta corrida."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Procesa las ventanas nuevas: resultados, eventos y último valor confiable."""
    _setup_logging(verbose)
    with get_connection() as conn:
        resumen = ejecutar_pipeline(
            conn, idautobus=idautobus, max_ventanas=max_ventanas
        )
    if not resumen.autobuses:
        typer.echo("No hay autobuses activos en fuel_config.")
        return
    for a in resumen.autobuses:
        estados = "  ".join(f"{k}={v}" for k, v in sorted(a.por_estado.items()))
        linea = f"{a.idautobus:<12} {a.resultado:<8} ventanas={a.procesadas}"
        if a.eventos:
            linea += f" eventos={a.eventos}"
        if a.omitidas_tardias:
            linea += f" tardías_omitidas={a.omitidas_tardias}"
        typer.echo(linea + (f"  [{estados}]" if estados else ""))
        if a.error:
            typer.echo(f"  ERROR: {a.error}", err=True)
    raise typer.Exit(code=0 if resumen.ok else 1)


@app.command("run-cycle")
def run_cycle_cmd(
    log_dir: Annotated[
        str, typer.Option(help="Carpeta del log rotativo (vacío = solo consola).")
    ] = "logs",
    max_ventanas: Annotated[
        int | None,
        typer.Option(help="Máximo de ventanas por autobús en este ciclo."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Un ciclo completo (consolidar + procesar). Pensado para el Task Scheduler."""
    from pathlib import Path

    configurar_logging(Path(log_dir) if log_dir else None, verbose)
    with get_connection() as conn:
        resultado = ejecutar_ciclo(conn, max_ventanas=max_ventanas)
    raise typer.Exit(code=resultado.codigo)


@app.command("reprocess")
def reprocess_cmd(
    idautobus: Annotated[str, typer.Option(help="Autobús a reprocesar.")],
    desde: Annotated[
        str | None,
        typer.Option(
            help="Inicio local 'AAAA-MM-DD HH:MM'. Sin él, se recalcula todo el autobús."
        ),
    ] = None,
    aplicar: Annotated[
        bool,
        typer.Option(
            "--aplicar",
            help="Sin esta opción solo muestra lo que se borraría (no cambia nada).",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Borra los resultados/eventos derivados de un autobús y los recalcula."""
    _setup_logging(verbose)
    d = _parse_local(desde, "--desde")
    with get_connection() as conn:
        plan = planificar(conn, idautobus, d)
        conn.commit()
        alcance = "todo el autobús" if d is None else f"desde {_fmt(d)}"
        typer.echo(f"Autobús:      {idautobus} ({alcance})")
        typer.echo(
            f"Se borrarían: {plan.resultados} resultados, {plan.eventos} eventos"
        )
        if d is not None:
            if plan.base is None:
                typer.echo(
                    "Estado base:  ninguno antes de esa fecha (se empieza de cero)"
                )
            else:
                typer.echo(
                    f"Estado base:  {plan.base.ultimo_litro_confiable:.2f} L "
                    f"({_fmt(plan.base.fecha_ultimo_confiable)})"
                )
        if not aplicar:
            typer.echo("\n(simulación: agrega --aplicar para ejecutar)")
            return
        try:
            reiniciar(conn, idautobus, d)
        except AutobusOcupado as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        resumen = ejecutar_pipeline(conn, idautobus=idautobus)
    for a in resumen.autobuses:
        estados = "  ".join(f"{k}={v}" for k, v in sorted(a.por_estado.items()))
        typer.echo(
            f"{a.idautobus:<12} {a.resultado:<8} ventanas={a.procesadas} eventos={a.eventos}"
            + (f"  [{estados}]" if estados else "")
        )
        if a.error:
            typer.echo(f"  ERROR: {a.error}", err=True)
    raise typer.Exit(code=0 if resumen.ok else 1)


@app.command("events")
def events_cmd(
    idautobus: Annotated[str | None, typer.Option(help="Solo este autobús.")] = None,
    desde: Annotated[
        str | None,
        typer.Option(
            help="Inicio local 'AAAA-MM-DD HH:MM'. Por defecto, 7 días atrás."
        ),
    ] = None,
    hasta: Annotated[str | None, typer.Option(help="Fin local exclusivo.")] = None,
    estado: Annotated[
        str | None, typer.Option(help="abierto | confirmado | descartado")
    ] = None,
    tipo: Annotated[
        str | None,
        typer.Option(help="Por ejemplo carga_confirmada, posible_extraccion."),
    ] = None,
    limite: Annotated[int, typer.Option(help="Máximo de eventos a mostrar.")] = 50,
) -> None:
    """Lista los eventos detectados (cargas, extracciones, pendientes). Solo lectura."""
    d = _parse_local(desde, "--desde") or datetime.now(UTC) - timedelta(days=7)
    with get_connection() as conn:
        filas = listar_eventos(
            conn,
            idautobus=idautobus,
            desde=d,
            hasta=_parse_local(hasta, "--hasta"),
            estado=estado,
            tipo=tipo,
            limite=limite,
        )
    for linea in formatear_eventos(filas, _tz()):
        typer.echo(linea)
