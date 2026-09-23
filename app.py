import os
import re
import json
import time
import base64
import hashlib
import logging
import mimetypes
import threading
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_from_directory
from openai import OpenAI
from PIL import Image, ImageOps


# =========================================================
# CONFIGURACION
# =========================================================

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")

GRAPH_API_VERSION = os.environ.get(
    "GRAPH_API_VERSION",
    "v26.0"
)

BASE_URL = os.environ.get(
    "BASE_URL",
    "https://superbikers-whatsapp-webhook-2.onrender.com"
).rstrip("/")


# =========================================================
# FACEBOOK / INSTAGRAM
# =========================================================

FACEBOOK_PAGE_ID = os.environ.get(
    "FACEBOOK_PAGE_ID",
    ""
)

INSTAGRAM_USER_ID = os.environ.get(
    "INSTAGRAM_USER_ID",
    ""
)

META_PAGE_ACCESS_TOKEN = (
    os.environ.get("META_PAGE_ACCESS_TOKEN", "")
    or
    os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")
)


# =========================================================
# OPENAI
# =========================================================

OPENAI_IMAGE_MODEL = os.environ.get(
    "OPENAI_IMAGE_MODEL",
    "gpt-image-2.5-sunburst"
)

openai_client = (
    OpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_API_KEY
    else None
)


# =========================================================
# REGLA DEL SISTEMA
# =========================================================

ORIGINAL_PHOTO_COUNT = 9
FINAL_SOCIAL_PHOTO_COUNT = 10


# =========================================================
# ESTADO TEMPORAL
# =========================================================

pending_motos = {}
seen_message_ids = set()

state_lock = threading.Lock()


def new_session():
    return {
        "photos": [],
        "text": "",
        "price_override": "",
        "processing": False,
        "publishing": False,
        "waiting_for_price": False,
        "price_notice_sent": False,
        "phone_number_id": "",
        "generated_cover": None,
        "ready_to_publish": False,
        "facebook_post_id": None,
        "instagram_post_id": None
    }


# =========================================================
# UTILIDADES
# =========================================================

def sender_key(sender):

    return hashlib.sha256(
        sender.encode("utf-8")
    ).hexdigest()[:18]


def sender_folder(sender):

    return (
        Path("/tmp/superbikers")
        / sender_key(sender)
    )


def normalize_text(text):

    if not text:
        return ""

    return (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def safe_filename(text):

    text = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "_",
        (text or "portada").strip()
    )

    return text[:80] or "portada"


# =========================================================
# NUMERO WHATSAPP MEXICO
# =========================================================

def normalize_whatsapp_recipient(number):

    digits = re.sub(
        r"\D",
        "",
        number or ""
    )

    if (
        digits.startswith("521")
        and
        len(digits) == 13
    ):

        digits = "52" + digits[3:]

    logger.info(
        "DESTINATARIO NORMALIZADO: %s",
        digits
    )

    return digits


# =========================================================
# PRECIO
# =========================================================

def price_from_candidate(value):

    digits = re.sub(
        r"[^\d]",
        "",
        value or ""
    )

    if not digits:
        return ""

    try:
        number = int(digits)
    except ValueError:
        return ""

    if number < 10000 or number > 5000000:
        return ""

    return f"${number:,}"


def extract_price(text):

    if not text:
        return ""

    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    # Segunda linea
    if len(lines) >= 2:

        match = re.search(
            r'(?i)^\s*'
            r'(?:precio\s*:?\s*)?'
            r'\$?\s*'
            r'(\d{2,3}(?:[,\.\s]\d{3})+|\d{5,7})'
            r'\s*(?:mxn|pesos?)?'
            r'\s*$',
            lines[1]
        )

        if match:

            price = price_from_candidate(
                match.group(1)
            )

            if price:
                return price

    # Signo $
    match = re.search(
        r'\$\s*'
        r'(\d{2,3}(?:[,\.\s]\d{3})+|\d{5,7})',
        text
    )

    if match:

        price = price_from_candidate(
            match.group(1)
        )

        if price:
            return price

    # Palabra precio
    match = re.search(
        r'(?i)\bprecio\b'
        r'[^\d]{0,20}'
        r'(\d{2,3}(?:[,\.\s]\d{3})+|\d{5,7})',
        text
    )

    if match:

        price = price_from_candidate(
            match.group(1)
        )

        if price:
            return price

    return ""


def is_price_only_message(text):

    if not text:
        return False

    stripped = text.strip()

    return bool(
        re.fullmatch(
            r'(?i)'
            r'(?:precio\s*:?\s*)?'
            r'\$?\s*'
            r'\d{2,3}(?:[,\.\s]\d{3})+'
            r'\s*(?:mxn|pesos?)?',
            stripped
        )
        or
        re.fullmatch(
            r'(?i)'
            r'(?:precio\s*:?\s*)?'
            r'\$?\s*'
            r'\d{5,7}'
            r'\s*(?:mxn|pesos?)?',
            stripped
        )
    )


# =========================================================
# TITULO
# =========================================================

def clean_cover_title(line):

    line = line.strip()

    line = re.sub(
        r'^[^\wÁÉÍÓÚÜÑáéíóúüñ]+',
        '',
        line
    )

    line = re.sub(
        r'[^\wÁÉÍÓÚÜÑáéíóúüñ\-\+\./ ]+$',
        '',
        line
    )

    return line.strip()


def extract_cover_title(text):

    if not text:
        return ""

    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    for line in lines:

        if line.startswith("#"):
            continue

        if extract_price(line):
            continue

        return clean_cover_title(line)

    return ""


# =========================================================
# FLAGS
# =========================================================

def extract_flags(text):

    lowered = (
        text or ""
    ).lower()

    possible = [
        ("preventa", "Preventa"),
        ("nacional", "Nacional"),
        ("nuevo ingreso", "Nuevo ingreso"),
        ("full system", "Full system"),
        ("placas de regalo", "Placas de regalo"),
        ("impecable", "Impecable")
    ]

    flags = []

    for key, label in possible:

        if key in lowered:
            flags.append(label)

    return flags


# =========================================================
# MIME / EXTENSION
# =========================================================

def get_extension_from_mime(mime_type):

    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp"
    }

    return (
        mapping.get(mime_type)
        or
        mimetypes.guess_extension(
            mime_type or ""
        )
        or
        ".jpg"
    )


# =========================================================
# WHATSAPP TEXTO
# =========================================================

def send_whatsapp_text(
    recipient,
    phone_number_id,
    message
):

    recipient = normalize_whatsapp_recipient(
        recipient
    )

    phone_number_id = (
        phone_number_id
        or
        PHONE_NUMBER_ID
    )

    if (
        not WHATSAPP_ACCESS_TOKEN
        or
        not phone_number_id
    ):

        logger.error(
            "Falta token WhatsApp o PHONE_NUMBER_ID"
        )

        return False


    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{phone_number_id}/messages"
    )

    headers = {
        "Authorization":
            f"Bearer {WHATSAPP_ACCESS_TOKEN}",

        "Content-Type":
            "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {
            "body": message
        }
    }

    try:

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=60
        )

        logger.info(
            "WHATSAPP TEXT STATUS: %s",
            response.status_code
        )

        if response.status_code >= 400:

            logger.error(
                "WHATSAPP TEXT ERROR: %s",
                response.text[:1000]
            )

            return False

        return True

    except Exception as error:

        logger.exception(
            "Error enviando texto WhatsApp: %s",
            error
        )

        return False


# =========================================================
# WHATSAPP MEDIA
# =========================================================

def upload_media_to_whatsapp(
    phone_number_id,
    file_path
):

    phone_number_id = (
        phone_number_id
        or
        PHONE_NUMBER_ID
    )

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{phone_number_id}/media"
    )

    headers = {
        "Authorization":
            f"Bearer {WHATSAPP_ACCESS_TOKEN}"
    }

    try:

        with open(
            file_path,
            "rb"
        ) as image_file:

            files = {
                "file": (
                    Path(file_path).name,
                    image_file,
                    "image/png"
                )
            }

            data = {
                "messaging_product":
                    "whatsapp",

                "type":
                    "image/png"
            }

            response = requests.post(
                url,
                headers=headers,
                files=files,
                data=data,
                timeout=120
            )

        logger.info(
            "UPLOAD PORTADA STATUS: %s",
            response.status_code
        )

        if response.status_code >= 400:

            logger.error(
                "UPLOAD PORTADA ERROR: %s",
                response.text[:1000]
            )

            return None

        return response.json().get("id")

    except Exception as error:

        logger.exception(
            "Error subiendo portada WhatsApp: %s",
            error
        )

        return None


def send_cover_to_whatsapp(
    recipient,
    phone_number_id,
    file_path,
    title,
    price
):

    recipient = normalize_whatsapp_recipient(
        recipient
    )

    phone_number_id = (
        phone_number_id
        or
        PHONE_NUMBER_ID
    )

    media_id = upload_media_to_whatsapp(
        phone_number_id,
        file_path
    )

    if not media_id:
        return False

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{phone_number_id}/messages"
    )

    headers = {
        "Authorization":
            f"Bearer {WHATSAPP_ACCESS_TOKEN}",

        "Content-Type":
            "application/json"
    }

    caption = (
        f"✅ PORTADA LISTA\n"
        f"{title}\n"
        f"{price}\n\n"
        f"Escribe PUBLICAR si está correcta."
    )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "image",
        "image": {
            "id": media_id,
            "caption": caption
        }
    }

    try:

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=60
        )

        logger.info(
            "ENVIO PORTADA STATUS: %s",
            response.status_code
        )

        if response.status_code >= 400:

            logger.error(
                "ENVIO PORTADA ERROR: %s",
                response.text[:1000]
            )

            return False

        logger.info(
            "PORTADA ENVIADA AL WHATSAPP ✅"
        )

        return True

    except Exception as error:

        logger.exception(
            "Error enviando portada: %s",
            error
        )

        return False


# =========================================================
# DESCARGAR FOTOS WHATSAPP
# =========================================================

def try_download_url(
    media_url,
    headers
):

    if not media_url:
        return None

    for attempt in range(3):

        try:

            response = requests.get(
                media_url,
                headers=headers,
                timeout=60
            )

            if response.status_code == 200:
                return response.content

            logger.warning(
                "Descarga intento %s: HTTP %s",
                attempt + 1,
                response.status_code
            )

        except requests.RequestException as error:

            logger.warning(
                "Descarga intento %s fallo: %s",
                attempt + 1,
                error
            )

        time.sleep(
            0.8 * (attempt + 1)
        )

    return None


def download_whatsapp_image(
    media_id,
    sender,
    photo_number,
    direct_url=None,
    mime_type="image/jpeg"
):

    if not WHATSAPP_ACCESS_TOKEN:

        logger.error(
            "Falta WHATSAPP_ACCESS_TOKEN"
        )

        return None

    headers = {
        "Authorization":
            f"Bearer {WHATSAPP_ACCESS_TOKEN}"
    }

    image_bytes = None


    if direct_url:

        image_bytes = try_download_url(
            direct_url,
            headers
        )


    if not image_bytes:

        info_url = (
            f"https://graph.facebook.com/"
            f"{GRAPH_API_VERSION}/"
            f"{media_id}"
        )

        try:

            info_response = requests.get(
                info_url,
                headers=headers,
                timeout=30
            )

            if info_response.status_code == 200:

                media_info = (
                    info_response.json()
                )

                fallback_url = (
                    media_info.get("url")
                )

                mime_type = (
                    media_info.get("mime_type")
                    or
                    mime_type
                )

                image_bytes = try_download_url(
                    fallback_url,
                    headers
                )

            else:

                logger.error(
                    "No se pudo obtener URL del media. HTTP %s | %s",
                    info_response.status_code,
                    info_response.text[:500]
                )

        except requests.RequestException as error:

            logger.exception(
                "Error consultando media_id: %s",
                error
            )


    if not image_bytes:

        logger.error(
            "FOTO %s NO SE PUDO DESCARGAR",
            photo_number
        )

        return None


    extension = get_extension_from_mime(
        mime_type
    )

    folder = sender_folder(
        sender
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    filepath = (
        folder
        /
        f"foto_{photo_number:02d}{extension}"
    )

    filepath.write_bytes(
        image_bytes
    )

    logger.info(
        "FOTO %s/%s DESCARGADA CORRECTAMENTE",
        photo_number,
        ORIGINAL_PHOTO_COUNT
    )

    return str(filepath)


# =========================================================
# HACER PUBLICAS LAS 9 ORIGINALES
# =========================================================

def prepare_public_originals(
    sender,
    photos
):

    output_folder = (
        sender_folder(sender)
        /
        "generated"
    )

    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    public_id = sender_key(sender)

    result = []

    for index, photo in enumerate(
        photos,
        start=1
    ):

        source_path = Path(
            photo["file_path"]
        )

        filename = (
            f"original_{index:02d}.jpg"
        )

        output_path = (
            output_folder
            /
            filename
        )

        try:

            with Image.open(
                source_path
            ) as img:

                img = ImageOps.exif_transpose(
                    img
                )

                img = img.convert(
                    "RGB"
                )

                img.save(
                    output_path,
                    "JPEG",
                    quality=92,
                    optimize=True
                )

        except Exception as error:

            logger.exception(
                "Error preparando original %s: %s",
                index,
                error
            )

            return None


        public_url = (
            f"{BASE_URL}"
            f"/generated/"
            f"{public_id}/"
            f"{filename}"
        )

        result.append({
            "number":
                index,

            "file_path":
                str(output_path),

            "url":
                public_url
        })


    logger.info(
        "9 ORIGINALES PUBLICAS LISTAS ✅"
    )

    return result


# =========================================================
# PROMPT SUPERBIKERS
# =========================================================

def build_superbikers_prompt(
    title,
    price,
    flags
):

    flag_instruction = ""

    if flags:

        flag_instruction = f"""
Agregar una etiqueta pequeña con:

"{flags[0]}"

Debe ir integrada cerca del título.
"""


    return f"""
CREAR UNA PORTADA PUBLICITARIA PREMIUM
DE SUPERBIKERS SHOP EDITANDO
LA FOTOGRAFIA ORIGINAL.

MANTENER LA MOTOCICLETA
Y EL FONDO LO MAS ORIGINALES POSIBLE.

NO CAMBIAR:

- modelo
- color
- carenados
- faros
- escape
- rines
- llantas
- asiento
- tanque
- accesorios
- piezas
- proporciones

MEJORAR SOLAMENTE:

- iluminación
- contraste
- nitidez
- profundidad
- sombras suaves

LA MOTOCICLETA DEBE SEGUIR
PARECIENDO LA FOTO ORIGINAL.


TITULO GRANDE ARRIBA:

"{title}"


El título debe ser:

- grande
- brush
- graffiti automotriz
- deportivo
- premium
- muy legible


PRECIO EXACTO:

"{price}"


NO CAMBIAR EL PRECIO.

NO ESCRIBIR:

- PRECIO DISPONIBLE
- CONSULTA PRECIO
- PREGUNTA PRECIO


PRECIO:

CENTRADO
ABAJO DE LA MOTOCICLETA

EN UN RECUADRO PEQUEÑO,
COMPACTO Y DEPORTIVO.


SUPERBIKERS SHOP:

HASTA ABAJO.

PEQUEÑO.

TIPOGRAFIA BRUSH,
GRAFFITI Y AUTOMOTRIZ.


{flag_instruction}


ORDEN VISUAL:

1. TITULO GRANDE ARRIBA
2. MOTOCICLETA
3. PRECIO PEQUEÑO ABAJO
4. SUPERBIKERS SHOP


NO AGREGAR:

- teléfonos
- direcciones
- vendedores
- hashtags
- facturas
- pedimentos
- marcas de agua nuevas
""".strip()


# =========================================================
# OPENAI PORTADA
# =========================================================

def create_cover_with_openai(
    sender,
    title,
    price,
    flags,
    cover_path
):

    if not openai_client:

        logger.error(
            "Falta OPENAI_API_KEY"
        )

        return None


    if (
        not cover_path
        or
        not Path(cover_path).exists()
    ):

        logger.error(
            "No existe foto base"
        )

        return None


    prompt = build_superbikers_prompt(
        title,
        price,
        flags
    )


    logger.info(
        "=============== OPENAI PORTADA ==============="
    )

    logger.info(
        "Titulo: %s",
        title
    )

    logger.info(
        "Precio EXACTO: %s",
        price
    )


    try:

        with open(
            cover_path,
            "rb"
        ) as image_file:

            result = (
                openai_client
                .images
                .edit(
                    model=
                        OPENAI_IMAGE_MODEL,

                    image=
                        image_file,

                    prompt=
                        prompt,

                    size=
                        "1024x1536",

                    quality=
                        "high"
                )
            )

    except Exception as error:

        logger.exception(
            "ERROR GENERANDO PORTADA: %s",
            error
        )

        return None


    if (
        not result.data
        or
        not result.data[0].b64_json
    ):

        logger.error(
            "OpenAI no devolvio imagen"
        )

        return None


    image_bytes = base64.b64decode(
        result.data[0].b64_json
    )


    output_folder = (
        sender_folder(sender)
        /
        "generated"
    )

    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )


    base_name = safe_filename(
        title
    )


    # PNG WhatsApp
    png_filename = (
        f"{base_name}_openai.png"
    )

    png_path = (
        output_folder
        /
        png_filename
    )

    png_path.write_bytes(
        image_bytes
    )


    # JPG redes
    jpg_filename = (
        f"{base_name}_instagram.jpg"
    )

    jpg_path = (
        output_folder
        /
        jpg_filename
    )


    try:

        with Image.open(
            png_path
        ) as img:

            img = ImageOps.exif_transpose(
                img
            )

            img = img.convert(
                "RGB"
            )

            img.save(
                jpg_path,
                "JPEG",
                quality=95,
                optimize=True
            )

    except Exception as error:

        logger.exception(
            "ERROR CREANDO JPG: %s",
            error
        )

        return None


    public_id = sender_key(
        sender
    )


    png_url = (
        f"{BASE_URL}"
        f"/generated/"
        f"{public_id}/"
        f"{png_filename}"
    )


    jpg_url = (
        f"{BASE_URL}"
        f"/generated/"
        f"{public_id}/"
        f"{jpg_filename}"
    )


    logger.info(
        "PORTADA OPENAI CREADA"
    )

    logger.info(
        "PORTADA OPENAI URL: %s",
        png_url
    )

    logger.info(
        "JPG REDES URL: %s",
        jpg_url
    )


    return {
        "file_path":
            str(png_path),

        "url":
            png_url,

        "social_file_path":
            str(jpg_path),

        "social_url":
            jpg_url,

        "public_originals":
            []
    }


# =========================================================
# FACEBOOK — 10 FOTOS EN UN SOLO POST
# =========================================================

def publish_facebook_carousel(
    image_urls,
    message
):

    if not (
        FACEBOOK_PAGE_ID
        and
        META_PAGE_ACCESS_TOKEN
    ):

        raise RuntimeError(
            "Faltan variables de Facebook"
        )


    if len(image_urls) != FINAL_SOCIAL_PHOTO_COUNT:

        raise RuntimeError(
            f"Facebook esperaba 10 fotos y recibió {len(image_urls)}"
        )


    photo_ids = []


    # Primero cargar las 10 fotos SIN publicar
    for index, image_url in enumerate(
        image_urls,
        start=1
    ):

        endpoint = (
            f"https://graph.facebook.com/"
            f"{GRAPH_API_VERSION}/"
            f"{FACEBOOK_PAGE_ID}/photos"
        )

        data = {
            "url":
                image_url,

            "published":
                "false",

            "access_token":
                META_PAGE_ACCESS_TOKEN
        }


        response = requests.post(
            endpoint,
            data=data,
            timeout=120
        )


        logger.info(
            "FB FOTO %s/10 STATUS: %s",
            index,
            response.status_code
        )


        if response.status_code >= 400:

            raise RuntimeError(
                f"Facebook foto {index}: "
                f"{response.text[:800]}"
            )


        photo_id = (
            response.json().get("id")
        )


        if not photo_id:

            raise RuntimeError(
                f"Facebook no devolvió ID para foto {index}"
            )


        photo_ids.append(
            photo_id
        )


    # Crear un SOLO post con las 10
    feed_endpoint = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{FACEBOOK_PAGE_ID}/feed"
    )


    data = {
        "message":
            message,

        "access_token":
            META_PAGE_ACCESS_TOKEN
    }


    for index, photo_id in enumerate(
        photo_ids
    ):

        data[
            f"attached_media[{index}]"
        ] = json.dumps({
            "media_fbid":
                photo_id
        })


    response = requests.post(
        feed_endpoint,
        data=data,
        timeout=120
    )


    logger.info(
        "FACEBOOK POST STATUS: %s",
        response.status_code
    )


    if response.status_code >= 400:

        raise RuntimeError(
            f"Facebook post: "
            f"{response.text[:1000]}"
        )


    post_id = (
        response.json().get("id")
    )


    logger.info(
        "FACEBOOK PUBLICADO ✅ %s",
        post_id
    )


    return post_id


# =========================================================
# INSTAGRAM STATUS
# =========================================================

def wait_instagram_container(
    container_id,
    timeout_seconds=60
):

    endpoint = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{container_id}"
    )


    deadline = (
        time.time()
        +
        timeout_seconds
    )


    while time.time() < deadline:

        response = requests.get(
            endpoint,
            params={
                "fields":
                    "status_code,status",

                "access_token":
                    META_PAGE_ACCESS_TOKEN
            },
            timeout=30
        )


        if response.status_code >= 400:

            raise RuntimeError(
                f"Instagram status: "
                f"{response.text[:800]}"
            )


        payload = response.json()

        status_code = payload.get(
            "status_code"
        )


        logger.info(
            "IG CONTAINER %s: %s",
            container_id,
            status_code
        )


        if status_code in (
            "FINISHED",
            "PUBLISHED"
        ):

            return True


        if status_code in (
            "ERROR",
            "EXPIRED"
        ):

            raise RuntimeError(
                f"Instagram container error: "
                f"{payload}"
            )


        time.sleep(2)


    raise RuntimeError(
        "Instagram tardó demasiado procesando el contenido"
    )


# =========================================================
# INSTAGRAM — CARRUSEL DE 10
# =========================================================

def publish_instagram_carousel(
    image_urls,
    caption
):

    if not (
        INSTAGRAM_USER_ID
        and
        META_PAGE_ACCESS_TOKEN
    ):

        raise RuntimeError(
            "Faltan variables de Instagram"
        )


    if len(image_urls) != FINAL_SOCIAL_PHOTO_COUNT:

        raise RuntimeError(
            f"Instagram esperaba 10 fotos y recibió {len(image_urls)}"
        )


    child_ids = []


    # Crear 10 hijos
    for index, image_url in enumerate(
        image_urls,
        start=1
    ):

        endpoint = (
            f"https://graph.facebook.com/"
            f"{GRAPH_API_VERSION}/"
            f"{INSTAGRAM_USER_ID}/media"
        )


        response = requests.post(
            endpoint,
            data={
                "image_url":
                    image_url,

                "is_carousel_item":
                    "true",

                "access_token":
                    META_PAGE_ACCESS_TOKEN
            },
            timeout=120
        )


        logger.info(
            "IG ITEM %s/10 STATUS: %s",
            index,
            response.status_code
        )


        if response.status_code >= 400:

            raise RuntimeError(
                f"Instagram foto {index}: "
                f"{response.text[:1000]}"
            )


        container_id = (
            response.json().get("id")
        )


        if not container_id:

            raise RuntimeError(
                f"Instagram no devolvió ID para foto {index}"
            )


        child_ids.append(
            container_id
        )


    # Crear contenedor padre
    parent_endpoint = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{INSTAGRAM_USER_ID}/media"
    )


    parent_response = requests.post(
        parent_endpoint,
        data={
            "media_type":
                "CAROUSEL",

            "children":
                ",".join(child_ids),

            "caption":
                caption,

            "access_token":
                META_PAGE_ACCESS_TOKEN
        },
        timeout=120
    )


    logger.info(
        "IG CAROUSEL STATUS: %s",
        parent_response.status_code
    )


    if parent_response.status_code >= 400:

        raise RuntimeError(
            f"Instagram carrusel: "
            f"{parent_response.text[:1000]}"
        )


    carousel_id = (
        parent_response.json().get("id")
    )


    if not carousel_id:

        raise RuntimeError(
            "Instagram no devolvió ID del carrusel"
        )


    # Esperar a que Meta termine de procesarlo
    wait_instagram_container(
        carousel_id,
        timeout_seconds=90
    )


    # Publicar
    publish_endpoint = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{INSTAGRAM_USER_ID}/media_publish"
    )


    publish_response = requests.post(
        publish_endpoint,
        data={
            "creation_id":
                carousel_id,

            "access_token":
                META_PAGE_ACCESS_TOKEN
        },
        timeout=120
    )


    logger.info(
        "IG PUBLISH STATUS: %s",
        publish_response.status_code
    )


    if publish_response.status_code >= 400:

        raise RuntimeError(
            f"Instagram publicación: "
            f"{publish_response.text[:1000]}"
        )


    media_id = (
        publish_response.json().get("id")
    )


    logger.info(
        "INSTAGRAM PUBLICADO ✅ %s",
        media_id
    )


    return media_id


# =========================================================
# GENERAR PORTADA
# =========================================================

def maybe_start_processing(
    sender
):

    with state_lock:

        session = pending_motos.get(
            sender
        )


        if not session:
            return False


        if session.get("processing"):
            return False


        if session.get("ready_to_publish"):
            return False


        if (
            len(session["photos"])
            != ORIGINAL_PHOTO_COUNT
        ):

            return False


        if not session["text"]:
            return False


        title = extract_cover_title(
            session["text"]
        )


        price = (
            session.get("price_override")
            or
            extract_price(
                session["text"]
            )
        )


        phone_number_id = (
            session.get("phone_number_id")
            or
            PHONE_NUMBER_ID
        )


        if not price:

            session[
                "waiting_for_price"
            ] = True


            if not session.get(
                "price_notice_sent"
            ):

                session[
                    "price_notice_sent"
                ] = True


                threading.Thread(
                    target=
                        send_whatsapp_text,

                    args=(
                        sender,
                        phone_number_id,
                        "⚠️ No detecté el precio.\n\n"
                        "Tus 9 fotos están guardadas.\n\n"
                        "Envíame solamente el precio.\n"
                        "Ejemplo: $349,900"
                    ),

                    daemon=True
                ).start()


            logger.error(
                "PRECIO NO DETECTADO"
            )

            return False


        snapshot = {
            "title":
                title,

            "price":
                price,

            "flags":
                extract_flags(
                    session["text"]
                ),

            "cover_path":
                session["photos"][0][
                    "file_path"
                ],

            "phone_number_id":
                phone_number_id,

            "photos":
                list(
                    session["photos"]
                )
        }


        session[
            "processing"
        ] = True


    threading.Thread(
        target=
            process_moto_background,

        args=(
            sender,
            snapshot
        ),

        daemon=True
    ).start()


    logger.info(
        "GENERACION INICIADA EN SEGUNDO PLANO"
    )


    return True


# =========================================================
# PROCESAR PORTADA BACKGROUND
# =========================================================

def process_moto_background(
    sender,
    snapshot
):

    try:

        title = snapshot["title"]
        price = snapshot["price"]
        flags = snapshot["flags"]
        cover_path = snapshot["cover_path"]
        phone_number_id = snapshot["phone_number_id"]
        photos = snapshot["photos"]


        generated_cover = (
            create_cover_with_openai(
                sender,
                title,
                price,
                flags,
                cover_path
            )
        )


        if not generated_cover:

            raise RuntimeError(
                "No se pudo generar portada"
            )


        public_originals = (
            prepare_public_originals(
                sender,
                photos
            )
        )


        if not public_originals:

            raise RuntimeError(
                "No se pudieron preparar originales"
            )


        generated_cover[
            "public_originals"
        ] = public_originals


        with state_lock:

            session = pending_motos.get(
                sender
            )

            if not session:
                return

            session[
                "generated_cover"
            ] = generated_cover

            session[
                "processing"
            ] = False

            session[
                "ready_to_publish"
            ] = True


        sent = send_cover_to_whatsapp(
            sender,
            phone_number_id,
            generated_cover[
                "file_path"
            ],
            title,
            price
        )


        if sent:

            logger.info(
                "PORTADA LISTA PARA APROBACION ✅"
            )


        else:

            logger.error(
                "Portada creada pero no enviada por WhatsApp"
            )


    except Exception as error:

        logger.exception(
            "ERROR PROCESANDO MOTO: %s",
            error
        )


        with state_lock:

            session = pending_motos.get(
                sender
            )

            if session:

                session[
                    "processing"
                ] = False


        send_whatsapp_text(
            sender,
            snapshot.get(
                "phone_number_id",
                PHONE_NUMBER_ID
            ),
            "⚠️ Hubo un error generando la portada.\n"
            "Envía REINTENTAR."
        )


# =========================================================
# PUBLICAR BACKGROUND
# =========================================================

def publish_background(
    sender
):

    with state_lock:

        session = pending_motos.get(
            sender
        )

        if not session:
            return

        phone_number_id = (
            session.get("phone_number_id")
            or
            PHONE_NUMBER_ID
        )

        text = session.get(
            "text",
            ""
        )

        generated_cover = session.get(
            "generated_cover"
        )

        facebook_existing = session.get(
            "facebook_post_id"
        )

        instagram_existing = session.get(
            "instagram_post_id"
        )


    try:

        if not generated_cover:

            raise RuntimeError(
                "No existe portada generada"
            )


        originals = (
            generated_cover.get(
                "public_originals",
                []
            )
        )


        if len(originals) != ORIGINAL_PHOTO_COUNT:

            raise RuntimeError(
                "No están disponibles las 9 originales"
            )


        # ORDEN FINAL:
        # 1 PORTADA + 9 ORIGINALES

        image_urls = [
            generated_cover[
                "social_url"
            ]
        ]

        image_urls.extend(
            item["url"]
            for item in originals
        )


        logger.info(
            "TOTAL IMAGENES PARA REDES: %s",
            len(image_urls)
        )


        if len(image_urls) != 10:

            raise RuntimeError(
                "La publicación no contiene exactamente 10 imágenes"
            )


        facebook_id = (
            facebook_existing
        )

        instagram_id = (
            instagram_existing
        )


        # Facebook
        if not facebook_id:

            facebook_id = (
                publish_facebook_carousel(
                    image_urls,
                    text
                )
            )

            with state_lock:

                if sender in pending_motos:

                    pending_motos[
                        sender
                    ][
                        "facebook_post_id"
                    ] = facebook_id


        # Instagram
        if not instagram_id:

            instagram_id = (
                publish_instagram_carousel(
                    image_urls,
                    text
                )
            )

            with state_lock:

                if sender in pending_motos:

                    pending_motos[
                        sender
                    ][
                        "instagram_post_id"
                    ] = instagram_id


        send_whatsapp_text(
            sender,
            phone_number_id,
            "✅ PUBLICACIÓN COMPLETADA\n\n"
            "Facebook ✅\n"
            "Instagram ✅\n\n"
            "10 imágenes publicadas:\n"
            "1 portada + 9 originales."
        )


        logger.info(
            "PUBLICACION COMPLETA FB + IG ✅"
        )


        # Ya puede entrar otra moto
        with state_lock:

            pending_motos[
                sender
            ] = new_session()

            pending_motos[
                sender
            ][
                "phone_number_id"
            ] = phone_number_id


    except Exception as error:

        logger.exception(
            "ERROR PUBLICANDO: %s",
            error
        )


        with state_lock:

            session = pending_motos.get(
                sender
            )

            if session:

                session[
                    "publishing"
                ] = False


        send_whatsapp_text(
            sender,
            phone_number_id,
            "⚠️ Hubo un error publicando.\n\n"
            "La moto quedó guardada.\n"
            "Puedes volver a enviar PUBLICAR.\n\n"
            "Revisa Render → Logs para ver el detalle."
        )


# =========================================================
# INICIAR PUBLICACION
# =========================================================

def start_publish(
    sender
):

    with state_lock:

        session = pending_motos.get(
            sender
        )


        if not session:

            return (
                False,
                "No hay una moto lista."
            )


        if not session.get(
            "ready_to_publish"
        ):

            return (
                False,
                "Todavía no hay una portada lista para publicar."
            )


        if session.get(
            "publishing"
        ):

            return (
                False,
                "La publicación ya está en proceso."
            )


        session[
            "publishing"
        ] = True


    threading.Thread(
        target=
            publish_background,

        args=(
            sender,
        ),

        daemon=True
    ).start()


    return (
        True,
        "Publicando..."
    )


# =========================================================
# HOME
# =========================================================

@app.get("/")
def home():

    return jsonify({
        "status": "ok",
        "service": "Superbikers Automatizacion",
        "original_photos": 9,
        "social_photos": 10
    }), 200


# =========================================================
# PRIVACY
# =========================================================

@app.get("/privacy")
def privacy():

    return """
    <html>
    <body style="font-family:Arial;max-width:800px;margin:40px auto;">
        <h1>Política de Privacidad - Superbikers Shop</h1>
        <p>
            La información recibida se utiliza para
            generar y publicar contenido relacionado
            con motocicletas.
        </p>
        <p>No vendemos información personal.</p>
    </body>
    </html>
    """, 200


# =========================================================
# ARCHIVOS PUBLICOS
# =========================================================

@app.get(
    "/generated/<public_id>/<filename>"
)
def generated(
    public_id,
    filename
):

    folder = (
        Path("/tmp/superbikers")
        /
        public_id
        /
        "generated"
    )

    return send_from_directory(
        folder,
        filename
    )


# =========================================================
# WEBHOOK VERIFY
# =========================================================

@app.get("/webhook/whatsapp")
def verify_webhook():

    mode = request.args.get(
        "hub.mode"
    )

    token = request.args.get(
        "hub.verify_token"
    )

    challenge = request.args.get(
        "hub.challenge"
    )

    if (
        mode == "subscribe"
        and
        token == VERIFY_TOKEN
        and
        challenge
    ):

        return challenge, 200

    return "Forbidden", 403


# =========================================================
# WEBHOOK WHATSAPP
# =========================================================

@app.post("/webhook/whatsapp")
def webhook():

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )


    logger.info(
        "WEBHOOK RECIBIDO"
    )


    for entry in payload.get(
        "entry",
        []
    ):

        for change in entry.get(
            "changes",
            []
        ):

            value = change.get(
                "value",
                {}
            )

            metadata = value.get(
                "metadata",
                {}
            )

            incoming_phone_number_id = (
                metadata.get(
                    "phone_number_id"
                )
                or
                PHONE_NUMBER_ID
            )


            for message in value.get(
                "messages",
                []
            ):

                sender = message.get(
                    "from"
                )

                if not sender:
                    continue


                message_id = message.get(
                    "id"
                )


                if message_id:

                    with state_lock:

                        if (
                            message_id
                            in seen_message_ids
                        ):

                            continue


                        seen_message_ids.add(
                            message_id
                        )


                        if len(
                            seen_message_ids
                        ) > 5000:

                            seen_message_ids.clear()

                            seen_message_ids.add(
                                message_id
                            )


                with state_lock:

                    session = (
                        pending_motos
                        .setdefault(
                            sender,
                            new_session()
                        )
                    )

                    session[
                        "phone_number_id"
                    ] = incoming_phone_number_id


                message_type = (
                    message.get(
                        "type"
                    )
                )


                # =================================================
                # IMAGEN
                # =================================================

                if message_type == "image":

                    image = message.get(
                        "image",
                        {}
                    )

                    media_id = image.get(
                        "id"
                    )

                    direct_url = image.get(
                        "url"
                    )

                    mime_type = image.get(
                        "mime_type",
                        "image/jpeg"
                    )

                    caption = normalize_text(
                        image.get(
                            "caption",
                            ""
                        )
                    )


                    with state_lock:

                        session = pending_motos[
                            sender
                        ]

                        if (
                            session.get(
                                "processing"
                            )
                            or
                            session.get(
                                "ready_to_publish"
                            )
                        ):

                            continue

                        current_count = len(
                            session["photos"]
                        )


                    if current_count >= ORIGINAL_PHOTO_COUNT:

                        send_whatsapp_text(
                            sender,
                            incoming_phone_number_id,
                            "Ya tengo las 9 fotos.\n"
                            "Espera la portada."
                        )

                        continue


                    photo_number = (
                        current_count
                        +
                        1
                    )


                    file_path = (
                        download_whatsapp_image(
                            media_id=
                                media_id,

                            sender=
                                sender,

                            photo_number=
                                photo_number,

                            direct_url=
                                direct_url,

                            mime_type=
                                mime_type
                        )
                    )


                    if not file_path:

                        send_whatsapp_text(
                            sender,
                            incoming_phone_number_id,
                            f"⚠️ No pude descargar la foto {photo_number}.\n"
                            f"Reenvía esa foto."
                        )

                        continue


                    with state_lock:

                        session = pending_motos[
                            sender
                        ]

                        session[
                            "photos"
                        ].append({
                            "number":
                                photo_number,

                            "media_id":
                                media_id,

                            "file_path":
                                file_path
                        })


                        if (
                            caption
                            and
                            not session["text"]
                        ):

                            session[
                                "text"
                            ] = caption


                    logger.info(
                        "FOTO %s/9 REGISTRADA",
                        photo_number
                    )


                    if photo_number == 1:

                        logger.info(
                            "FOTO #1 = BASE DE PORTADA"
                        )


                    if photo_number == 9:

                        logger.info(
                            "9 FOTOS COMPLETAS"
                        )


                # =================================================
                # TEXTO
                # =================================================

                elif message_type == "text":

                    text = normalize_text(
                        message.get(
                            "text",
                            {}
                        ).get(
                            "body",
                            ""
                        )
                    )

                    command = (
                        text.strip().upper()
                    )


                    # RESET
                    if command == "RESET":

                        with state_lock:

                            pending_motos[
                                sender
                            ] = new_session()

                            pending_motos[
                                sender
                            ][
                                "phone_number_id"
                            ] = incoming_phone_number_id


                        send_whatsapp_text(
                            sender,
                            incoming_phone_number_id,
                            "✅ Sesión reiniciada.\n"
                            "Manda las 9 fotos de la siguiente moto."
                        )

                        continue


                    # PUBLICAR
                    if command == "PUBLICAR":

                        ok, status = start_publish(
                            sender
                        )

                        send_whatsapp_text(
                            sender,
                            incoming_phone_number_id,
                            (
                                "🚀 Publicando en Facebook e Instagram..."
                                if ok
                                else
                                f"⚠️ {status}"
                            )
                        )

                        continue


                    # REINTENTAR PORTADA
                    if command == "REINTENTAR":

                        with state_lock:

                            session = pending_motos.get(
                                sender
                            )

                            if session:

                                session[
                                    "processing"
                                ] = False

                                session[
                                    "ready_to_publish"
                                ] = False


                        maybe_start_processing(
                            sender
                        )

                        continue


                    with state_lock:

                        session = pending_motos[
                            sender
                        ]


                        # Si ya hay portada lista no alterar información
                        if session.get(
                            "ready_to_publish"
                        ):

                            send_whatsapp_text(
                                sender,
                                incoming_phone_number_id,
                                "La portada ya está lista.\n\n"
                                "Escribe PUBLICAR para subirla\n"
                                "o RESET para cancelar."
                            )

                            continue


                    detected_price = extract_price(
                        text
                    )


                    with state_lock:

                        session = pending_motos[
                            sender
                        ]


                        if (
                            session.get(
                                "waiting_for_price"
                            )
                            and
                            detected_price
                        ):

                            session[
                                "price_override"
                            ] = detected_price

                            session[
                                "waiting_for_price"
                            ] = False

                            session[
                                "price_notice_sent"
                            ] = False

                            logger.info(
                                "PRECIO RECIBIDO: %s",
                                detected_price
                            )


                        elif (
                            is_price_only_message(
                                text
                            )
                            and
                            session.get("text")
                        ):

                            session[
                                "price_override"
                            ] = detected_price


                        else:

                            session[
                                "text"
                            ] = text

                            if detected_price:

                                session[
                                    "price_override"
                                ] = detected_price


                    logger.info(
                        "TEXTO RECIBIDO"
                    )


                # =================================================
                # INTENTAR GENERAR
                # =================================================

                maybe_start_processing(
                    sender
                )


    return jsonify({
        "success": True
    }), 200


# =========================================================
# START
# =========================================================

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
