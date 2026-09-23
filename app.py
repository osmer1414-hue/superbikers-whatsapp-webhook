import os
import json
import logging
import re
import mimetypes
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_from_directory
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers-whatsapp-webhook")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v26.0")
BASE_URL = os.environ.get("BASE_URL", "https://superbikers-whatsapp-webhook-2.onrender.com")

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


def get_font(size, bold=False):
    candidates = []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)

    return ImageFont.load_default()


def wrap_text(draw, text, font, max_width):
    words = text.split()
    if not words:
        return []

    lines = []
    current = words[0]

    for word in words[1:]:
        test = current + " " + word
        bbox = draw.textbbox((0, 0), test, font=font)
        width = bbox[2] - bbox[0]
        if width <= max_width:
            current = test
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def fit_title_lines(draw, text, max_width, max_lines=3):
    for size in [72, 66, 60, 56, 52, 48, 44, 40, 36]:
        font = get_font(size, bold=True)
        lines = wrap_text(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
    font = get_font(34, bold=True)
    lines = wrap_text(draw, text, font, max_width)[:max_lines]
    return font, lines


def create_cover_image(moto, sender):
    cover_path = moto.get("cover_file_path")
    title = moto.get("cover_title") or "MOTOCICLETA DISPONIBLE"
    price = moto.get("cover_price") or "PRECIO DISPONIBLE"

    if not cover_path or not Path(cover_path).exists():
        logger.error("No existe la foto de portada para generar imagen.")
        return None

    output_folder = Path("/tmp/superbikers") / sender / "generated"
    output_folder.mkdir(parents=True, exist_ok=True)

    with Image.open(cover_path).convert("RGB") as original:
        canvas_width, canvas_height = 1080, 1350
        canvas = Image.new("RGB", (canvas_width, canvas_height), (20, 20, 20))

        img = original.copy()
        img.thumbnail((canvas_width, canvas_height), Image.Resampling.LANCZOS)
        x = (canvas_width - img.width) // 2
        y = (canvas_height - img.height) // 2
        canvas.paste(img, (x, y))

        overlay = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)

        # Degradado superior
        for i in range(340):
            alpha = int(185 * (1 - i / 340))
            odraw.rectangle([(0, i), (canvas_width, i + 1)], fill=(0, 0, 0, alpha))

        # Degradado inferior
        for i in range(430):
            alpha = int(210 * (i / 430))
            y0 = canvas_height - 430 + i
            odraw.rectangle([(0, y0), (canvas_width, y0 + 1)], fill=(0, 0, 0, alpha))

        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(canvas)

        title_font, title_lines = fit_title_lines(draw, title, max_width=900, max_lines=3)
        price_font = get_font(54, bold=True)
        brand_font = get_font(34, bold=True)

        # Título arriba
        current_y = 70
        for line in title_lines:
            bbox = draw.textbbox((0, 0), line, font=title_font)
            text_width = bbox[2] - bbox[0]
            draw.text(
                ((canvas_width - text_width) / 2, current_y),
                line,
                font=title_font,
                fill=(255, 255, 255, 255)
            )
            current_y += (bbox[3] - bbox[1]) + 8

        # Precio en recuadro
        pbox = draw.textbbox((0, 0), price, font=price_font)
        pw = pbox[2] - pbox[0]
        ph = pbox[3] - pbox[1]
        box_padding_x = 34
        box_padding_y = 20
        box_w = pw + box_padding_x * 2
        box_h = ph + box_padding_y * 2
        box_x = (canvas_width - box_w) / 2
        box_y = canvas_height - 225

        draw.rounded_rectangle(
            [(box_x, box_y), (box_x + box_w, box_y + box_h)],
            radius=18,
            fill=(255, 255, 255, 235)
        )
        draw.text(
            (box_x + box_padding_x, box_y + box_padding_y - 4),
            price,
            font=price_font,
            fill=(0, 0, 0, 255)
        )

        # Marca abajo
        brand_text = "Superbikers Shop"
        bb = draw.textbbox((0, 0), brand_text, font=brand_font)
        bw = bb[2] - bb[0]
        brand_y = canvas_height - 95
        draw.text(
            ((canvas_width - bw) / 2, brand_y),
            brand_text,
            font=brand_font,
            fill=(255, 255, 255, 255)
        )

        filename = safe_filename(title) + "_portada.jpg"
        output_path = output_folder / filename
        canvas.convert("RGB").save(output_path, "JPEG", quality=95)

    url = f"{BASE_URL}/generated/{sender}/{filename}"

    logger.info("================ CREANDO PORTADA ================")
    logger.info("Título: %s", title)
    logger.info("Precio: %s", price)
    logger.info("Foto usada: %s", cover_path)
    logger.info("PORTADA CREADA: %s", output_path)
    logger.info("PORTADA URL: %s", url)
    logger.info("================================================")

    return {
        "file_path": str(output_path),
        "url": url,
        "filename": filename,
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
        "hashtags": extract_hashtags(text),
        "total_photos": 10,
        "cover_media_id": session["photos"][0]["media_id"],
        "cover_file_path": session["photos"][0]["file_path"],
        "photos": session["photos"],
        "gallery_photos": session["photos"][1:]
    }

    portada = create_cover_image(moto, sender)
    if portada:
        moto["generated_cover"] = portada

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
