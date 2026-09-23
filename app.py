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

GRAPH_API_VERSION = os.environ.get(
    "GRAPH_API_VERSION",
    "v26.0"
)

BASE_URL = os.environ.get(
    "BASE_URL",
    "https://superbikers-whatsapp-webhook-2.onrender.com"
)


# =========================================================
# OPENAI
# =========================================================

openai_client = None

if OPENAI_API_KEY:
    openai_client = OpenAI(
        api_key=OPENAI_API_KEY
    )


# =========================================================
# SESIONES TEMPORALES
# =========================================================

pending_motos = {}


# =========================================================
# HOME
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "ok",
        "service": "Superbikers WhatsApp Automatización"
    }), 200


# =========================================================
# PRIVACIDAD
# =========================================================

@app.route("/privacy", methods=["GET"])
def privacy():

    return """
    <html>
        <head>
            <title>
                Política de Privacidad - Superbikers Shop
            </title>
        </head>

        <body style="
            font-family:Arial;
            max-width:800px;
            margin:40px auto;
            line-height:1.6;
        ">

            <h1>
                Política de Privacidad de Superbikers Shop
            </h1>

            <p>
                Superbikers Shop utiliza la información
                recibida mediante WhatsApp, Facebook e
                Instagram para atender clientes,
                administrar motocicletas y gestionar
                publicaciones.
            </p>

            <p>
                No vendemos ni comercializamos
                información personal.
            </p>

            <p>
                Última actualización:
                septiembre de 2026.
            </p>

        </body>
    </html>
    """, 200


# =========================================================
# MOSTRAR PORTADA GENERADA
# =========================================================

@app.route(
    "/generated/<sender>/<filename>",
    methods=["GET"]
)
def serve_generated(sender, filename):

    folder = (
        Path("/tmp/superbikers")
        / sender
        / "generated"
    )

    return send_from_directory(
        folder,
        filename
    )


# =========================================================
# VERIFICACIÓN WEBHOOK META
# =========================================================

@app.route(
    "/webhook/whatsapp",
    methods=["GET"]
)
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
        and token == VERIFY_TOKEN
        and challenge
    ):

        logger.info(
            "Webhook verificado correctamente por Meta."
        )

        return challenge, 200


    return "Forbidden", 403


# =========================================================
# LIMPIAR TEXTO
# =========================================================

def normalize_text(text):

    if not text:
        return ""

    return (
        text
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


# =========================================================
# SACAR PRECIO
# =========================================================

def extract_price(text):

    if not text:
        return ""

    patterns = [

        r'\$\s?\d{1,3}(?:[,\.\s]\d{3})+',

        r'\$\s?\d+',

        r'(?i)precio\s*(?:de)?\s*\$?\s*'
        r'(\d{1,3}(?:[,\.\s]\d{3})+|\d+)'
    ]


    for pattern in patterns:

        match = re.search(
            pattern,
            text
        )

        if match:

            value = (
                match.group(1)
                if match.lastindex
                else match.group(0)
            )

            digits = re.sub(
                r"[^\d]",
                "",
                value
            )

            if digits:

                return f"${int(digits):,}"


    return ""


# =========================================================
# LIMPIAR TÍTULO
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


# =========================================================
# SACAR TÍTULO
# =========================================================

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

        if re.fullmatch(
            r'[\$\d\.,\s]+',
            line
        ):
            continue


        return clean_cover_title(
            line
        )


    return ""


# =========================================================
# HASHTAGS
# =========================================================

def extract_hashtags(text):

    if not text:
        return []

    return re.findall(
        r'#\w+',
        text
    )


# =========================================================
# FLAGS
# =========================================================

def extract_flags(text):

    if not text:
        return []

    lowered = text.lower()

    possible_flags = [

        (
            "preventa",
            "Preventa"
        ),

        (
            "nacional",
            "Nacional"
        ),

        (
            "nuevo ingreso",
            "Nuevo ingreso"
        ),

        (
            "full system",
            "Full system"
        ),

        (
            "placas de regalo",
            "Placas de regalo"
        )
    ]


    flags = []


    for key, label in possible_flags:

        if key in lowered:

            flags.append(
                label
            )


    return flags


# =========================================================
# EXTENSIÓN IMAGEN
# =========================================================

def get_extension_from_mime(mime_type):

    mapping = {

        "image/jpeg":
            ".jpg",

        "image/jpg":
            ".jpg",

        "image/png":
            ".png",

        "image/webp":
            ".webp"
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
# DESCARGAR FOTO WHATSAPP
# =========================================================

def download_whatsapp_image(
    media_id,
    sender,
    photo_number
):

    if not WHATSAPP_ACCESS_TOKEN:

        logger.error(
            "ERROR: falta WHATSAPP_ACCESS_TOKEN"
        )

        return None


    headers = {

        "Authorization":
            f"Bearer {WHATSAPP_ACCESS_TOKEN}"
    }


    media_info_url = (

        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{media_id}"

    )


    # -----------------------------------------
    # PASO 1
    # Obtener URL temporal
    # -----------------------------------------

    try:

        response = requests.get(
            media_info_url,
            headers=headers,
            timeout=30
        )

    except requests.RequestException as error:

        logger.exception(
            "Error obteniendo URL de media: %s",
            error
        )

        return None


    if response.status_code != 200:

        logger.error(
            "Meta rechazó media_id. "
            "status=%s respuesta=%s",
            response.status_code,
            response.text[:500]
        )

        return None


    media_info = response.json()

    media_url = media_info.get(
        "url"
    )

    mime_type = media_info.get(
        "mime_type",
        "image/jpeg"
    )


    if not media_url:

        logger.error(
            "Meta no devolvió URL."
        )

        return None


    # -----------------------------------------
    # PASO 2
    # Descargar archivo
    # -----------------------------------------

    try:

        image_response = requests.get(
            media_url,
            headers=headers,
            timeout=60
        )

    except requests.RequestException as error:

        logger.exception(
            "Error descargando foto: %s",
            error
        )

        return None


    if image_response.status_code != 200:

        logger.error(
            "Error descargando foto. "
            "status=%s",
            image_response.status_code
        )

        return None


    extension = get_extension_from_mime(
        mime_type
    )


    folder = (

        Path("/tmp/superbikers")
        / sender

    )


    folder.mkdir(
        parents=True,
        exist_ok=True
    )


    filepath = (

        folder
        / f"foto_{photo_number:02d}{extension}"

    )


    filepath.write_bytes(
        image_response.content
    )


    logger.info(

        "FOTO %s/10 DESCARGADA | %s",

        photo_number,

        filepath
    )


    return str(
        filepath
    )


# =========================================================
# SESIÓN
# =========================================================

def get_session(sender):

    if sender not in pending_motos:

        pending_motos[sender] = {

            "photos": [],

            "text": ""
        }


    return pending_motos[
        sender
    ]


# =========================================================
# NOMBRE ARCHIVO SEGURO
# =========================================================

def safe_filename(text):

    if not text:
        return "portada"


    text = re.sub(

        r"[^a-zA-Z0-9_-]+",

        "_",

        text.strip()
    )


    return (
        text[:80]
        or
        "portada"
    )


# =========================================================
# PROMPT SUPERBIKERS
# =========================================================

def build_superbikers_prompt(moto):

    title = (
        moto.get("cover_title")
        or
        "MOTOCICLETA DISPONIBLE"
    )

    price = (
        moto.get("cover_price")
        or
        "PRECIO DISPONIBLE"
    )

    flags = (
        moto.get("flags")
        or
        []
    )


    flag_text = ""


    if flags:

        flag_text = f"""
Agregar una etiqueta pequeña con:
"{flags[0]}"

La etiqueta debe estar bien integrada
y no debe dominar el diseño.
"""


    prompt = f"""
Crear un post publicitario profesional
para Superbikers Shop utilizando
EXACTAMENTE la fotografía proporcionada
como fotografía principal.

MUY IMPORTANTE:

Mantener la motocicleta lo más original
posible.

No cambiar:

- modelo
- color
- carenados
- faros
- escape
- rines
- llantas
- accesorios
- asiento
- tanque
- piezas
- proporciones

No inventar modificaciones.

Mantener también el fondo original
lo más posible.

Solamente mejorar de manera natural:

- iluminación
- contraste
- claridad
- nitidez
- profundidad
- sombras suaves

La motocicleta debe ser la protagonista.

NO convertir la foto en ilustración.
Debe seguir pareciendo una fotografía real.

-------------------------------------

TEXTO EXACTO DE PORTADA:

Título:

"{title}"

Precio:

"{price}"

Marca inferior:

"Superbikers Shop"

{flag_text}

-------------------------------------

DISEÑO SUPERBIKERS SHOP:

Título en la parte superior.

El título debe ser moderno,
automotriz y con personalidad.

Puede tener un toque brush
o graffiti elegante.

IMPORTANTE:

El título NO debe ser demasiado grande.

Debe dejar respirar la imagen
y no tapar la motocicleta.

-------------------------------------

PRECIO:

Colocar "{price}"
en un recuadro pequeño o mediano.

Debe verse:

- limpio
- moderno
- premium
- fácil de leer

No hacer el precio gigantesco.

-------------------------------------

PARTE INFERIOR:

Colocar:

"Superbikers Shop"

pequeño.

Usar un estilo visual:

- brush
- graffiti
- exótico
- automotriz

Debe verse integrado
y no como texto genérico.

-------------------------------------

NO AGREGAR:

- teléfonos
- direcciones
- hashtags
- vendedores
- millas
- pedimento
- factura
- condiciones de venta
- marcas de agua nuevas
- textos adicionales
- logotipos inventados

-------------------------------------

ESTÉTICA FINAL:

- limpia
- moderna
- automotriz
- premium
- deportiva
- elegante
- realista
- alto contraste controlado
- iluminación atractiva
- detalles gráficos mínimos

La imagen debe parecer una publicidad
profesional creada específicamente
para Superbikers Shop.

NO debe parecer una plantilla genérica.

Mantener la fotografía original
claramente reconocible.
"""


    return prompt.strip()


# =========================================================
# CREAR PORTADA CON OPENAI
# =========================================================

def create_cover_with_openai(
    moto,
    sender
):

    if not openai_client:

        logger.error(
            "ERROR: falta OPENAI_API_KEY"
        )

        return None


    cover_path = moto.get(
        "cover_file_path"
    )


    if (
        not cover_path
        or
        not Path(
            cover_path
        ).exists()
    ):

        logger.error(
            "No existe foto de portada."
        )

        return None


    prompt = build_superbikers_prompt(
        moto
    )


    logger.info(
        "=============== OPENAI PORTADA ==============="
    )

    logger.info(
        "Título: %s",
        moto.get(
            "cover_title"
        )
    )

    logger.info(
        "Precio: %s",
        moto.get(
            "cover_price"
        )
    )

    logger.info(
        "Foto base: %s",
        cover_path
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
                        prompt
                )
            )


    except Exception as error:

        logger.exception(

            "ERROR GENERANDO PORTADA OPENAI: %s",

            error
        )

        return None


    if (
        not result.data
        or
        not result.data[0].b64_json
    ):

        logger.error(
            "OpenAI no devolvió imagen."
        )

        return None


    try:

        image_bytes = (
            base64.b64decode(
                result.data[0].b64_json
            )
        )

    except Exception as error:

        logger.exception(
            "Error decodificando imagen: %s",
            error
        )

        return None


    output_folder = (

        Path("/tmp/superbikers")
        / sender
        / "generated"

    )


    output_folder.mkdir(
        parents=True,
        exist_ok=True
    )


    filename = (

        safe_filename(
            moto.get(
                "cover_title"
            )
            or
            "portada"
        )

        +

        "_openai.png"

    )


    output_path = (

        output_folder
        / filename

    )


    output_path.write_bytes(
        image_bytes
    )


    url = (

        f"{BASE_URL}"
        f"/generated/"
        f"{sender}/"
        f"{filename}"

    )


    logger.info(
        "PORTADA OPENAI CREADA"
    )

    logger.info(
        "PORTADA OPENAI URL: %s",
        url
    )

    logger.info(
        "=============================================="
    )


    return {

        "file_path":
            str(output_path),

        "filename":
            filename,

        "url":
            url,

        "prompt":
            prompt
    }


# =========================================================
# FINALIZAR MOTO
# =========================================================

def finalize_moto(sender):

    session = pending_motos.get(
        sender
    )


    if not session:
        return None


    # -----------------------------------------
    # Necesitamos 10 fotos
    # -----------------------------------------

    if len(
        session["photos"]
    ) != 10:

        return None


    # -----------------------------------------
    # Necesitamos texto
    # -----------------------------------------

    if not session["text"]:

        return None


    # -----------------------------------------
    # Verificar archivos
    # -----------------------------------------

    for photo in session["photos"]:

        if not photo.get(
            "file_path"
        ):

            logger.error(
                "Una foto no se descargó."
            )

            return None


    text = session["text"]


    moto = {

        "full_text_original":
            text,

        "cover_title":
            extract_cover_title(
                text
            ),

        "cover_price":
            extract_price(
                text
            ),

        "flags":
            extract_flags(
                text
            ),

        "hashtags":
            extract_hashtags(
                text
            ),

        "total_photos":
            10,

        "cover_media_id":
            session["photos"][0][
                "media_id"
            ],

        "cover_file_path":
            session["photos"][0][
                "file_path"
            ],

        "photos":
            session["photos"],

        "gallery_photos":
            session["photos"][1:]
    }


    logger.info(
        "MOTO COMPLETA DETECTADA"
    )


    # -----------------------------------------
    # OPENAI
    # -----------------------------------------

    generated_cover = (
        create_cover_with_openai(
            moto,
            sender
        )
    )


    # =========================================
    # SI OPENAI FUNCIONÓ
    # =========================================

    if generated_cover:

        moto[
            "generated_cover"
        ] = generated_cover


        logger.info(
            "PORTADA GENERADA CORRECTAMENTE"
        )


        logger.info(
            "\n"
            "================ MOTO FINAL ================\n"
            "%s\n"
            "============================================",
            json.dumps(
                moto,
                ensure_ascii=False,
                indent=2
            )
        )


        # SOLO AQUÍ BORRAMOS
        # LAS 10 FOTOS TEMPORALES DE LA SESIÓN

        pending_motos[
            sender
        ] = {

            "photos": [],

            "text": ""
        }


        return moto


    # =========================================
    # SI OPENAI FALLA
    # =========================================

    logger.error(
        "OPENAI NO GENERÓ LA PORTADA."
    )

    logger.error(
        "LAS 10 FOTOS Y EL TEXTO SE CONSERVAN."
    )

    logger.error(
        "NO NECESITAS REENVIAR TODO."
    )


    return None


# =========================================================
# RECIBIR WHATSAPP
# =========================================================

@app.route(
    "/webhook/whatsapp",
    methods=["POST"]
)
def receive_webhook():

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )


    logger.info(
        "WEBHOOK RECIBIDO"
    )


    completed_motos = []


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


            for message in value.get(
                "messages",
                []
            ):


                sender = message.get(
                    "from"
                )


                if not sender:

                    continue


                session = get_session(
                    sender
                )


                message_type = message.get(
                    "type"
                )


                # =====================================
                # FOTO
                # =====================================

                if message_type == "image":


                    image = message.get(
                        "image",
                        {}
                    )


                    media_id = image.get(
                        "id"
                    )


                    caption = normalize_text(

                        image.get(
                            "caption",
                            ""
                        )

                    )


                    if (
                        media_id
                        and
                        len(
                            session[
                                "photos"
                            ]
                        ) < 10
                    ):


                        photo_number = (

                            len(
                                session[
                                    "photos"
                                ]
                            )

                            +

                            1
                        )


                        file_path = (
                            download_whatsapp_image(

                                media_id,

                                sender,

                                photo_number
                            )
                        )


                        # SOLO GUARDAR SI DESCARGÓ

                        if file_path:


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
                                    "LAS 10 FOTOS YA ESTÁN COMPLETAS"
                                )


                    if (
                        caption
                        and
                        not session["text"]
                    ):

                        session["text"] = caption


                # =====================================
                # TEXTO
                # =====================================

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


                    session[
                        "text"
                    ] = text


                    logger.info(
                        "TEXTO RECIBIDO"
                    )


                # =====================================
                # INTENTAR CREAR MOTO
                # =====================================

                moto = finalize_moto(
                    sender
                )


                if moto:

                    completed_motos.append(
                        moto
                    )


    return jsonify({

        "success":
            True,

        "completed_motos":
            len(
                completed_motos
            )

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

        host=
            "0.0.0.0",

        port=
            port

    )
