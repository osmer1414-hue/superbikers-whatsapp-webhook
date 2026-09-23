import os
import json
import logging
import re
import mimetypes
import base64
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_from_directory
from openai import OpenAI

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers-whatsapp-webhook")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v26.0")
BASE_URL = os.environ.get("BASE_URL", "https://superbikers-whatsapp-webhook-2.onrender.com")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

pending_motos = {}


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "Superbikers WhatsApp Webhook"
    }), 200


@app.get("/privacy")
def privacy():
    return '''
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Política de Privacidad - Superbikers Shop</title>
    </head>
    <body style="font-family:Arial;max-width:800px;margin:40px auto;line-height:1.6;">
        <h1>Política de Privacidad de Superbikers Shop</h1>
        <p>En Superbikers Shop respetamos la privacidad de nuestros clientes y usuarios.</p>
        <p>La información recibida a través de WhatsApp, Facebook, Instagram y nuestros canales digitales
        se utiliza para atender solicitudes, proporcionar información sobre motocicletas,
        responder consultas y gestionar publicaciones relacionadas con nuestros servicios.</p>
        <p>No vendemos ni comercializamos información personal.</p>
        <p>Última actualización: septiembre de 2026.</p>
    </body>
    </html>
    ''', 200


@app.get("/generated/<sender>/<filename>")
def serve_generated(sender, filename):
    folder = Path("/tmp/superbikers") / sender / "generated"
    return send_from_directory(folder, filename)


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
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


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
        return clean_cover_title(line)

    return ""


def extract_hashtags(text):
    if not text:
        return []
    return re.findall(r'#\w+', text)


def extract_flags(text):
    if not text:
        return []

    lowered = text.lower()
    raw_flags = [
        ("preventa", "Preventa"),
        ("nacional", "Nacional"),
        ("nuevo ingreso", "Nuevo ingreso"),
        ("full system", "Full system"),
        ("placas de regalo", "Placas de regalo")
    ]

    found = []
    for key, label in raw_flags:
        if key in lowered:
            found.append(label)

    return found


def get_extension_from_mime(mime_type):
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    return mapping.get(mime_type) or mimetypes.guess_extension(mime_type or "") or ".jpg"


def download_whatsapp_image(media_id, sender, photo_number):
    if not WHATSAPP_ACCESS_TOKEN:
        logger.error("Falta WHATSAPP_ACCESS_TOKEN en Render.")
        return None

    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    info_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}"

    try:
        info_response = requests.get(info_url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        logger.exception("Error consultando media_id=%s: %s", media_id, exc)
        return None

    if info_response.status_code != 200:
        logger.error(
            "No se pudo obtener URL de la foto. status=%s respuesta=%s",
            info_response.status_code,
            info_response.text[:500]
        )
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
        logger.error(
            "No se pudo descargar la foto. status=%s respuesta=%s",
            image_response.status_code,
            image_response.text[:500]
        )
        return None

    extension = get_extension_from_mime(mime_type)
    folder = Path("/tmp/superbikers") / sender
    folder.mkdir(parents=True, exist_ok=True)

    filepath = folder / f"foto_{photo_number:02d}{extension}"
    filepath.write_bytes(image_response.content)

    logger.info(
        "FOTO %s/10 DESCARGADA | archivo=%s | bytes=%s",
        photo_number,
        filepath,
        len(image_response.content)
    )

    return str(filepath)


def get_session(sender):
    if sender not in pending_motos:
        pending_motos[sender] = {"photos": [], "text": ""}
    return pending_motos[sender]


def safe_filename(text):
    if not text:
        return "portada"
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip())
    return text[:80] or "portada"


def build_superbikers_prompt(moto):
    title = moto.get("cover_title") or "MOTOCICLETA DISPONIBLE"
    price = moto.get("cover_price") or "PRECIO DISPONIBLE"
    flags = moto.get("flags") or []

    flag_instruction = ""
    if flags:
        flag_instruction = (
            f'Si existe espacio visual adecuado, agrega una etiqueta pequeña con este texto: "{flags[0]}".\n'
        )

    prompt = f"""
Crear un post publicitario profesional para Superbikers Shop
utilizando la fotografía proporcionada como imagen principal.

REGLA PRINCIPAL:
Mantener la motocicleta y el fondo lo más originales posible.
No cambiar el modelo, color, accesorios, piezas, rines ni detalles
reales de la motocicleta.

Mejorar únicamente iluminación, contraste, claridad, profundidad
y acabado general de manera natural.

La motocicleta debe seguir siendo la protagonista.

TEXTO DE PORTADA:

Título:
"{title}"

Precio:
"{price}"

Marca inferior:
"Superbikers Shop"

DISEÑO:

Colocar el título en la zona superior con una composición moderna,
automotriz y ligeramente brush/graffiti.

El título debe ser llamativo pero de tamaño contenido.
No debe ocupar demasiado espacio ni tapar la motocicleta.

Colocar el precio dentro de un recuadro pequeño o mediano,
limpio y elegante.

Colocar "Superbikers Shop" pequeño en la parte inferior,
con estilo brush/graffiti/exótico.

{flag_instruction}
No agregar teléfonos.
No agregar direcciones.
No agregar hashtags.
No agregar textos que no fueron solicitados.
No inventar logotipos ni marcas de agua.

ESTÉTICA:
- imagen limpia
- fotografía realista
- publicidad premium de motocicletas
- alto contraste controlado
- iluminación atractiva
- sombras suaves
- diseño contemporáneo
- detalles gráficos mínimos
- mantener la imagen original reconocible
- evitar aspecto de plantilla genérica
"""
    return prompt.strip()


def create_cover_with_openai(moto, sender):
    if not openai_client:
        logger.error("Falta OPENAI_API_KEY en Render.")
        return None

    cover_path = moto.get("cover_file_path")
    if not cover_path or not Path(cover_path).exists():
        logger.error("No existe la foto de portada para generar imagen.")
        return None

    prompt = build_superbikers_prompt(moto)

    try:
        with open(cover_path, "rb") as image_file:
            result = openai_client.images.edit(
                model="gpt-image-1",
                image=image_file,
                prompt=prompt
            )
    except Exception as exc:
        logger.exception("Error generando portada con OpenAI: %s", exc)
        return None

    if not result.data or not result.data[0].b64_json:
        logger.error("OpenAI no devolvió imagen para la portada.")
        return None

    image_bytes = base64.b64decode(result.data[0].b64_json)

    output_folder = Path("/tmp/superbikers") / sender / "generated"
    output_folder.mkdir(parents=True, exist_ok=True)

    filename = safe_filename(moto.get("cover_title") or "portada") + "_portada_openai.png"
    output_path = output_folder / filename
    output_path.write_bytes(image_bytes)

    url = f"{BASE_URL}/generated/{sender}/{filename}"

    logger.info("=============== CREANDO PORTADA OPENAI ===============")
    logger.info("Título: %s", moto.get("cover_title"))
    logger.info("Precio: %s", moto.get("cover_price"))
    logger.info("Flags: %s", moto.get("flags"))
    logger.info("Foto usada: %s", cover_path)
    logger.info("PORTADA OPENAI CREADA: %s", output_path)
    logger.info("PORTADA OPENAI URL: %s", url)
    logger.info("======================================================")

    return {
        "file_path": str(output_path),
        "url": url,
        "filename": filename,
        "prompt": prompt
    }


def finalize_moto(sender):
    session = pending_motos.get(sender)

    if not session or len(session["photos"]) != 10 or not session["text"]:
        return None

    if any(not photo.get("file_path") for photo in session["photos"]):
        logger.error("La moto tiene 10 fotos pero alguna no se descargó correctamente.")
        return None

    text = session["text"]

    moto = {
        "full_text_original": text,
        "cover_title": extract_cover_title(text),
        "cover_price": extract_price(text),
        "flags": extract_flags(text),
        "hashtags": extract_hashtags(text),
        "total_photos": 10,
        "cover_media_id": session["photos"][0]["media_id"],
        "cover_file_path": session["photos"][0]["file_path"],
        "photos": session["photos"],
        "gallery_photos": session["photos"][1:]
    }

    generated_cover = create_cover_with_openai(moto, sender)
    if generated_cover:
        moto["generated_cover"] = generated_cover

    logger.info(
        "\n================ NUEVA MOTO COMPLETA ================\n%s\n=====================================================\n",
        json.dumps(moto, ensure_ascii=False, indent=2)
    )

    pending_motos[sender] = {"photos": [], "text": ""}
    return moto


@app.post("/webhook/whatsapp")
def receive_webhook():
    payload = request.get_json(silent=True) or {}
    logger.info("Webhook recibido RAW: %s", json.dumps(payload, ensure_ascii=False))

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

                if message_type == "image":
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

                        logger.info(
                            "FOTO %s/10 REGISTRADA | media_id=%s | file=%s",
                            photo_number,
                            media_id,
                            file_path
                        )

                        if photo_number == 1:
                            logger.info("FOTO #1 MARCADA COMO PORTADA")

                        if photo_number == 10:
                            logger.info("LAS 10 FOTOS YA ESTÁN COMPLETAS")

                    if caption and not session["text"]:
                        session["text"] = caption

                elif message_type == "text":
                    text = normalize_text(
                        message.get("text", {}).get("body", "")
                    )
                    session["text"] = text
                    logger.info("TEXTO RECIBIDO:\n%s", text)

                moto = finalize_moto(sender)
                if moto:
                    completed_motos.append(moto)

    return jsonify({
        "received": True,
        "completed_motos": len(completed_motos)
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
