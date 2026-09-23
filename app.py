import os
import json
import logging
import re
from pathlib import Path

import requests
from flask import Flask, request, jsonify


# =========================================================
# CONFIGURACIÓN
# =========================================================

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")

GRAPH_API_VERSION = "v26.0"

# Aquí guardaremos temporalmente las fotos de cada moto.
pending_motos = {}


# =========================================================
# SERVIDOR
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "Superbikers WhatsApp"
    }), 200


@app.route("/privacy", methods=["GET"])
def privacy():
    return """
    <html>
        <head>
            <title>Política de Privacidad - Superbikers Shop</title>
        </head>

        <body style="font-family:Arial;max-width:800px;margin:40px auto;">
            <h1>Política de Privacidad de Superbikers Shop</h1>

            <p>
                Superbikers Shop utiliza la información recibida mediante
                WhatsApp, Facebook e Instagram para atender clientes,
                administrar motocicletas y gestionar publicaciones.
            </p>

            <p>
                No vendemos ni comercializamos información personal.
            </p>

            <p>
                Última actualización: septiembre de 2026.
            </p>
        </body>
    </html>
    """, 200


# =========================================================
# VERIFICACIÓN DE META
# =========================================================

@app.route("/webhook/whatsapp", methods=["GET"])
def verify_webhook():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:

        logger.info("Webhook verificado correctamente.")

        return challenge, 200

    return "Forbidden", 403


# =========================================================
# FUNCIONES PARA TEXTO
# =========================================================

def limpiar_texto(texto):

    if not texto:
        return ""

    return (
        texto
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def obtener_precio(texto):

    if not texto:
        return ""

    resultado = re.search(
        r"\$\s*([\d,\.]+)",
        texto
    )

    if not resultado:
        return ""

    numeros = re.sub(
        r"[^\d]",
        "",
        resultado.group(1)
    )

    if not numeros:
        return ""

    return f"${int(numeros):,}"


def obtener_titulo(texto):

    if not texto:
        return ""

    lineas = [
        linea.strip()
        for linea in texto.split("\n")
        if linea.strip()
    ]

    for linea in lineas:

        if linea.startswith("#"):
            continue

        if linea.startswith("$"):
            continue

        # Quitar emojis/símbolos al inicio.
        titulo = re.sub(
            r"^[^\wÁÉÍÓÚÜÑáéíóúüñ]+",
            "",
            linea
        )

        return titulo.strip()

    return ""


def obtener_hashtags(texto):

    return re.findall(
        r"#\w+",
        texto or ""
    )


# =========================================================
# DESCARGAR FOTO DESDE WHATSAPP
# =========================================================

def descargar_foto(media_id, numero_whatsapp, numero_foto):

    if not WHATSAPP_ACCESS_TOKEN:

        logger.error(
            "ERROR: falta WHATSAPP_ACCESS_TOKEN"
        )

        return None

    headers = {
        "Authorization":
        f"Bearer {WHATSAPP_ACCESS_TOKEN}"
    }

    # PASO 1:
    # Pedirle a Meta la URL temporal de la fotografía.

    url_info = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/{media_id}"
    )

    try:

        respuesta = requests.get(
            url_info,
            headers=headers,
            timeout=30
        )

    except Exception as error:

        logger.error(
            "Error consultando foto: %s",
            error
        )

        return None


    if respuesta.status_code != 200:

        logger.error(
            "ERROR obteniendo URL de foto: %s",
            respuesta.text
        )

        return None


    datos = respuesta.json()

    url_foto = datos.get("url")
    mime_type = datos.get(
        "mime_type",
        "image/jpeg"
    )


    if not url_foto:

        logger.error(
            "Meta no devolvió URL de fotografía."
        )

        return None


    # PASO 2:
    # Descargar la imagen.

    try:

        foto = requests.get(
            url_foto,
            headers=headers,
            timeout=60
        )

    except Exception as error:

        logger.error(
            "Error descargando fotografía: %s",
            error
        )

        return None


    if foto.status_code != 200:

        logger.error(
            "ERROR descargando foto: %s",
            foto.text
        )

        return None


    # Extensión de imagen.

    if mime_type == "image/png":
        extension = ".png"

    elif mime_type == "image/webp":
        extension = ".webp"

    else:
        extension = ".jpg"


    carpeta = (
        Path("/tmp/superbikers")
        / numero_whatsapp
    )

    carpeta.mkdir(
        parents=True,
        exist_ok=True
    )


    archivo = (
        carpeta
        / f"foto_{numero_foto:02d}{extension}"
    )


    archivo.write_bytes(
        foto.content
    )


    logger.info(
        "FOTO %s/10 DESCARGADA | %s",
        numero_foto,
        archivo
    )


    return str(archivo)


# =========================================================
# SESIÓN DE CADA MOTO
# =========================================================

def obtener_sesion(numero_whatsapp):

    if numero_whatsapp not in pending_motos:

        pending_motos[numero_whatsapp] = {
            "photos": [],
            "text": ""
        }

    return pending_motos[numero_whatsapp]


# =========================================================
# CREAR MOTO COMPLETA
# =========================================================

def intentar_completar_moto(numero_whatsapp):

    sesion = pending_motos.get(
        numero_whatsapp
    )


    if not sesion:
        return None


    # Necesitamos exactamente 10 fotos.

    if len(sesion["photos"]) != 10:
        return None


    # También necesitamos el texto.

    if not sesion["text"]:
        return None


    texto = sesion["text"]


    moto = {

        "full_text_original":
            texto,

        "cover_title":
            obtener_titulo(texto),

        "cover_price":
            obtener_precio(texto),

        "hashtags":
            obtener_hashtags(texto),

        "total_photos":
            10,

        # FOTO 1 = PORTADA

        "cover_file_path":
            sesion["photos"][0]["file_path"],

        "photos":
            sesion["photos"]
    }


    logger.info(
        "\n"
        "============================================\n"
        "MOTO COMPLETA\n"
        "============================================\n"
        "%s\n"
        "============================================",
        json.dumps(
            moto,
            ensure_ascii=False,
            indent=2
        )
    )


    # Prepararnos para la siguiente moto.

    pending_motos[numero_whatsapp] = {
        "photos": [],
        "text": ""
    }


    return moto


# =========================================================
# RECIBIR MENSAJES DE WHATSAPP
# =========================================================

@app.route(
    "/webhook/whatsapp",
    methods=["POST"]
)
def recibir_whatsapp():

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

                numero = message.get(
                    "from"
                )


                if not numero:
                    continue


                tipo = message.get(
                    "type"
                )


                sesion = obtener_sesion(
                    numero
                )


                # =====================================
                # SI LLEGA FOTO
                # =====================================

                if tipo == "image":

                    imagen = message.get(
                        "image",
                        {}
                    )


                    media_id = imagen.get(
                        "id"
                    )


                    if not media_id:
                        continue


                    if len(
                        sesion["photos"]
                    ) >= 10:

                        logger.warning(
                            "Ya hay 10 fotos."
                        )

                        continue


                    numero_foto = (
                        len(
                            sesion["photos"]
                        )
                        + 1
                    )


                    archivo = descargar_foto(
                        media_id,
                        numero,
                        numero_foto
                    )


                    # Solo contamos la foto
                    # si realmente se descargó.

                    if archivo:

                        sesion[
                            "photos"
                        ].append({

                            "number":
                                numero_foto,

                            "media_id":
                                media_id,

                            "file_path":
                                archivo
                        })


                        logger.info(
                            "FOTO %s/10 REGISTRADA",
                            numero_foto
                        )


                        if numero_foto == 1:

                            logger.info(
                                "FOTO #1 = PORTADA"
                            )


                        if numero_foto == 10:

                            logger.info(
                                "10 FOTOS COMPLETAS"
                            )


                # =====================================
                # SI LLEGA TEXTO
                # =====================================

                elif tipo == "text":

                    texto = limpiar_texto(

                        message.get(
                            "text",
                            {}
                        ).get(
                            "body",
                            ""
                        )

                    )


                    sesion["text"] = texto


                    logger.info(
                        "TEXTO RECIBIDO"
                    )


                # Intentar terminar la publicación.

                intentar_completar_moto(
                    numero
                )


    # SIEMPRE contestar 200 a Meta.

    return jsonify({
        "success": True
    }), 200


# =========================================================
# INICIAR SERVIDOR
# =========================================================

if __name__ == "__main__":

    puerto = int(
        os.environ.get(
            "PORT",
            10000
        )
    )


    app.run(
        host="0.0.0.0",
        port=puerto
    )
