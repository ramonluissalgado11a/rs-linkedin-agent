# Runbook

## Estados de salud
`HEALTHY` todo bien · `DEGRADED` un proveedor no disponible o sin configurar (el resto sigue funcionando) · `BLOCKED` token vencido o presupuesto agotado · `FAILED` la base de datos no responde.

## Despliegue en Render (manual, solo con CI en verde)
1. Crea una app en LinkedIn Developers con los productos *Sign In with LinkedIn using OpenID Connect* y *Share on LinkedIn*. Redirect URL: `https://<servicio>.onrender.com/oauth/linkedin/callback`.
2. En Render: **New → Blueprint** apuntando al repo. Se crean web, worker y PostgreSQL.
3. Completa en el dashboard de Render las variables `sync: false` (ver `.env.example`). Genera `ADMIN_TOKEN`, `SESSION_SECRET` y `TOKEN_ENCRYPTION_KEY` con los comandos del archivo.
4. Deploy manual. Las migraciones corren al arrancar. Verifica `GET /ready` → `{"ready": true}`.
5. Entra al dashboard y conecta LinkedIn.

## Incidentes comunes
| Síntoma | Causa probable | Acción |
|---|---|---|
| LinkedIn `BLOCKED` | token vencido / refresh falló | Reconecta en `/oauth/linkedin/start` |
| Posts en `BLOCKED_PERMISSION` | falta `w_member_social` | Revisa productos de la app LinkedIn y reconecta |
| Errores 426 de LinkedIn | versión de API retirada | Sube `LINKEDIN_API_VERSION` a una versión vigente y redeploy |
| Errores 429 | límite de LinkedIn | El worker respeta `Retry-After`; no subas límites |
| Claude `BLOCKED` | presupuesto diario/mensual alcanzado | Espera al reset o ajusta `DAILY_BUDGET_USD` conscientemente |
| Claude `FAILED` | circuit breaker abierto (errores 5xx/529) | Se recupera solo tras 5 min; revisa la tabla `errors` |
| Jobs en `dead` | error no reintentable o máximo de intentos | Revisa `jobs.last_error`; corrige y usa `requeue_dead` |
| `/ready` 503 | base de datos caída | Revisa la instancia PostgreSQL en Render |

## Rotación de secretos
- `ADMIN_TOKEN` / `SESSION_SECRET`: cambia en Render y redeploy (cierra todas las sesiones).
- `TOKEN_ENCRYPTION_KEY`: al rotarla los tokens guardados quedan ilegibles → reconecta LinkedIn.
- Credenciales de LinkedIn/Anthropic: rota en sus consolas y actualiza Render.

## Revisión trimestral
Verificar la versión vigente de la API de LinkedIn y los precios por token de Claude; actualizar variables sin tocar código.

## Despliegue GRATIS en Render (`render.free.yaml`)
El plan gratis de Render no permite workers, duerme el servicio web tras 15 minutos sin tráfico y borra la base de datos gratis a los 30 días. Por eso esta variante:
- usa **un solo servicio web** con `RUN_WORKER_IN_WEB=true` (el worker corre dentro del web);
- usa tu **Supabase** como base de datos: en `DATABASE_URL` pega la cadena de conexión URI del pooler (Project Settings → Database);
- al abrir el dashboard el servicio despierta (~1 minuto) y el worker procesa lo aprobado. Mientras duerme no publica nada, lo cual es aceptable porque cada post requiere tu aprobación.
Blueprint: en Render, New → Blueprint y elige `render.free.yaml`.
