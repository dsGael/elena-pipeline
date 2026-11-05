-- 21_fuel_eventos_ignicion.sql
-- Nuevos tipos en fuel_events: encendido y apagado (cambios de gps_records.ignicion) y
-- ralenti_en_curso (ralentí que todavía no termina; 'ralenti' se guarda cuando termina).
-- Se puede correr más de una vez y no borra datos. Requiere sql/19_fuel_eventos.sql.
-- Correr ANTES de actualizar el código en el servidor.

ALTER TABLE public.fuel_events DROP CONSTRAINT IF EXISTS fuel_events_evento_check;
ALTER TABLE public.fuel_events ADD CONSTRAINT fuel_events_evento_check CHECK (evento IN
    ('carga', 'extraccion', 'aceleracion', 'frenado', 'ralenti', 'giro_brusco',
     'ralenti_en_curso', 'encendido', 'apagado'));

COMMENT ON COLUMN public.fuel_events.cantidad IS
    'Litros cargados o extraídos (siempre positivos), minutos (ralentí y ralentí en curso: los '
    'que lleva). NULL en maniobras, encendido y apagado.';
