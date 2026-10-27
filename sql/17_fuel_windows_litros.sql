-- 17_fuel_windows_litros.sql
-- Litros de cada ventana, interpolados con fuel_config.calibracion durante la consolidación.
-- Son litros crudos (con el ruido del sensor). estado_calibracion:
--   ok | zona_baja (<= primer punto) | arriba_de_calibracion (>= último punto) | sin_lectura
-- En zona_baja y arriba_de_calibracion los litros son una cota, no una medición.
-- Se puede correr más de una vez. Correr ANTES de actualizar el código en el servidor.
-- Las ventanas existentes quedan en NULL hasta re-consolidarlas (elena-fuel consolidate --desde ...).

ALTER TABLE public.fuel_windows
    ADD COLUMN IF NOT EXISTS litros             numeric(8, 2),
    ADD COLUMN IF NOT EXISTS estado_calibracion varchar(30);

COMMENT ON COLUMN public.fuel_windows.litros IS
    'Litros interpolados de sensor_mediana con la calibración del autobús (crudos, con ruido).';
COMMENT ON COLUMN public.fuel_windows.estado_calibracion IS
    'ok | zona_baja | arriba_de_calibracion | sin_lectura. NULL = sin calibración válida.';
