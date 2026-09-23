import os
import re
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


# =========================================================
# APP
# =========================================================

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers")


# =========================================================
# VARIABLES DE ENTORNO
# =========================================================

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


openai_client = (
    OpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_API_KEY
    else None
)


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
        "waiting_for_price": False,
        "price_notice_sent": False,
        "phone_number_id": "",
        "generated_cover": None
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

    # rango razonable para precio de motocicleta
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

    # -----------------------------------------
    # PRIORIDAD 1:
    # SEGUNDA LINEA
    # -----------------------------------------

    if len(lines) >= 2:

        second = lines[1]

        match = re.search(
            r'(?i)^\s*'
            r'(?:precio\s*:?\s*)?'
            r'\$?\s*'
            r'(\d{2,3}(?:[,\.\s]\d{3})+|\d{5,7})'
            r'\s*(?:mxn|pesos?)?'
            r'\s*$',
            second
        )

        if match:

            price = price_from_candidate(
                match.group(1)
            )

            if price:
                return price


    # -----------------------------------------
    # PRIORIDAD 2:
    # NUMERO CON SIGNO $
    # -----------------------------------------

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


    # -----------------------------------------
    # PRIORIDAD 3:
    # PALABRA PRECIO
    # -----------------------------------------

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
# FLAGS / ETIQUETAS
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
# EXTENSION
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
# WHATSAPP - TEXTO
# =========================================================

def send_whatsapp_text(
    recipient,
    phone_number_id,
    message
):

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
            "Falta token o PHONE_NUMBER_ID para enviar mensaje"
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
            "Error enviando texto a WhatsApp: %s",
            error
        )

        return False


# =========================================================
# WHATSAPP - SUBIR PORTADA
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


        result = response.json()

        return result.get(
            "id"
        )


    except Exception as error:

        logger.exception(
            "Error subiendo portada a WhatsApp: %s",
            error
        )

        return None


# =========================================================
# WHATSAPP - ENVIAR PORTADA
# =========================================================

def send_cover_to_whatsapp(
    recipient,
    phone_number_id,
    file_path,
    title,
    price
):

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
        f"{price}\n"
        f"Superbikers Shop"
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
# DESCARGAR FOTO WHATSAPP
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
                "Descarga directa intento %s: HTTP %s",
                attempt + 1,
                response.status_code
            )

        except requests.RequestException as error:

            logger.warning(
                "Descarga directa intento %s fallo: %s",
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


    # =========================================
    # INTENTO 1:
    # URL QUE YA MANDA META
    # =========================================

    if direct_url:

        image_bytes = try_download_url(
            direct_url,
            headers
        )


    # =========================================
    # INTENTO 2:
    # PEDIR URL POR MEDIA ID
    # =========================================

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
                    media_info.get(
                        "url"
                    )
                )


                mime_type = (
                    media_info.get(
                        "mime_type"
                    )
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


    # =========================================
    # SOLO GUARDAR SI DESCARGO
    # =========================================

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
        "FOTO %s/10 DESCARGADA CORRECTAMENTE",
        photo_number
    )


    return str(
        filepath
    )


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
Agregar una etiqueta pequena con el texto:

"{flags[0]}"

Debe ser discreta y estar integrada
cerca del titulo.
"""


    return f"""
EDITAR LA FOTOGRAFIA PROPORCIONADA
PARA CREAR UNA PORTADA PUBLICITARIA
PREMIUM DE SUPERBIKERS SHOP.

NO CREAR UNA MOTOCICLETA DIFERENTE.

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
- proporciones
- piezas mecanicas

Mejorar solamente:

- iluminacion
- contraste
- claridad
- nitidez
- profundidad
- sombras suaves

Debe seguir pareciendo
una fotografia real.


==================================================

TITULO:

ESCRIBIR EXACTAMENTE:

"{title}"


Debe estar:

- GRANDE
- en la parte superior
- brush / graffiti automotriz
- agresivo pero limpio
- premium
- claramente legible

El titulo debe ser
el texto mas grande de la portada.


==================================================

MOTOCICLETA:

Debe ser la protagonista.

Debe permanecer:

- grande
- visible
- completa
- realista

No tapar partes importantes
con texto.


==================================================

PRECIO:

ESCRIBIR EXACTAMENTE:

"{price}"


REGLA MUY IMPORTANTE:

NO cambiar el precio.

NO escribir:

- PRECIO DISPONIBLE
- CONSULTA PRECIO
- PREGUNTA PRECIO
- ninguna frase sustituta


El recuadro de precio debe estar:

DEBAJO DE LA MOTOCICLETA.


Debe ser:

- pequeno
- compacto
- centrado
- deportivo
- premium


Debe ser claramente
MAS PEQUENO que el titulo.


==================================================

SUPERBIKERS SHOP:

Colocar exactamente:

"Superbikers Shop"

Hasta abajo.

Pequeno.

Tipografia:

- brush
- graffiti
- exotica
- automotriz


==================================================

{flag_instruction}


USAR DETALLES GRAFICOS
INSPIRADOS EN LOS COLORES
DE LA MOTOCICLETA.

Se permiten:

- pincelazos
- detalles de esquinas
- trazos dinamicos

NO sobrecargar.


==================================================

ORDEN VISUAL OBLIGATORIO:


TITULO GRANDE ARRIBA

↓

MOTOCICLETA

↓

PRECIO PEQUENO
DEBAJO DE LA MOTO

↓

SUPERBIKERS SHOP
HASTA ABAJO


NO CAMBIAR ESTE ORDEN.


NO AGREGAR:

- telefonos
- direcciones
- hashtags
- vendedores
- pedimento
- factura
- millas
- textos adicionales
- marcas de agua nuevas


Resultado:

Publicidad profesional
de motocicletas deportivas
para Superbikers Shop.
""".strip()


# =========================================================
# OPENAI
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
        not Path(
            cover_path
        ).exists()
    ):

        logger.error(
            "No existe la foto de portada"
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
                        "gpt-image-2.5-sunburst",

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


    try:

        image_bytes = base64.b64decode(
            result.data[0].b64_json
        )

    except Exception as error:

        logger.exception(
            "Error decodificando portada: %s",
            error
        )

        return None


    public_id = sender_key(
        sender
    )


    output_folder = (
        sender_folder(
            sender
        )
        /
        "generated"
    )


    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )


    filename = (
        safe_filename(
            title
        )
        +
        "_openai.png"
    )


    output_path = (
        output_folder
        /
        filename
    )


    output_path.write_bytes(
        image_bytes
    )


    url = (
        f"{BASE_URL}"
        f"/generated/"
        f"{public_id}/"
        f"{filename}"
    )


    logger.info(
        "PORTADA OPENAI CREADA"
    )

    logger.info(
        "PORTADA OPENAI URL: %s",
        url
    )


    return {
        "file_path":
            str(output_path),

        "url":
            url,

        "filename":
            filename
    }


# =========================================================
# INICIAR PROCESAMIENTO
# =========================================================

def maybe_start_processing(
    sender
):

    phone_number_id = ""


    with state_lock:

        session = pending_motos.get(
            sender
        )


        if not session:

            return False


        phone_number_id = (
            session.get(
                "phone_number_id"
            )
            or
            PHONE_NUMBER_ID
        )


        if session.get(
            "processing"
        ):

            return False


        if (
            len(
                session[
                    "photos"
                ]
            )
            != 10
        ):

            return False


        if not session[
            "text"
        ]:

            return False


        # -------------------------------------
        # TITULO
        # -------------------------------------

        title = extract_cover_title(
            session["text"]
        )


        # -------------------------------------
        # PRECIO
        # -------------------------------------

        price = (
            session.get(
                "price_override"
            )
            or
            extract_price(
                session["text"]
            )
        )


        # =====================================
        # SI NO HAY PRECIO:
        # NO GENERAR
        # =====================================

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
                        "No voy a generar la portada con un precio inventado.\n\n"
                        "Envíame SOLO el precio, por ejemplo:\n"
                        "$349,900\n\n"
                        "Conservaré tus 10 fotos."
                    ),

                    daemon=True
                ).start()


            logger.error(
                "PRECIO NO DETECTADO"
            )

            return False


        # -------------------------------------
        # SNAPSHOT
        # -------------------------------------

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
                phone_number_id
        }


        session[
            "processing"
        ] = True


    # =========================================
    # SEGUNDO PLANO
    # =========================================

    thread = threading.Thread(
        target=
            process_moto_background,

        args=(
            sender,
            snapshot
        ),

        daemon=True
    )


    thread.start()


    logger.info(
        "GENERACION DE PORTADA INICIADA EN SEGUNDO PLANO"
    )


    return True


# =========================================================
# BACKGROUND
# =========================================================

def process_moto_background(
    sender,
    snapshot
):

    title = snapshot[
        "title"
    ]

    price = snapshot[
        "price"
    ]

    flags = snapshot[
        "flags"
    ]

    cover_path = snapshot[
        "cover_path"
    ]

    phone_number_id = snapshot[
        "phone_number_id"
    ]


    try:

        # =====================================
        # SI YA TENEMOS PORTADA,
        # SOLO REENVIAR
        # =====================================

        with state_lock:

            current_session = pending_motos.get(
                sender
            )

            existing_cover = (
                current_session.get(
                    "generated_cover"
                )
                if current_session
                else None
            )


        if (
            existing_cover
            and
            Path(
                existing_cover[
                    "file_path"
                ]
            ).exists()
        ):

            generated_cover = existing_cover

            logger.info(
                "REUTILIZANDO PORTADA YA GENERADA"
            )


        else:

            generated_cover = (
                create_cover_with_openai(
                    sender,
                    title,
                    price,
                    flags,
                    cover_path
                )
            )


        # =====================================
        # OPENAI FALLO
        # =====================================

        if not generated_cover:

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
                phone_number_id,
                "⚠️ No pude generar la portada.\n\n"
                "Tus 10 fotos siguen guardadas.\n"
                "Cuando quieras vuelve a enviar:\n"
                "REINTENTAR"
            )


            return


        # =====================================
        # GUARDAR PORTADA
        # =====================================

        with state_lock:

            session = pending_motos.get(
                sender
            )

            if session:

                session[
                    "generated_cover"
                ] = generated_cover


        # =====================================
        # REGRESAR PORTADA A WHATSAPP
        # =====================================

        sent = send_cover_to_whatsapp(
            recipient=
                sender,

            phone_number_id=
                phone_number_id,

            file_path=
                generated_cover[
                    "file_path"
                ],

            title=
                title,

            price=
                price
        )


        # =====================================
        # EXITO
        # =====================================

        if sent:

            logger.info(
                "PORTADA GENERADA Y DEVUELTA A WHATSAPP ✅"
            )


            with state_lock:

                pending_motos[
                    sender
                ] = new_session()


        # =====================================
        # ERROR SOLO EN ENVIO
        # =====================================

        else:

            logger.error(
                "PORTADA GENERADA PERO NO SE PUDO ENVIAR"
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
                phone_number_id,
                "⚠️ La portada sí se generó, "
                "pero WhatsApp no pudo recibirla.\n\n"
                "Envía REINTENTAR y volveré a mandarla "
                "sin volver a generar la imagen."
            )


    except Exception as error:

        logger.exception(
            "Error inesperado: %s",
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


# =========================================================
# HOME
# =========================================================

@app.get("/")
def home():

    return jsonify({
        "status": "ok",
        "service": "Superbikers Automatizacion"
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
            La información recibida se utiliza
            para atender solicitudes y generar
            contenido relacionado con motocicletas.
        </p>
        <p>
            No vendemos información personal.
        </p>
    </body>
    </html>
    """, 200


# =========================================================
# VER PORTADA
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
# META VERIFY
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
# WEBHOOK
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


                # =====================================
                # EVITAR DUPLICADOS
                # =====================================

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


                        if (
                            len(
                                seen_message_ids
                            )
                            > 5000
                        ):

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


                message_type = message.get(
                    "type"
                )


                # =====================================
                # IMAGEN
                # =====================================

                if (
                    message_type
                    ==
                    "image"
                ):


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


                    with state_lock:

                        session = pending_motos[
                            sender
                        ]


                        if session.get(
                            "processing"
                        ):

                            continue


                        current_count = len(
                            session[
                                "photos"
                            ]
                        )


                    if current_count >= 10:

                        send_whatsapp_text(
                            sender,
                            incoming_phone_number_id,
                            "Ya tengo las 10 fotos.\n"
                            "Si quieres empezar otra moto envía RESET."
                        )

                        continue


                    photo_number = (
                        current_count
                        +
                        1
                    )


                    file_path = download_whatsapp_image(
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


                    # =================================
                    # FOTO FALLO
                    # =================================

                    if not file_path:

                        send_whatsapp_text(
                            sender,
                            incoming_phone_number_id,
                            f"⚠️ No pude descargar la foto {photo_number}.\n\n"
                            f"Reenvía ESA foto.\n"
                            f"No la conté dentro de las 10."
                        )

                        continue


                    # =================================
                    # SOLO AHORA LA CONTAMOS
                    # =================================

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


                    logger.info(
                        "FOTO %s/10 REGISTRADA",
                        photo_number
                    )


                    if photo_number == 1:

                        logger.info(
                            "FOTO #1 = PORTADA"
                        )


                    if photo_number == 10:

                        logger.info(
                            "10 FOTOS COMPLETAS"
                        )


                # =====================================
                # TEXTO
                # =====================================

                elif (
                    message_type
                    ==
                    "text"
                ):


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
                        text
                        .strip()
                        .upper()
                    )


                    # ---------------------------------
                    # RESET
                    # ---------------------------------

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
                            "Puedes mandar una moto nueva."
                        )

                        continue


                    # ---------------------------------
                    # REINTENTAR
                    # ---------------------------------

                    if command == "REINTENTAR":

                        with state_lock:

                            session = pending_motos.get(
                                sender
                            )

                            if session:

                                session[
                                    "processing"
                                ] = False


                        maybe_start_processing(
                            sender
                        )

                        continue


                    detected_price = extract_price(
                        text
                    )


                    with state_lock:

                        session = pending_motos[
                            sender
                        ]


                        # =============================
                        # ESTABAMOS ESPERANDO PRECIO
                        # =============================

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


                        # =============================
                        # MENSAJE SOLO PRECIO
                        # =============================

                        elif (
                            is_price_only_message(
                                text
                            )
                            and
                            session.get(
                                "text"
                            )
                        ):

                            session[
                                "price_override"
                            ] = detected_price


                        # =============================
                        # TEXTO COMPLETO
                        # =============================

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


                # =====================================
                # VER SI YA PODEMOS GENERAR
                # =====================================

                maybe_start_processing(
                    sender
                )


    # META RECIBE 200 RAPIDO

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
