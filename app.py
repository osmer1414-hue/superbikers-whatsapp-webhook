def get_session(sender):
    if sender not in pending_motos:
        pending_motos[sender] = {"photos": [], "text": ""}
    return pending_motos[sender]


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
