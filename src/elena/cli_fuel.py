"""Comandos de CLI del procesamiento de combustible.

Se registran en el CLI principal (cli.py) con dos líneas:

    from .cli_fuel import app as fuel_app
    app.add_typer(fuel_app)

Comandos:
    elena-fuel audit-schema
    elena-fuel consolidate [--desde ...] [--hasta ...] [--imei ...] [--dry-run] [--show N]
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
from .quality import QualityRules
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
    ] = 51,
    ignicion_apagada_invalida: Annotated[
        bool,
        typer.Option(
            "--ignicion-apagada-invalida",
            help="Marca dudosas las ventanas con ignición apagada.",
        ),
    ] = True,
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
        )
        # el bloque 'with' confirma la transacción al salir sin errores; con --dry-run no hay nada que confirmar
    _print_summary(summary, show)
    if dry_run:
        typer.echo("\n(dry-run: no se escribió nada)")
