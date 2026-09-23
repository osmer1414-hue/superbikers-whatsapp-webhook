import os
import json
import logging
import re
import mimetypes
from pathlib import Path

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("superbikers-whatsapp-webhook")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v26.0")

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
