-- 16_fuel_config_concordancia_carga.sql
-- Dos parámetros nuevos del procesador (processor.py):
--  * concordancia_carga: la concordancia (ver sql/14) también puede CONFIRMAR una carga, solo con
--    lecturas de ventanas válidas y subida >= umbral_carga_confirmada. false = solo baja el nivel.
--  * pendiente_max_minutos: un pendiente (carga/extracción sin confirmar) con más de estos
--    minutos se descarta. NULL = sin límite (comportamiento anterior).
-- Se puede correr más de una vez. Correr ANTES de actualizar el código en el servidor.

ALTER TABLE public.fuel_config
    ADD COLUMN IF NOT EXISTS concordancia_carga    boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pendiente_max_minutos integer;

COMMENT ON COLUMN public.fuel_config.concordancia_carga IS
    'Concordancia: puede confirmar cargas con lecturas válidas. false = solo baja el nivel.';
COMMENT ON COLUMN public.fuel_config.pendiente_max_minutos IS
    'Minutos máximos de un pendiente sin confirmar antes de descartarlo. NULL = sin límite.';

-- Sugerido para el Yaris (ajustar tras simular):
-- UPDATE public.fuel_config
--    SET concordancia_carga = true, pendiente_max_minutos = 60
--  WHERE idautobus = '1';
