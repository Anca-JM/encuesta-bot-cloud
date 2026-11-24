import os
import io
import sys
import re
import time
import json
import base64
import threading
import urllib.request
import urllib.parse
import urllib.error
import http.client

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

sys.stdout.reconfigure(encoding="utf-8")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "jafet_encuesta_903689").strip()
DEFAULT_CHAT_ID = int(os.environ.get("CHAT_ID", "7835887634").strip())
GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "Anca-JM/encuesta-bot-cloud").strip()

MAX_RUNTIME_SECONDS = 5 * 3600 + 25 * 60  # 5h 25m
START_TIME = time.time()

lock = threading.Lock()
seen_ntfy_ids = set()
last_update_id = 0
custom_context = ""
toggle_idx = 0
previous_msg_ids = []

CODE_WORDS = {
    "A": ["Árbol", "Ámbar"],
    "1": ["Árbol", "Ámbar"],
    "B": ["Barco", "Bosque"],
    "2": ["Barco", "Bosque"],
    "C": ["Casa", "Cielo"],
    "3": ["Casa", "Cielo"],
    "D": ["Dedo", "Duna"],
    "4": ["Dedo", "Duna"],
    "E": ["Eco", "Eje"],
    "5": ["Eco", "Eje"],
    "F": ["Faro", "Fuego"],
    "6": ["Faro", "Fuego"],
    "G": ["Gato", "Gris"],
    "7": ["Gato", "Gris"],
    "H": ["Hilo", "Hoja"],
    "8": ["Hilo", "Hoja"],
}

SYSTEM_PROMPT = (
    "Eres un profesor universitario experto con precisión analítica y matemática del 100%.\n"
    "Enfócate EXCLUSIVAMENTE en la pregunta activa (la última de abajo si hay historial anterior).\n"
    "IGNORA si alguna casilla u opción ya aparece marcada en pantalla; evalúa todas las opciones desde cero.\n"
    "PASOS OBLIGATORIOS:\n"
    "1. Razona o calcula paso a paso en 1 o 2 líneas MUY breves (NO transcribas el texto largo de las opciones).\n"
    "2. Si hay opciones (en lista vertical u horizontal separadas por puntos •, comas o espacios):\n"
    "   - Mapea las opciones en orden de aparición: 1ª=A, 2ª=B, 3ª=C, 4ª=D, 5ª=E, 6ª=F, 7ª=G, 8ª=H.\n"
    "   - Si es opción única, responde solo la letra: <R>C</R>. NUNCA pongas el texto ni el número de la opción.\n"
    "   - Si es selección múltiple (casillas cuadradas / pide marcar varias), incluye TODAS las letras correctas separadas por espacio: ej. <R>B F</R> o <R>A C D</R>.\n"
    "3. Si es Verdadero/Falso: escribe <R>VERDADERO</R> o <R>FALSO</R>.\n"
    "4. Si es Sí/No: escribe <R>SI</R> o <R>NO</R>.\n"
    "5. Si pide dos sub-preguntas distintas (A y B): <R>A: [letra] | B: [letra]</R>.\n"
    "6. Si es pregunta abierta sin opciones: escribe solo la primera palabra clave dentro de <R>...</R>.\n"
    "7. Cierra OBLIGATORIAMENTE en la última línea con: <R>RESPUESTA</R>."
)

FALLBACK_PROMPT = (
    "Analiza la pregunta académica mostrada en la imagen y determina la respuesta correcta.\n"
    "Mapea las opciones visibles en orden (1ª=A, 2ª=B, 3ª=C, 4ª=D, 5ª=E, 6ª=F, 7ª=G, 8ª=H).\n"
    "Si hay varias opciones correctas, sepáralas con espacio (ej. <R>B F</R>).\n"
    "Si es Verdadero/Falso escribe <R>VERDADERO</R> o <R>FALSO</R>. Si es Sí/No escribe <R>SI</R> o <R>NO</R>.\n"
    "Responde brevemente y termina siempre con la etiqueta <R>RESPUESTA</R>."
)

REFUSAL_PHRASES = (
    "lo siento",
    "no puedo ayudar",
    "no puedo responder",
    "no puedo asistir",
    "i'm sorry",
    "i cannot assist",
    "i can't help",
    "cannot help with",
)


def is_refusal(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in REFUSAL_PHRASES)


def prepare_image(image_bytes: bytes):
    if not HAS_PIL:
        return image_bytes, "image/png"
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        if max(w, h) > 1440:
            scale = 1440.0 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88, optimize=False)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, "image/png"


def format_final_output(raw_text: str) -> str:
    global toggle_idx
    if not raw_text:
        return "..."

    # Extraer etiquetas <R>...</R> (tolerando si se cortó el '>' final)
    tags = re.findall(r"<R>\s*([^<]+?)\s*(?:</R>|</R|$)", raw_text, re.I)
    valid_tags = [
        t.strip()
        for t in tags
        if t.strip().upper() not in ("VALOR", "RESULTADO", "RESPUESTA", "LETRA_O_RESPUESTA", "ANSWER", "")
    ]

    if valid_tags:
        val = valid_tags[0]
    else:
        # Fallback: buscar letra A-H aislada al final
        m = re.search(r"\b([A-H])\b[^\w]*$", raw_text.strip(), re.I)
        if m:
            val = m.group(1)
        else:
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            val = lines[-1].strip(".,:;() ") if lines else "..."

    # Respuesta combinada con dos sub-preguntas tipo "A: C | B: D"
    if "|" in val and ":" in val:
        return val

    # Múltiples letras separadas por coma, espacio, barra o "y" (ej: "A C D", "B, F", "B | F", "C y G")
    clean_parts = [
        p.strip(",.;:() ")
        for p in re.split(r"[\s,|]+|\by\b", val.strip(), flags=re.I)
        if p.strip(",.;:() ")
    ]
    if len(clean_parts) > 1 and all(p.upper() in CODE_WORDS for p in clean_parts):
        with lock:
            mapped = []
            for p in clean_parts:
                words = CODE_WORDS[p.upper()]
                mapped.append(words[toggle_idx % len(words)])
                toggle_idx += 1
            return " | ".join(mapped)

    val_upper = val.strip(".,:;() ").upper()

    with lock:
        if val_upper in ("V", "VERDADERO", "TRUE"):
            return "V"
        if val_upper in ("FALSO", "FALSE"):
            return "F"
        if val_upper == "F" and re.search(r"\b(falso|verdadero|false|true)\b", raw_text, re.I):
            return "F"
        if val_upper in ("S", "SI", "SÍ", "YES"):
            return "S"
        if val_upper in ("N", "NO"):
            return "N"

        # Mapeo de letra a palabra clave (A->Árbol/Ámbar ... H->Hilo/Hoja)
        if val_upper in CODE_WORDS:
            words = CODE_WORDS[val_upper]
            word = words[toggle_idx % len(words)]
            toggle_idx += 1
            return word

        # Pregunta abierta: primera palabra
        first_word = val.split()[0] if val.split() else val
        return first_word


def delete_single_message(chat_id, message_id):
    if not BOT_TOKEN or not chat_id or not message_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "message_id": message_id
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception:
        pass


def delete_old_messages(chat_id, msg_ids):
    for mid in msg_ids:
        delete_single_message(chat_id, mid)


def send_telegram(chat_id, text):
    global previous_msg_ids
    if not BOT_TOKEN or not chat_id:
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text
    }).encode("utf-8")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers={"Connection": "close"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                if res_data.get("ok"):
                    new_id = res_data["result"]["message_id"]
                    with lock:
                        to_delete = list(previous_msg_ids)
                        previous_msg_ids = [new_id]
                    if to_delete:
                        threading.Thread(target=delete_old_messages, args=(chat_id, to_delete), daemon=True).start()
                    return
        except Exception as e:
            print(f"[WARN] Error enviando Telegram (intento {attempt + 1}/4): {e}", flush=True)
            time.sleep(0.35 * (attempt + 1))


def call_openai_vision(model_name: str, prompt: str, b64_img: str = None, mime_type: str = "image/png"):
    content_list = [{"type": "text", "text": prompt}]
    if b64_img:
        content_list.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{b64_img}",
                "detail": "high"
            }
        })

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": content_list}],
        "max_tokens": 220,
        "temperature": 0.0
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=14) as resp:
        r_data = json.loads(resp.read().decode("utf-8"))
    return r_data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()


def ask_ai(text_input=None, image_bytes=None, mime_type="image/png"):
    prompt = SYSTEM_PROMPT
    if custom_context:
        prompt += f"\nContexto especial del usuario: {custom_context}"
    if text_input:
        prompt += f"\nTexto capturado de pantalla:\n{text_input}"

    b64_img = None
    if image_bytes:
        hd_bytes, mime_type = prepare_image(image_bytes)
        b64_img = base64.b64encode(hd_bytes).decode("utf-8")

    # 1. GPT-4.1 principal + respaldo automático si hay rechazo o error
    if OPENAI_API_KEY:
        attempts = [
            ("gpt-4.1", prompt),
            ("gpt-4o", FALLBACK_PROMPT + (f"\nContexto: {custom_context}" if custom_context else "") + (f"\nTexto:\n{text_input}" if text_input else "")),
        ]
        for model, used_prompt in attempts:
            try:
                ans = call_openai_vision(model, used_prompt, b64_img, mime_type)
                if ans and not is_refusal(ans):
                    final_out = format_final_output(ans)
                    print(f"[{model} -> {final_out}] {ans.replace(chr(10), ' ')}", flush=True)
                    return final_out
                elif ans:
                    print(f"[WARN] {model} rechazó la imagen, probando respaldo...", flush=True)
            except Exception as e:
                print(f"[WARN] {model} fallo: {e}", flush=True)

    return "..."


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
            with urllib.request.urlopen(req_att, timeout=12) as r_att:
                content_bytes = r_att.read()
            if len(content_bytes) == 0:
                return
            if att_name.endswith(".txt") or att_type.startswith("text/"):
                txt = content_bytes.decode("utf-8", errors="replace")
                ans = ask_ai(text_input=txt)
            else:
                mime = "image/jpeg" if (att_name.endswith(".jpg") or att_name.endswith(".jpeg") or "jpeg" in att_type) else "image/png"
                ans = ask_ai(image_bytes=content_bytes, mime_type=mime)
            send_telegram(DEFAULT_CHAT_ID, ans)
            print(f"[RESUELTO OK] Respondido ({ans}) en {round(time.time() - t0, 2)}s", flush=True)
        except Exception as e:
            print(f"[ERR] Procesando attachment ntfy: {e}", flush=True)
    else:
        msg_text = (ev.get("message") or "").strip()
        if not msg_text or msg_text == "triggered":
            return
        ans = ask_ai(text_input=msg_text)
        send_telegram(DEFAULT_CHAT_ID, ans)
        print(f"[RESUELTO OK] Respondido texto ({ans}) en {round(time.time() - t0, 2)}s", flush=True)


def init_ntfy_history():
    try:
        url = f"https://ntfy.sh/{NTFY_TOPIC}/json?poll=1&since=2m"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            for line in resp.read().decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    ev = json.loads(line)
                    if ev.get("id"):
                        seen_ntfy_ids.add(ev["id"])
    except Exception:
        pass


def stream_ntfy_loop():
    init_ntfy_history()
    print("[STREAM] Escuchando ntfy en vivo...", flush=True)
    while time.time() - START_TIME < MAX_RUNTIME_SECONDS:
        try:
            conn = http.client.HTTPSConnection("ntfy.sh", timeout=75)
            conn.request("GET", f"/{NTFY_TOPIC}/json", headers={"User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            while time.time() - START_TIME < MAX_RUNTIME_SECONDS:
                raw_line = resp.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("event") == "message":
                    threading.Thread(target=process_ntfy_event, args=(ev,), daemon=True).start()
            conn.close()
        except Exception:
            time.sleep(0.15)


def init_telegram_offset():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset=-1&timeout=1"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for upd in data.get("result", []):
            last_update_id = max(last_update_id, upd["update_id"])
    except Exception:
        pass


def poll_telegram_loop():
    global last_update_id, custom_context
    init_telegram_offset()
    while time.time() - START_TIME < MAX_RUNTIME_SECONDS:
        try:
            offset = last_update_id + 1
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=15"
            with urllib.request.urlopen(url, timeout=20) as resp:
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
                    with urllib.request.urlopen(url_f, timeout=8) as rf:
                        fp = json.loads(rf.read().decode("utf-8"))["result"]["file_path"]
                    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}", timeout=10) as r_img:
                        img_bytes = r_img.read()
                    ans = ask_ai(text_input=msg.get("caption", ""), image_bytes=img_bytes, mime_type="image/jpeg")
                    send_telegram(chat_id, ans)

                elif "text" in msg:
                    txt = msg["text"].strip()
                    if txt.lower().startswith("/contexto "):
                        custom_context = txt[10:].strip()
                        send_telegram(chat_id, f"✅ Contexto: {custom_context}")
                    elif txt.lower() in ("/start", "hola", "/estado", "estado"):
                        send_telegram(chat_id, "🧠 Modo GPT-4.1 Ultra Preciso 24/7 Activo")
                    else:
                        ans = ask_ai(text_input=txt)
                        send_telegram(chat_id, ans)
        except Exception:
            time.sleep(0.5)


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
    except Exception:
        pass


if __name__ == "__main__":
    t1 = threading.Thread(target=stream_ntfy_loop, daemon=True)
    t2 = threading.Thread(target=poll_telegram_loop, daemon=True)
    t1.start()
    t2.start()

    while time.time() - START_TIME < MAX_RUNTIME_SECONDS:
        time.sleep(5)

    trigger_next_workflow()
