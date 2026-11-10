import os
import sys
import time
import json
import base64
import threading
import urllib.request
import urllib.parse
import urllib.error

sys.stdout.reconfigure(encoding="utf-8")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "jafet_encuesta_903689").strip()
DEFAULT_CHAT_ID = int(os.environ.get("CHAT_ID", "7835887634").strip())
GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "Anca-JM/encuesta-bot-cloud").strip()

MAX_RUNTIME_SECONDS = 5 * 3600 + 25 * 60  # 5h 25m (antes del limite de 6h de GitHub Actions)
START_TIME = time.time()

lock = threading.Lock()
seen_ntfy_ids = set()
last_update_id = 0
custom_context = ""

SYSTEM_PROMPT = (
    "Eres un asistente experto en resolver preguntas de encuestas, cuestionarios y exámenes en tiempo real. "
    "Lee la imagen o texto de la pantalla del teléfono y responde en español de forma ULTRA CORTA Y DIRECTA "
    "(ideal para leer en el banner de notificación de Telegram sin abrir la app):\n"
    "• Primera línea: ✅ Respuesta: [Opción correcta exacta]\n"
    "• Si hay varias preguntas visibles en la captura, pon la respuesta de cada una numerada.\n"
    "• Si es una encuesta de opinión/escala (ej. Totalmente de acuerdo, Del 1 al 5) sin respuesta única, "
    "sugiere la opción más coherente y positiva según el contexto."
)


def send_telegram(chat_id, text):
    if not BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f"[WARN] Error enviando Telegram: {e}", flush=True)


def ask_gemini(text_input=None, image_bytes=None, mime_type="image/png"):
    prompt = SYSTEM_PROMPT
    if custom_context:
        prompt += f"\n\nContexto adicional indicado por el usuario: {custom_context}"
    if text_input:
        prompt += f"\n\nContenido de texto detectado en pantalla:\n{text_input}"

    parts = [{"text": prompt}]
    if image_bytes:
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        parts.append({
            "inline_data": {
                "mime_type": mime_type,
                "data": b64_img
            }
        })

    models_to_try = [
        ("gemini-3.5-flash", {"thinkingConfig": {"thinkingBudget": 0}}),
        ("gemini-3.5-flash-lite", {}),
        ("gemini-3.8-flash", {"thinkingConfig": {"thinkingBudget": 0}}),
    ]

    for model_name, gen_cfg in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": parts}]}
        if gen_cfg:
            payload["generationConfig"] = gen_cfg
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=18) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                ans = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                if ans:
                    return ans
        except Exception as e:
            print(f"[WARN] Fallo con {model_name}: {e}", flush=True)
            continue

    return "⚠️ No pude leer la pregunta en este intento. Presiona el botón nuevamente."


def process_ntfy_event(ev):
    if ev.get("event") != "message":
        return
    ev_id = ev.get("id")
    with lock:
        if not ev_id or ev_id in seen_ntfy_ids:
            return
        seen_ntfy_ids.add(ev_id)

    t0 = time.time()
    if "attachment" in ev and ev["attachment"].get("url"):
        att = ev["attachment"]
        att_url = att["url"]
        att_name = att.get("name", "").lower()
        att_type = att.get("type", "").lower()
        try:
            req_att = urllib.request.Request(att_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req_att, timeout=15) as r_att:
                content_bytes = r_att.read()
            if len(content_bytes) == 0:
                return
            if att_name.endswith(".txt") or att_type.startswith("text/"):
                txt = content_bytes.decode("utf-8", errors="replace")
                ans = ask_gemini(text_input=txt)
            else:
                mime = "image/jpeg" if (att_name.endswith(".jpg") or att_name.endswith(".jpeg") or "jpeg" in att_type) else "image/png"
                ans = ask_gemini(image_bytes=content_bytes, mime_type=mime)
            send_telegram(DEFAULT_CHAT_ID, ans)
            print(f"[OK] Respondido ntfy attachment en {round(time.time() - t0, 2)}s", flush=True)
        except Exception as e:
            print(f"[ERR] Procesando attachment ntfy: {e}", flush=True)
    else:
        msg_text = (ev.get("message") or "").strip()
        if not msg_text or msg_text == "triggered":
            return
        ans = ask_gemini(text_input=msg_text)
        send_telegram(DEFAULT_CHAT_ID, ans)
        print(f"[OK] Respondido ntfy texto en {round(time.time() - t0, 2)}s", flush=True)


def init_ntfy_history():
    """Marca como vistos los mensajes viejos antes de iniciar el stream en vivo."""
    try:
        url = f"https://ntfy.sh/{NTFY_TOPIC}/json?poll=1&since=2m"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            for line in resp.read().decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    ev = json.loads(line)
                    if ev.get("id"):
                        seen_ntfy_ids.add(ev["id"])
    except Exception:
        pass


def stream_ntfy_loop():
    init_ntfy_history()
    print("[STREAM] Escuchando ntfy en tiempo real (0ms latencia)...", flush=True)
    while time.time() - START_TIME < MAX_RUNTIME_SECONDS:
        try:
            url = f"https://ntfy.sh/{NTFY_TOPIC}/json"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                for raw_line in resp:
                    if time.time() - START_TIME >= MAX_RUNTIME_SECONDS:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev.get("event") == "message":
                        threading.Thread(target=process_ntfy_event, args=(ev,), daemon=True).start()
        except Exception:
            time.sleep(0.5)


def init_telegram_offset():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset=-1&timeout=1"
        with urllib.request.urlopen(url, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for upd in data.get("result", []):
            last_update_id = max(last_update_id, upd["update_id"])
    except Exception:
        pass


def poll_telegram_loop():
    global last_update_id, custom_context
    init_telegram_offset()
    print("[TELEGRAM] Escuchando mensajes directos en @Polito23_bot...", flush=True)
    while time.time() - START_TIME < MAX_RUNTIME_SECONDS:
        try:
            offset = last_update_id + 1
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            with urllib.request.urlopen(url, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for upd in data.get("result", []):
                last_update_id = upd["update_id"]
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]

                if "photo" in msg:
                    best_photo = msg["photo"][-1]
                    url_f = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={best_photo['file_id']}"
                    with urllib.request.urlopen(url_f, timeout=10) as rf:
                        fp = json.loads(rf.read().decode("utf-8"))["result"]["file_path"]
                    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}", timeout=15) as r_img:
                        img_bytes = r_img.read()
                    ans = ask_gemini(text_input=msg.get("caption", ""), image_bytes=img_bytes, mime_type="image/jpeg")
                    send_telegram(chat_id, ans)

                elif "text" in msg:
                    txt = msg["text"].strip()
                    if txt.lower().startswith("/contexto "):
                        custom_context = txt[10:].strip()
                        send_telegram(chat_id, f"✅ Contexto actualizado: {custom_context}")
                    elif txt.lower() in ("/start", "hola", "/estado", "estado"):
                        send_telegram(chat_id, "🟢 Bot 24/7 activo en la nube (GitHub). Presiona el botón en tu celular o envíame una captura/pregunta.")
                    else:
                        ans = ask_gemini(text_input=txt)
                        send_telegram(chat_id, ans)
        except Exception:
            time.sleep(1)


def trigger_next_workflow():
    if not GH_PAT:
        return
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/bot_24_7.yml/dispatches"
        payload = json.dumps({"ref": "main"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"token {GH_PAT}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print("[RELEVO] Siguiente ejecucion 24/7 disparada con exito.", flush=True)
    except Exception as e:
        print(f"[WARN] No se pudo disparar relevo: {e}", flush=True)


if __name__ == "__main__":
    t1 = threading.Thread(target=stream_ntfy_loop, daemon=True)
    t2 = threading.Thread(target=poll_telegram_loop, daemon=True)
    t1.start()
    t2.start()

    while time.time() - START_TIME < MAX_RUNTIME_SECONDS:
        time.sleep(5)

    trigger_next_workflow()
