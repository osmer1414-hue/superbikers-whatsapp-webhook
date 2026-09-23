import os
import json
import logging
import re
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


def normalize_text(text: str) -> str:
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def extract_price(text: str) -> str:
    if not text:
        return ""

    # Busca algo tipo $159,900 o precio 159900
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


def extract_cover_title(text: str) -> str:
    if not text:
        return ""

    lines = [line.strip() for line in text.split("\n") if line.strip()]

    for line in lines:
        # Ignorar hashtags puros
        if line.startswith("#"):
            continue

        # Ignorar líneas que sean solo precio/números
        if re.fullmatch(r'[\$\d\.,\s]+', line):
            continue

        return line

    return lines[0] if lines else ""


def extract_flags(text: str):
    if not text:
        return []

    lowered = text.lower()
    keywords = [
        "preventa",
        "nacional",
        "nuevo ingreso",
        "full system",
        "impecable",
        "factura original",
        "único dueño",
        "como nueva",
        "importada"
    ]

    found = []
    for keyword in keywords:
        if keyword in lowered:
            found.append(keyword)

    return found


def extract_hashtags(text: str):
    if not text:
        return []
    return re.findall(r'#\w+', text)


def build_message_summary(message: dict, metadata: dict, contacts_map: dict):
    message_type = message.get("type", "")
    sender_wa_id = message.get("from", "")
    contact_name = contacts_map.get(sender_wa_id, "")

    original_text = ""
    media_id = None

    if message_type == "text":
        original_text = message.get("text", {}).get("body", "")

    elif message_type == "image":
        image_data = message.get("image", {})
        original_text = image_data.get("caption", "")
        media_id = image_data.get("id")

    elif message_type == "document":
        doc_data = message.get("document", {})
        original_text = doc_data.get("caption", "")
        media_id = doc_data.get("id")

    elif message_type == "video":
        video_data = message.get("video", {})
        original_text = video_data.get("caption", "")
        media_id = video_data.get("id")

    elif message_type == "audio":
        media_id = message.get("audio", {}).get("id")

    elif message_type == "sticker":
        media_id = message.get("sticker", {}).get("id")

    original_text = normalize_text(original_text)

    summary = {
        "message_id": message.get("id"),
        "from": sender_wa_id,
        "contact_name": contact_name,
        "type": message_type,
        "timestamp": message.get("timestamp"),
        "to_phone_number": metadata.get("display_phone_number"),
        "phone_number_id": metadata.get("phone_number_id"),

        # Esto se conserva tal cual para Facebook / Instagram
        "full_text_original": original_text,

        # Esto es solo para la portada automática
        "cover_title": extract_cover_title(original_text),
        "cover_price": extract_price(original_text),
        "flags": extract_flags(original_text),
        "hashtags": extract_hashtags(original_text),

        # Para más adelante, cuando descarguemos imágenes/videos
        "media_id": media_id
    }

    return summary


@app.post("/webhook/whatsapp")
def receive_webhook():
    payload = request.get_json(silent=True) or {}
    logger.info("Webhook recibido RAW: %s", json.dumps(payload, ensure_ascii=False))

    processed_messages = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            metadata = value.get("metadata", {})

            contacts_map = {}
            for contact in value.get("contacts", []):
                wa_id = contact.get("wa_id")
                profile_name = contact.get("profile", {}).get("name", "")
                if wa_id:
                    contacts_map[wa_id] = profile_name

            for message in value.get("messages", []):
                summary = build_message_summary(message, metadata, contacts_map)
                processed_messages.append(summary)

                logger.info(
                    "MENSAJE PROCESADO:\n%s",
                    json.dumps(summary, ensure_ascii=False, indent=2)
                )

    return jsonify({
        "received": True,
        "messages_processed": len(processed_messages),
        "messages": processed_messages
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
