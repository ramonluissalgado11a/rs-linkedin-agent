# RS LinkedIn Agent — Claude Edition

Agente de networking y marca personal para profesionales de supply chain, logística, transporte, almacén, inventario, compras, flota y operaciones. **Claude es el cerebro; LinkedIn solo se toca a través de APIs oficialmente autorizadas.** Lo que no tiene API autorizada se convierte en una acción `MANUAL_ACTION_REQUIRED` en la cola de aprobación: el agente prepara, tú lo haces en la app.

## Arquitectura

```
                 ┌──────────────── Web (Starlette) ────────────────┐
 Navegador ──▶   │ /login  /  (dashboard + cola)  /api/*  /oauth/* │
                 │ /health  /ready                                 │
                 └──────┬───────────────────────────────┬──────────┘
                        │                               │
          ┌─────────────▼─────────────┐    ┌────────────▼────────────┐
          │ Qualification engine      │    │ Content engine (Claude) │
          │ discovered→identity→      │    │ lint + dedup semántico  │
          │ employment→industry→role→ │    └────────────┬────────────┘
          │ dedup→qualified/rejected  │                 │
          └─────────────┬─────────────┘                 │
                        └──────────┬────────────────────┘
                          ┌────────▼─────────┐
                          │ Policy engine    │  check_permission / policy / rate_limit /
                          │ (compliance)     │  duplicate / recent_action / target / approval
                          └────────┬─────────┘
           ┌───────────────────────┼───────────────────────┐
  APPROVED_AUTOMATION     MANUAL_APPROVAL_REQUIRED   MANUAL_ACTION_REQUIRED / BLOCKED_*
  (solo si lo activas)    (cola → Approve)           (tú lo haces / se registra el motivo)
           └───────────┬───────────┘
                ┌──────▼──────┐   reintentos con backoff, DLQ, idempotencia,
                │ Job queue   │   timeouts, circuit breaker por proveedor
                └──────┬──────┘
                ┌──────▼──────┐
                │ Worker      │──▶ LinkedIn Posts API (w_member_social)
                └─────────────┘
         PostgreSQL (prod) / SQLite (dev/test) · logs JSON con redacción · audit_log
```

## Estructura del repositorio

```
rs_agent/
  config.py         configuración central (versión API LinkedIn, modelos Claude, presupuestos, límites)
  logging_setup.py  logs JSON estructurados, run_id/correlation_id, redacción de secretos
  db.py             capa de BD portable + migraciones
  errors.py         taxonomía de errores, registro de errores y auditoría
  dedup.py          claves estables y hashes (personas, empresas, URLs, acciones, contenido)
  qualification.py  pipeline people-first con reason codes y KPI
  policy.py         motor de cumplimiento y estados
  approvals.py      propuesta de acciones + cola human-in-the-loop
  jobs.py           cola de trabajos (retry, backoff, DLQ, idempotencia, timeout, circuit breaker)
  linkedin.py       OAuth 2.0, detección dinámica de scopes, Posts API, cifrado de tokens
  claude.py         routing de modelos, costo por tokens, presupuestos, circuit breaker
  content.py        generación de posts humanizados + controles de calidad
  executor.py       ejecución de acciones aprobadas (re-verifica permisos antes de actuar)
  metrics.py        salud de componentes y métricas del dashboard
  web.py            app web, dashboard, API, OAuth
  worker.py         proceso worker
  server.py         proceso web
  templates/        HTML del login y dashboard
migrations/         SQL de esquema (14 tablas pedidas + soporte)
tests/              suite unittest/pytest (sin red: LinkedIn y Claude simulados)
Dockerfile · render.yaml · .github/workflows/ci.yml · .env.example
docs/RUNBOOK.md · docs/SECURITY.md · docs/LINKEDIN_CAPABILITIES.md
```

## Arranque local

```bash
cp .env.example .env            # rellena los valores
pip install -r requirements-dev.txt
python -m rs_agent.server       # web en http://localhost:8000
python -m rs_agent.worker       # en otra terminal
python -m pytest                # o: python -m unittest discover -s tests -t .
```

## Flujo de uso

1. Entra en `/login` con tu `ADMIN_TOKEN`.
2. Conecta LinkedIn en `/oauth/linkedin/start` (scopes: `openid profile w_member_social`).
3. Envía candidatos desde fuentes autorizadas a `POST /api/candidates` (por ejemplo, la lista diaria de tu asistente de networking). El motor los califica y deduplica.
4. Propón acciones con `POST /api/actions` o genera posts con el content engine.
5. Revisa la cola en el dashboard: **Approve / Edit / Skip / Reject**. Los posts aprobados los publica el worker; las acciones manuales las haces tú y marcas **Done**.

## API

| Método | Ruta | Uso |
|---|---|---|
| GET | `/health` | proceso vivo |
| GET | `/ready` | BD accesible |
| GET | `/api/dashboard` | métricas y estados (Bearer ADMIN_TOKEN) |
| GET | `/api/queue?status=pending` | cola de aprobación |
| POST | `/api/queue/{id}` | `{"decision": "approve|reject|edit|skip|mark_done", "content": "..."}` |
| POST | `/api/candidates` | `{"source": {"kind": "public_web|referral|manual_entry|csv_import|networking_assistant", "name": "...", "url": "..."}, "candidates": [...]}` |
| POST | `/api/actions` | `{"action_type": "CREATE_POST|FOLLOW|CONNECT|REACT|COMMENT|MESSAGE", ...}` |
