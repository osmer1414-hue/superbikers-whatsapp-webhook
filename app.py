import os
import re
import json
import base64
import hashlib
import logging
import mimetypes
import threading
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_from_directory
from openai import OpenAI


app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers")


# =========================================================
# VARIABLES
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
).rstrip("/")


openai_client = (
    OpenAI(api_key=OPENAI_API_KEY)
    if OPENAI_API_KEY
    else None
)


pending_motos = {}
seen_message_ids = set()
state_lock = threading.Lock()


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


def new_session():

    return {
        "photos": [],
        "text": "",
        "processing": False
    }


# =========================================================
# HOME
# =========================================================

@app.get("/")
def home():

    return jsonify({
        "status": "ok",
        "service": "Superbikers WhatsApp Automatizacion"
    }), 200


# =========================================================
# PRIVACIDAD
# =========================================================

@app.get("/privacy")
def privacy():

    return """
    <html>

    <head>

        <title>
            Politica de Privacidad - Superbikers Shop
        </title>

    </head>

    <body style="
        font-family:Arial;
        max-width:800px;
        margin:40px auto;
        line-height:1.6;
    ">

        <h1>
            Politica de Privacidad de Superbikers Shop
        </h1>

        <p>
            Superbikers Shop utiliza la informacion
            recibida mediante WhatsApp, Facebook e
            Instagram para atender clientes,
            administrar motocicletas y gestionar
            publicaciones.
        </p>

        <p>
            No vendemos ni comercializamos
            informacion personal.
        </p>

        <p>
            Ultima actualizacion:
            septiembre de 2026.
        </p>

    </body>

    </html>
    """, 200


# =========================================================
# MOSTRAR PORTADAS
# =========================================================

@app.get("/generated/<public_id>/<filename>")
def serve_generated(public_id, filename):

    folder = (
        Path("/tmp/superbikers")
        / public_id
        / "generated"
    )

    return send_from_directory(
        folder,
        filename
    )


# =========================================================
# VERIFICAR WEBHOOK META
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

        logger.info(
            "Webhook verificado correctamente por Meta"
        )

        return challenge, 200


    return "Forbidden", 403


# =========================================================
# TEXTO
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


        if re.fullmatch(
            r'[\$\d\.,\s]+',
            line
        ):
            continue


        return clean_cover_title(
            line
        )


    return ""


def extract_hashtags(text):

    return re.findall(
        r'#\w+',
        text or ""
    )


def extract_flags(text):

    lowered = (
        text or ""
    ).lower()


    possible = [

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


    return [

        label

        for key, label in possible

        if key in lowered

    ]


# =========================================================
# WHATSAPP FOTO
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

        mapping.get(
            mime_type
        )

        or

        mimetypes.guess_extension(
            mime_type or ""
        )

        or

        ".jpg"

    )


def download_whatsapp_image(
    media_id,
    sender,
    photo_number
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


        info_response.raise_for_status()


        media_info = (
            info_response.json()
        )


        media_url = (
            media_info.get(
                "url"
            )
        )


        mime_type = (
            media_info.get(
                "mime_type",
                "image/jpeg"
            )
        )


        if not media_url:

            logger.error(
                "Meta no devolvio URL"
            )

            return None


        image_response = requests.get(

            media_url,

            headers=headers,

            timeout=60

        )


        image_response.raise_for_status()


    except requests.RequestException as error:

        logger.exception(

            "Error descargando foto de WhatsApp: %s",

            error
        )

        return None


    folder = sender_folder(
        sender
    )


    folder.mkdir(
        parents=True,
        exist_ok=True
    )


    extension = get_extension_from_mime(
        mime_type
    )


    filepath = (

        folder

        /

        f"foto_{photo_number:02d}{extension}"

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
# NOMBRE ARCHIVO
# =========================================================

def safe_filename(text):

    text = re.sub(

        r"[^a-zA-Z0-9_-]+",

        "_",

        (
            text
            or
            "portada"
        ).strip()

    )


    return (
        text[:80]
        or
        "portada"
    )


# =========================================================
# PROMPT DEFINITIVO SUPERBIKERS
# =========================================================

def build_superbikers_prompt(moto):

    title = (

        moto.get(
            "cover_title"
        )

        or

        "MOTOCICLETA DISPONIBLE"

    )


    price = (

        moto.get(
            "cover_price"
        )

        or

        "PRECIO DISPONIBLE"

    )


    flags = (
        moto.get(
            "flags"
        )
        or
        []
    )


    flag_instruction = ""


    if flags:

        flag_instruction = f"""
ETIQUETA OPCIONAL:

Agregar:

"{flags[0]}"

Pequeno y discreto cerca del titulo.

No debe competir con el titulo,
la motocicleta ni el precio.
"""


    return f"""
EDITAR LA FOTOGRAFIA PROPORCIONADA
PARA CREAR UNA PORTADA PUBLICITARIA
PREMIUM DE SUPERBIKERS SHOP.

LA FOTOGRAFIA DE ENTRADA ES LA BASE
PRINCIPAL.

DEBE SEGUIR SIENDO CLARAMENTE
RECONOCIBLE.


==================================================

PRIORIDAD ABSOLUTA:

CONSERVAR LA MOTOCICLETA
Y EL FONDO LO MAS ORIGINAL POSIBLE.

NO cambiar:

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

NO inventar modificaciones.

Mantener el fondo original
tanto como sea posible.

Solamente mejorar naturalmente:

- iluminacion
- contraste
- claridad
- nitidez
- profundidad
- sombras suaves

Debe parecer una fotografia real.

NO convertir la foto
en una ilustracion.


==================================================

JERARQUIA VISUAL OBLIGATORIA:


1. TITULO GRANDE ARRIBA

Texto EXACTO:

"{title}"


El titulo debe:

- estar arriba
- ser GRANDE
- ser claramente mas grande que el precio
- ocupar aproximadamente 15-20%
  de la parte superior
- tener estilo brush / graffiti
  automotriz
- ser agresivo pero limpio
- verse premium
- ser facil de leer

Puede usar:

- blanco
- negro
- un color de acento inspirado
  en la motocicleta

NO tapar partes importantes
de la moto.


==================================================

2. MOTOCICLETA PROTAGONISTA

La motocicleta debe permanecer:

- grande
- completa
- visible
- dominante
- realista

NO colocar textos importantes
encima de:

- tanque
- carenado
- asiento
- ruedas

NO cubrir la motocicleta
con graficos innecesarios.


==================================================

3. PRECIO DEBAJO DE LA MOTO

Texto EXACTO:

"{price}"


MUY IMPORTANTE:

El precio debe estar:

CENTRADO DEBAJO
DE LA MOTOCICLETA.


NO poner el precio:

- arriba
- junto al titulo
- sobre la motocicleta


Usar un recuadro:

- COMPACTO
- PEQUENO
- deportivo
- premium


El recuadro debe ocupar
aproximadamente 30-35%
del ancho total.


Debe ser MUCHO MAS PEQUENO
que el titulo.


Estilo sugerido:

- fondo oscuro
- borde fino
- acento inspirado
  en el color de la motocicleta
- buena legibilidad


El precio debe destacar,
pero debe ser secundario
al titulo.


==================================================

4. SUPERBIKERS SHOP HASTA ABAJO

Texto EXACTO:

"Superbikers Shop"


Debe estar:

- centrado
- hasta abajo
- pequeno
- debajo del precio


Tipografia:

- brush
- graffiti
- exotica
- automotriz


Debe verse integrado
al diseño.

NO como texto generico.


==================================================

{flag_instruction}


ACENTOS GRAFICOS:

Se permiten:

- pincelazos
- trazos dinamicos
- detalles discretos
  en bordes o esquinas

Usar colores inspirados
en la motocicleta
mas negro y blanco.

NO sobrecargar.

Mantener aspecto
de publicidad premium
de motocicletas deportivas.


==================================================

NO AGREGAR:

- telefonos
- direcciones
- hashtags
- vendedores
- millas
- pedimento
- factura
- condiciones de venta
- textos adicionales
- marcas de agua nuevas
- logotipos inventados


==================================================

REGLA FINAL:

LA IMAGEN DEBE LEERSE
VISUALMENTE EN ESTE ORDEN:


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


La composicion debe parecer
una publicidad profesional
creada especificamente
para Superbikers Shop.

NO debe parecer
una plantilla generica.
""".strip()


# =========================================================
# OPENAI
# =========================================================

def create_cover_with_openai(
    moto,
    sender
):

    if not openai_client:

        logger.error(
            "Falta OPENAI_API_KEY"
        )

        return None


    cover_path = (
        moto.get(
            "cover_file_path"
        )
    )


    if (
        not cover_path
        or
        not Path(
            cover_path
        ).exists()
    ):

        logger.error(
            "No existe foto de portada"
        )

        return None


    prompt = build_superbikers_prompt(
        moto
    )


    logger.info(
        "=============== OPENAI PORTADA ==============="
    )


    logger.info(
        "Titulo: %s",
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
                        prompt,

                    size=
                        "1024x1536",

                    quality=
                        "high"

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
            "OpenAI no devolvio imagen"
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
            moto.get(
                "cover_title"
            )
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


    logger.info(
        "=============================================="
    )


    return {

        "file_path":
            str(output_path),

        "filename":
            filename,

        "url":
            url

    }


# =========================================================
# CREAR MOTO
# =========================================================

def build_moto_from_session(
    session
):

    text = session[
        "text"
    ]


    return {

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
            [
                dict(photo)
                for photo
                in session["photos"]
            ],

        "gallery_photos":
            [
                dict(photo)
                for photo
                in session["photos"][1:]
            ]

    }


# =========================================================
# PROCESAR EN SEGUNDO PLANO
# =========================================================

def process_moto_background(
    sender,
    snapshot
):

    try:

        moto = build_moto_from_session(
            snapshot
        )


        generated_cover = (
            create_cover_with_openai(
                moto,
                sender
            )
        )


        if generated_cover:


            moto[
                "generated_cover"
            ] = generated_cover


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


            with state_lock:

                pending_motos[
                    sender
                ] = new_session()


            logger.info(
                "PORTADA GENERADA CORRECTAMENTE"
            )


        else:


            with state_lock:

                current = pending_motos.get(
                    sender
                )


                if current:

                    current[
                        "processing"
                    ] = False


            logger.error(
                "OPENAI NO GENERO LA PORTADA"
            )


            logger.error(
                "LAS 10 FOTOS Y EL TEXTO SE CONSERVAN"
            )


            logger.error(
                "ENVIA REINTENTAR PARA VOLVER A INTENTAR"
            )


    except Exception as error:


        logger.exception(

            "Error inesperado procesando moto: %s",

            error

        )


        with state_lock:

            current = pending_motos.get(
                sender
            )


            if current:

                current[
                    "processing"
                ] = False


# =========================================================
# INICIAR PROCESO
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


        if (
            len(
                session["photos"]
            )
            != 10
        ):

            return False


        if not session[
            "text"
        ]:

            return False


        if session.get(
            "processing"
        ):

            return False


        if any(

            not photo.get(
                "file_path"
            )

            for photo
            in session["photos"]

        ):

            logger.error(
                "Una foto no se descargo correctamente"
            )

            return False


        session[
            "processing"
        ] = True


        snapshot = {

            "photos":
                [
                    dict(photo)
                    for photo
                    in session["photos"]
                ],

            "text":
                session["text"]

        }


    logger.info(
        "MOTO COMPLETA DETECTADA"
    )


    logger.info(
        "GENERANDO PORTADA EN SEGUNDO PLANO"
    )


    thread = threading.Thread(

        target=
            process_moto_background,

        args=
            (
                sender,
                snapshot
            ),

        daemon=
            True

    )


    thread.start()


    return True


# =========================================================
# RECIBIR WHATSAPP
# =========================================================

@app.post("/webhook/whatsapp")
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


                message_id = (
                    message.get(
                        "id"
                    )
                )


                if message_id:


                    with state_lock:


                        if (
                            message_id
                            in seen_message_ids
                        ):

                            logger.info(
                                "MENSAJE DUPLICADO IGNORADO"
                            )

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


                sender = (
                    message.get(
                        "from"
                    )
                )


                if not sender:
                    continue


                with state_lock:


                    session = (
                        pending_motos
                        .setdefault(
                            sender,
                            new_session()
                        )
                    )


                message_type = (
                    message.get(
                        "type"
                    )
                )


                # =====================================
                # FOTO
                # =====================================

                if (
                    message_type
                    ==
                    "image"
                ):


                    with state_lock:


                        if session.get(
                            "processing"
                        ):

                            logger.warning(
                                "PORTADA EN PROCESO"
                            )

                            continue


                        current_count = len(
                            session[
                                "photos"
                            ]
                        )


                    if (
                        current_count
                        >= 10
                    ):

                        logger.warning(
                            "YA HAY 10 FOTOS"
                        )

                        logger.warning(
                            "ENVIA RESET PARA OTRA MOTO"
                        )

                        continue


                    image = (
                        message.get(
                            "image",
                            {}
                        )
                    )


                    media_id = (
                        image.get(
                            "id"
                        )
                    )


                    caption = normalize_text(

                        image.get(
                            "caption",
                            ""
                        )

                    )


                    if not media_id:
                        continue


                    photo_number = (
                        current_count
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


                    if file_path:


                        with state_lock:


                            session = (

                                pending_motos
                                .setdefault(
                                    sender,
                                    new_session()
                                )

                            )


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
                                not session[
                                    "text"
                                ]
                            ):

                                session[
                                    "text"
                                ] = caption


                        logger.info(

                            "FOTO %s/10 REGISTRADA",

                            photo_number

                        )


                        if (
                            photo_number
                            == 1
                        ):

                            logger.info(
                                "FOTO #1 = PORTADA"
                            )


                        if (
                            photo_number
                            == 10
                        ):

                            logger.info(
                                "LAS 10 FOTOS YA ESTAN COMPLETAS"
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

                    if (
                        command
                        ==
                        "RESET"
                    ):


                        with state_lock:


                            pending_motos[
                                sender
                            ] = new_session()


                        logger.info(
                            "SESION REINICIADA"
                        )

                        continue


                    # ---------------------------------
                    # REINTENTAR
                    # ---------------------------------

                    if (
                        command
                        ==
                        "REINTENTAR"
                    ):


                        logger.info(
                            "REINTENTO SOLICITADO"
                        )


                        maybe_start_processing(
                            sender
                        )

                        continue


                    with state_lock:


                        session = (

                            pending_motos
                            .setdefault(
                                sender,
                                new_session()
                            )

                        )


                        if session.get(
                            "processing"
                        ):

                            logger.warning(
                                "PORTADA EN PROCESO"
                            )

                            continue


                        session[
                            "text"
                        ] = text


                    logger.info(
                        "TEXTO RECIBIDO"
                    )


                # =====================================
                # INTENTAR GENERAR
                # =====================================

                maybe_start_processing(
                    sender
                )


    # RESPONDER INMEDIATAMENTE A META

    return jsonify({

        "success":
            True

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
