# Superbikers WhatsApp Webhook

Webhook inicial para conectar WhatsApp Business Platform (Meta) con el sistema de Superbikers Shop.

## Rutas
- `GET /` — comprobación de que el servicio está activo.
- `GET /webhook/whatsapp` — verificación de Meta.
- `POST /webhook/whatsapp` — recepción de eventos de WhatsApp.

## Variable de entorno requerida en Render
`VERIFY_TOKEN`

Usa una frase secreta creada por ti. Debe ser exactamente la misma en Render y en el campo
"Identificador de verificación" de Meta.

No publiques tokens de acceso de Meta en GitHub.
