# Security checklist

- [x] Sin contraseñas de LinkedIn: solo OAuth 2.0 authorization code con `state` de un solo uso y expiración de 10 minutos.
- [x] Tokens de acceso y refresh cifrados en reposo (Fernet, `TOKEN_ENCRYPTION_KEY`); nunca se registran en logs.
- [x] Redacción automática en logs de tokens, secretos, cookies, cabeceras Authorization y claves de API (pruebas incluidas).
- [x] Secretos solo por variables de entorno; `.env` en `.gitignore`; `.env.example` sin valores.
- [x] Validación de configuración al arrancar: en producción bloquea secretos débiles, falta de clave de cifrado y URLs sin HTTPS.
- [x] Admin protegido con token de 32+ caracteres (comparación en tiempo constante), cookie de sesión firmada `HttpOnly`, `SameSite=Strict`, `Secure` en producción.
- [x] CSRF en formularios del dashboard.
- [x] Plantillas con autoescape (prueba de XSS incluida). Cabeceras `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`.
- [x] Validación de entradas en la API (tamaños, tipos, fuentes autorizadas, decisiones válidas).
- [x] SQL siempre parametrizado.
- [x] Mínimo privilegio: contenedor sin root; scopes mínimos (`openid profile w_member_social`).
- [x] Audit log de propuestas, decisiones y ejecuciones.
- [x] Sin scraping, sin automatización de navegador, sin evasión, sin proxies, sin rotación de cuentas; acciones prohibidas bloqueadas por política.
- [x] Límites duros de publicación (1/hora, 2/día) que la configuración no puede superar.
- [x] CI: ruff, mypy estricto, tests, bandit, pip-audit, escaneo de secretos.
- [ ] Fijar versiones exactas con un lockfile antes de producción (pendiente).
- [ ] Ejecutar bandit y pip-audit (no disponibles en el entorno donde se construyó; corren en CI).
