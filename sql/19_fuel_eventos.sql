-- 19_fuel_eventos.sql
-- Eventos por autobús: carga, extracción (de litros_grafica), aceleración, frenado, giro brusco
-- (AVL 253 del Teltonika en gps_records.maniobra) y ralentí (encendido y quieto al menos
-- fuel_config.minutos_ralenti).
-- OJO: borra fuel_events y la recrea (los datos anteriores no se conservan). Correr más de una vez
-- vuelve a vaciarla; los eventos se recalculan con elena-fuel consolidate --desde ...
-- Correr ANTES de actualizar el código en el servidor.

-- Sin CHECK: un valor inesperado del equipo no debe tirar la ingesta; el código solo usa 1-3.
ALTER TABLE public.gps_records
    ADD COLUMN IF NOT EXISTS maniobra smallint;

ALTER TABLE public.fuel_windows
    ADD COLUMN IF NOT EXISTS maniobra smallint[];

ALTER TABLE public.fuel_config
    ADD COLUMN IF NOT EXISTS minutos_ralenti integer NOT NULL DEFAULT 5
        CHECK (minutos_ralenti > 0);

-- Sin CASCADE: si una vista depende de fuel_events, falla en lugar de borrarla.
DROP TABLE IF EXISTS public.fuel_events;

CREATE TABLE public.fuel_events (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idautobus  varchar     NOT NULL,
    fecha      timestamptz NOT NULL,
    evento     varchar(20) NOT NULL CHECK (evento IN
               ('carga', 'extraccion', 'aceleracion', 'frenado', 'ralenti', 'giro_brusco')),
    cantidad   numeric(8, 2),
    ubicacion  text,
    creado_en  timestamptz NOT NULL DEFAULT now(),
    -- idempotencia del upsert y consultas por autobús y rango de fechas
    UNIQUE (idautobus, fecha, evento)
);

COMMENT ON COLUMN public.gps_records.maniobra IS
    'AVL 253: 1 aceleración, 2 frenado, 3 giro brusco. NULL = sin maniobra en este registro.';
COMMENT ON COLUMN public.fuel_windows.maniobra IS
    'Maniobras distintas (1-3) que hubo en la ventana. NULL = ninguna.';
COMMENT ON COLUMN public.fuel_config.minutos_ralenti IS
    'Minutos encendido y sin moverse a partir de los cuales se registra un evento de ralentí.';
COMMENT ON TABLE public.fuel_events IS
    'Eventos por autobús. Los calcula elena-fuel al consolidar.';
COMMENT ON COLUMN public.fuel_events.fecha IS 'Inicio del evento.';
COMMENT ON COLUMN public.fuel_events.cantidad IS
    'Litros cargados o extraídos (siempre positivos), minutos (ralentí). NULL en maniobras.';
COMMENT ON COLUMN public.fuel_events.ubicacion IS
    '''lat,lon'' (5 decimales) de la primera muestra GPS desde la fecha del evento.';
