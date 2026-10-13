# Qué permite LinkedIn y cómo lo maneja el agente

Verificado contra la documentación oficial de versionado de LinkedIn (Microsoft Learn): header `Linkedin-Version: YYYYMM`, última versión listada **202605**, cada versión soportada al menos 1 año. Valor centralizado en `LINKEDIN_API_VERSION`.

| Capacidad | API autorizada | Scope | Estado en el agente |
|---|---|---|---|
| Iniciar sesión / identificar al miembro | OpenID Connect `/v2/userinfo` | `openid profile` | Implementado |
| Publicar posts propios (texto) | Posts API `/rest/posts` | `w_member_social` | Implementado, con aprobación manual por defecto |
| Publicar como página de empresa | Posts API | `w_organization_social` + rol admin | Catalogado; bloqueado hasta que el scope exista (`BLOCKED_PERMISSION`) |
| Leer el feed / posts de otros | Restringido (`r_member_social`, partner) | — | No se asume; degradación elegante |
| Comentar o reaccionar en posts de terceros | No disponible para esta app | — | `MANUAL_ACTION_REQUIRED` + `HUMAN_RESPONSE_REQUIRED` |
| Seguir perfiles | No disponible | — | `MANUAL_ACTION_REQUIRED` |
| Enviar invitaciones de conexión | No disponible | — | `MANUAL_ACTION_REQUIRED` |
| Mensajes directos | No disponible | — | `MANUAL_ACTION_REQUIRED` |
| Buscar o extraer perfiles | No permitido | — | No implementado (`BLOCKED_POLICY` para SCRAPE/BULK_*) |

Los scopes concedidos se detectan en cada autorización mediante el endpoint de introspección de tokens y se guardan en `api_permissions`. Si LinkedIn aprueba más productos para tu app en el futuro, el agente lo detecta solo; aun así, cualquier nueva acción automática requiere agregarla explícitamente al catálogo de `policy.py`.
