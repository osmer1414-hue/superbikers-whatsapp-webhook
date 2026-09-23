import os
import json
import logging
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers-whatsapp-webhook")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")

# Sesiones temporales por número de WhatsApp
# Durante esta fase de prueba almacenaremos aquí las 10 fotos
pending_motos = {}


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "Superbikers WhatsApp Webhook"
    }), 200


@app.get("/privacy")
def privacy():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Política de Privacidad - Superbikers Shop</title>
    </head>
    <body style="font-family:Arial;max-width:800px;margin:40px auto;line-height:1.6;">
        <h1>Política de Privacidad de Superbikers Shop</h1>

        <p>En Superbikers Shop respetamos la privacidad de nuestros clientes y usuarios.</p>

        <p>La información recibida a través de WhatsApp, Facebook, Instagram
        y nuestros canales digitales se utiliza para atender solicitudes,
        proporcionar información sobre motocicletas, responder consultas
        y gestionar publicaciones relacionadas con nuestros servicios.</p>

        <p>No vendemos ni comercializamos información personal.</p>

        <p>Última actualización: septiembre de 2026.</p>
    </body>
    </html>
    """, 200


@app.get("/webhook/whatsapp")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        logger.info("Webhook verificado correctamente por Meta.")
        return challenge, 200

    return "Forbidden", 403


def normalize_text(text):
    if not text:
        return ""

    return (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def extract_price(text):
    patterns = [
        r'\$\s?\d{1,3}(?:[,\.\s]\d{3})+',
        r'\$\s?\d+',
        r'(?i)precio\s*(?:de)?\s*\$?\s*(\d{1,3}(?:[,\.\s]\d{3})+|\d+)'
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            value = match.group(1) if match.lastindex else match.group(0)
            digits = re.sub(r"[^\d]", "", value)

            if digits:
                return f"${int(digits):,}"

    return ""


def extract_cover_title(text):
    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    for line in lines:

        if line.startswith("#"):
            continue

        if re.fullmatch(r'[\$\d\.,\s]+', line):
            continue

        return line

    return ""


def extract_hashtags(text):
    return re.findall(r'#\w+', text)


def get_session(sender):
    if sender not in pending_motos:

        pending_motos[sender] = {
            "photos": [],
            "text": ""
        }

    return pending_motos[sender]


def finalize_moto(sender):

    session = pending_motos.get(sender)

    if not session:
        return None

    if len(session["photos"]) != 10:
        return None

    if not session["text"]:
        return None

    text = session["text"]

    moto = {

        # Texto completo para Facebook / Instagram
        "full_text_original": text,

        # Datos para portada
        "cover_title": extract_cover_title(text),
        "cover_price": extract_price(text),

        # Hashtags originales
        "hashtags": extract_hashtags(text),

        # Regla Superbikers
        "total_photos": 10,

        # FOTO 1 = PORTADA
        "cover_media_id": session["photos"][0],

        # Las 10 fotos en orden
        "photos": session["photos"],

        # Fotos 2-10
        "gallery_photos": session["photos"][1:]
    }

    logger.info(
        "\n\n================ NUEVA MOTO COMPLETA ================\n%s\n=====================================================\n",
        json.dumps(moto, ensure_ascii=False, indent=2)
    )

    # Limpiamos para recibir la siguiente moto
    pending_motos[sender] = {
        "photos": [],
        "text": ""
    }

    return moto


@app.post("/webhook/whatsapp")
def receive_webhook():

    payload = request.get_json(silent=True) or {}

    logger.info(
        "Webhook recibido RAW: %s",
        json.dumps(payload, ensure_ascii=False)
    )

    completed_motos = []

    for entry in payload.get("entry", []):

        for change in entry.get("changes", []):

            value = change.get("value", {})

            for message in value.get("messages", []):

                sender = message.get("from")

                if not sender:
                    continue

                session = get_session(sender)

                message_type = message.get("type")

                # ------------------------------
                # FOTO
                # ------------------------------

                if message_type == "image":

                    image = message.get("image", {})

                    media_id = image.get("id")

                    caption = normalize_text(
                        image.get("caption", "")
                    )

                    if media_id:

                        # Solo aceptamos las primeras 10 fotos
                        if len(session["photos"]) < 10:

                            session["photos"].append(media_id)

                            numero = len(session["photos"])

                            logger.info(
                                "FOTO %s/10 recibida | sender=%s | media_id=%s",
                                numero,
                                sender,
                                media_id
                            )

                            if numero == 1:
                                logger.info(
                                    ">>> FOTO #1 MARCADA COMO PORTADA <<<"
                                )

                            if numero == 10:
                                logger.info(
                                    ">>> LAS 10 FOTOS YA ESTÁN COMPLETAS <<<"
                                )

                    # Si alguna foto trae caption también podemos usarlo
                    if caption and not session["text"]:
                        session["text"] = caption


                # ------------------------------
                # TEXTO
                # ------------------------------

                elif message_type == "text":

                    text = normalize_text(
                        message.get("text", {}).get("body", "")
                    )

                    session["text"] = text

                    logger.info(
                        "TEXTO RECIBIDO:\n%s",
                        text
                    )


                # ------------------------------
                # INTENTAR TERMINAR LA MOTO
                # ------------------------------

                moto = finalize_moto(sender)

                if moto:
                    completed_motos.append(moto)


    return jsonify({
        "received": True,
        "completed_motos": len(completed_motos)
    }), 200


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
