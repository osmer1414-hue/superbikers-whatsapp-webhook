import os
import json
import logging
from flask import Flask, request, jsonify

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers-whatsapp-webhook")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")

@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "Superbikers WhatsApp Webhook"
    }), 200

@app.get("/webhook/whatsapp")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        logger.info("Webhook verificado correctamente por Meta.")
        return challenge, 200

    logger.warning("Intento de verificación rechazado.")
    return "Forbidden", 403

@app.post("/webhook/whatsapp")
def receive_webhook():
    payload = request.get_json(silent=True) or {}
    logger.info("Webhook recibido: %s", json.dumps(payload, ensure_ascii=False))
    return jsonify({"received": True}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
