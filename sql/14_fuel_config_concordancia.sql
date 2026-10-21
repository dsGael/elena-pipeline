-- 14_fuel_config_concordancia.sql
-- Parámetros de la "concordancia" del procesador (processor.py): una ventana que por sí sola no
-- alcanza (en movimiento, o parada con pocas muestras) puede BAJAR el nivel si las últimas
-- N lecturas coinciden entre sí una vez descontado el consumo por km.
-- NULL en cualquiera de las tres = regla desactivada (comportamiento anterior).
-- Se puede correr más de una vez. Correr ANTES de actualizar el código en el servidor:
-- el pipeline nuevo lee estas columnas.

ALTER TABLE public.fuel_config
    ADD COLUMN IF NOT EXISTS concordancia_ventanas    integer,        -- lecturas que deben coincidir (>= 2)
    ADD COLUMN IF NOT EXISTS concordancia_tolerancia  numeric(6, 2),  -- litros: rango máx - mín permitido
    ADD COLUMN IF NOT EXISTS concordancia_max_minutos integer;        -- antigüedad máxima de esas lecturas

COMMENT ON COLUMN public.fuel_config.concordancia_ventanas IS
    'Concordancia: lecturas recientes que deben coincidir (descontado el consumo). NULL = desactivada.';
COMMENT ON COLUMN public.fuel_config.concordancia_tolerancia IS
    'Concordancia: rango máximo (litros) entre esas lecturas. NULL = desactivada.';
COMMENT ON COLUMN public.fuel_config.concordancia_max_minutos IS
    'Concordancia: antigüedad máxima (minutos) de las lecturas usadas. NULL = desactivada.';

-- Valores sugeridos para el Yaris tras simular sobre su historial (29/09 a 21/10):
-- 4 lecturas, rango <= 1.0 L, dentro de 30 min. El CORBER no se simuló: déjalo en NULL.
-- UPDATE public.fuel_config
--    SET concordancia_ventanas = 4, concordancia_tolerancia = 1.0, concordancia_max_minutos = 30
--  WHERE idautobus = '1';
