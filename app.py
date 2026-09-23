import os
import re
import json
import base64
import logging
import mimetypes
import unicodedata
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_from_directory
from openai import OpenAI

app = Flask(__name__)

# =========================
# CONFIG
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v26.0")

TMP_ROOT = Path("/tmp/superbikers")
TMP_ROOT.mkdir(parents=True, exist_ok=True)

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# sesiones en memoria
pending_motos = {}


# =========================
# HELPERS GENERALES
# =========================
def normalize_text(text):
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def slugify(text):
    if not text:
        return "portada"
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")
    return text[:80] if text else "portada"


def get_extension_from_mime(mime_type):
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    return mapping.get(mime_type) or mimetypes.guess_extension(mime_type or "") or ".jpg"


def extract_price(text):
    if not text:
        return ""

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


def clean_cover_title(line):
    line = line.strip()
    line = re.sub(r'^[^\wÁÉÍÓÚÜÑáéíóúüñ]+', '', line)
    line = re.sub(r'[^\wÁÉÍÓÚÜÑáéíóúüñ\-\+\./ ]+$', '', line)
    return line.strip()


def extract_cover_title(text):
    if not text:
        return ""

    lines = [line.strip() for line in text.split("\n") if line.strip()]

    for line in lines:
        if line.startswith("#"):
            continue
        if re.fullmatch(r'[\$\d\.,\s]+', line):
            continue
        if "aceptamos" in line.lower():
            continue
        return clean_cover_title(line)

    return "MOTO DISPONIBLE"


def extract_hashtags(text):
    if not text:
        return []
    return re.findall(r'#\w+', text)


def extract_flags(text):
    if not text:
        return []

    text_low = text.lower()
    found = []

    posibles = [
        "preventa",
        "full system",
        "nacional",
        "nuevo ingreso",
        "impecable",
        "factura de importación",
        "pedimento",
    ]

    for item in posibles:
        if item in text_low:
            found.append(item)

    return found


def get_session(sender):
    if sender not in pending_motos:
        pending_motos[sender] = {"photos": [], "text": ""}
    return pending_motos[sender]


# =========================
# WHATSAPP CLOUD API
# =========================
def send_whatsapp_text(to_number, text_body):
    if not WHATSAPP_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        logger.error("Falta WHATSAPP_ACCESS_TOKEN o PHONE_NUMBER_ID.")
        return None

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {
            "body": text_body
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        logger.info("WHATSAPP TEXT RESPONSE: %s | %s", response.status_code, response.text[:500])
        return response
    except Exception as e:
        logger.exception("Error enviando texto por WhatsApp: %s", e)
        return None


def send_whatsapp_image_by_link(to_number, image_url, caption="Aquí está tu portada"):
    if not WHATSAPP_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        logger.error("Falta WHATSAPP_ACCESS_TOKEN o PHONE_NUMBER_ID.")
        return None

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "image",
        "image": {
            "link": image_url,
            "caption": caption
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        logger.info("WHATSAPP IMAGE RESPONSE: %s | %s", response.status_code, response.text[:500])
        return response
    except Exception as e:
        logger.exception("Error enviando imagen por WhatsApp: %s", e)
        return None


# =========================
# DESCARGA DE IMÁGENES
# =========================
def download_whatsapp_image(media_id, sender, photo_number):
    if not WHATSAPP_ACCESS_TOKEN:
        logger.error("Falta WHATSAPP_ACCESS_TOKEN.")
        return None

    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    info_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}"

    try:
        info_response = requests.get(info_url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        logger.exception("Error consultando media_id=%s: %s", media_id, exc)
        return None

    if info_response.status_code != 200:
        logger.error("No se pudo obtener URL de foto. %s | %s", info_response.status_code, info_response.text[:500])
        return None

    media_info = info_response.json()
    media_url = media_info.get("url")
    mime_type = media_info.get("mime_type", "image/jpeg")

    if not media_url:
        logger.error("Meta no devolvió URL para media_id=%s", media_id)
        return None

    try:
        image_response = requests.get(media_url, headers=headers, timeout=60)
    except requests.RequestException as exc:
        logger.exception("Error descargando media_id=%s: %s", media_id, exc)
        return None

    if image_response.status_code != 200:
        logger.error("No se pudo descargar la foto. %s | %s", image_response.status_code, image_response.text[:500])
        return None

    extension = get_extension_from_mime(mime_type)
    folder = TMP_ROOT / sender
    folder.mkdir(parents=True, exist_ok=True)

    filepath = folder / f"foto_{photo_number:02d}{extension}"
    filepath.write_bytes(image_response.content)

    logger.info("FOTO %s/10 DESCARGADA | %s", photo_number, filepath)
    return str(filepath)


# =========================
# PARSEO DE MOTO COMPLETA
# =========================
def finalize_moto(sender):
    session = pending_motos.get(sender)

    if not session:
        return None

    if len(session["photos"]) != 10:
        return None

    if not session["text"]:
        return None

    if any(not photo.get("file_path") for photo in session["photos"]):
        logger.error("Hay fotos sin descargar correctamente.")
        return None

    text = session["text"]

    moto = {
        "full_text_original": text,
        "cover_title": extract_cover_title(text),
        "cover_price": extract_price(text),
        "hashtags": extract_hashtags(text),
        "flags": extract_flags(text),
        "total_photos": 10,
        "cover_media_id": session["photos"][0]["media_id"],
        "cover_file_path": session["photos"][0]["file_path"],
        "photos": session["photos"],
        "gallery_photos": session["photos"][1:]
    }

    logger.info(
        "\n================ MOTO COMPLETA DETECTADA ================\n%s\n=========================================================\n",
        json.dumps(moto, ensure_ascii=False, indent=2)
    )

    # limpiamos la sesión para el siguiente lote
    pending_motos[sender] = {"photos": [], "text": ""}
    return moto


# =========================
# OPENAI - GENERAR PORTADA
# =========================
def build_cover_prompt(title, price, flags):
    flags_text = ", ".join(flags) if flags else ""

    extra_flag_instruction = ""
    if flags_text:
        extra_flag_instruction = (
            f'Agrega en pequeño cerca del título etiquetas o subtítulos relacionados con: "{flags_text}". '
        )

    price_instruction = ""
    if price:
        price_instruction = (
            f'Coloca EXACTAMENTE el precio "{price}" dentro de un recuadro pequeño debajo de la moto. '
            'Es obligatorio que aparezca el precio exacto y NO se debe reemplazar por frases como '
            '"PRECIO DISPONIBLE", "CONSULTA PRECIO" o similares. '
        )
    else:
        price_instruction = (
            'Si no hay precio detectado, coloca un recuadro pequeño abajo de la moto con estilo limpio. '
        )

    prompt = f"""
Edita la imagen proporcionada y conviértela en un post publicitario vertical profesional para motocicleta.

IMPORTANTE:
- Conserva la moto y el fondo lo más originales posible.
- Estilo visual: limpio, contrastado, elegante, publicitario, moderno.
- La portada debe verse como un anuncio premium de Superbikers Shop.
- Sin saturar demasiado la composición.
- No cambies el modelo de la moto.
- Mantén la moto protagonista y nítida.
- Iluminación atractiva y sombras suaves.
- Sin bordes gruesos innecesarios.

TEXTO:
- Título principal grande en la parte superior, más grande que antes, con tipografía agresiva tipo brush/graffiti automotriz.
- El texto del título debe decir EXACTAMENTE: "{title}".
- {extra_flag_instruction}
- {price_instruction}
- En la parte inferior agrega "Superbikers Shop" en estilo brush/graffiti limpio.
- El recuadro del precio debe ser pequeño y estar claramente DEBAJO de la moto.
- Jamás cambies el texto del precio por otra cosa.

COMPOSICIÓN:
- Formato vertical tipo post para redes sociales.
- Título arriba grande.
- Recuadro del precio abajo de la moto.
- Logo/nombre "Superbikers Shop" pequeño en la parte inferior.
- Debe sentirse como una portada de venta de moto de alta calidad.
"""
    return prompt.strip()


def create_cover_with_openai(sender, moto):
    if not OPENAI_API_KEY:
        raise ValueError("Falta OPENAI_API_KEY")

    if not BASE_URL:
        raise ValueError("Falta BASE_URL")

    cover_file = moto["cover_file_path"]
    title = moto["cover_title"] or "MOTO DISPONIBLE"
    price = moto["cover_price"] or ""
    flags = moto["flags"] or []

    logger.info("=========== OPENAI PORTADA ===========")
    logger.info("Título: %s", title)
    logger.info("Precio: %s", price)
    logger.info("Foto base: %s", cover_file)

    prompt = build_cover_prompt(title, price, flags)

    with open(cover_file, "rb") as image_file:
        result = openai_client.images.edit(
            model="gpt-image-1",
            image=image_file,
            prompt=prompt
        )

    image_b64 = result.data[0].b64_json
    image_bytes = base64.b64decode(image_b64)

    generated_dir = TMP_ROOT / sender / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{slugify(title)}_openai.png"
    output_path = generated_dir / filename
    output_path.write_bytes(image_bytes)

    public_url = f"{BASE_URL}/generated/{sender}/{filename}"

    generated_cover = {
        "file_path": str(output_path),
        "url": public_url,
        "filename": filename
    }

    logger.info("PORTADA GENERADA: %s", json.dumps(generated_cover, ensure_ascii=False, indent=2))
    return generated_cover


# =========================
# RUTAS
# =========================
@app.get("/")
def home():
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
        <p>La información recibida a través de WhatsApp se utiliza para atender solicitudes, generar portadas y gestionar publicaciones.</p>
        <p>No vendemos información personal.</p>
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
        logger.info("Webhook verificado correctamente.")
        return challenge, 200

    return "Forbidden", 403


@app.get("/generated/<sender>/<filename>")
def serve_generated_image(sender, filename):
    folder = TMP_ROOT / sender / "generated"
    return send_from_directory(folder, filename)


# =========================
# WEBHOOK PRINCIPAL
# =========================
@app.post("/webhook/whatsapp")
def receive_webhook():
    payload = request.get_json(silent=True) or {}
    logger.info("WEBHOOK RECIBIDO")
    logger.info(json.dumps(payload, ensure_ascii=False))

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            for message in value.get("messages", []):
                sender = message.get("from")
                if not sender:
                    continue

                session = get_session(sender)
                message_type = message.get("type")

                # TEXTO
                if message_type == "text":
                    text = normalize_text(message.get("text", {}).get("body", ""))
                    session["text"] = text
                    logger.info("TEXTO RECIBIDO")
                    logger.info(text)

                # IMAGEN
                elif message_type == "image":
                    image = message.get("image", {})
                    media_id = image.get("id")
                    caption = normalize_text(image.get("caption", ""))

                    if media_id and len(session["photos"]) < 10:
                        photo_number = len(session["photos"]) + 1
                        file_path = download_whatsapp_image(
                            media_id=media_id,
                            sender=sender,
                            photo_number=photo_number
                        )

                        session["photos"].append({
                            "number": photo_number,
                            "media_id": media_id,
                            "file_path": file_path
                        })

                        logger.info("FOTO %s/10 REGISTRADA | %s", photo_number, file_path)

                        if photo_number == 1:
                            logger.info("FOTO #1 = PORTADA")

                        if photo_number == 10:
                            logger.info("LAS 10 FOTOS YA ESTÁN COMPLETAS")

                    if caption:
                        session["text"] = caption

                moto = finalize_moto(sender)

                if moto:
                    try:
                        logger.info("GENERANDO PORTADA CON OPENAI...")
                        generated_cover = create_cover_with_openai(sender, moto)

                        caption = f"Portada lista ✅\n{moto['cover_title']}"
                        if moto["cover_price"]:
                            caption += f"\n{moto['cover_price']}"

                        send_whatsapp_image_by_link(
                            to_number=sender,
                            image_url=generated_cover["url"],
                            caption=caption
                        )

                        # también manda el link por texto por si quieres abrirlo directo
                        send_whatsapp_text(
                            to_number=sender,
                            text_body=f"Aquí está tu portada:\n{generated_cover['url']}"
                        )

                    except Exception as e:
                        logger.exception("Error generando portada con OpenAI: %s", e)
                        send_whatsapp_text(
                            to_number=sender,
                            text_body="Hubo un error generando la portada. Revisa logs y vuelve a intentar."
                        )

    return jsonify({"received": True}), 200


# =========================
# MAIN
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
