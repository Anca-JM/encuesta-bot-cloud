import os
import io
import sys
import re
import time
import datetime
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
GDRIVE_WEBHOOK_URL = os.environ.get("GDRIVE_WEBHOOK_URL", "").strip()
GDRIVE_SECRET = "jafet_drive_903689"

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
    "IGNORA si alguna casilla u opción ya aparece marcada en pantalla; evalúa todo desde cero.\n"
    "PASOS OBLIGATORIOS:\n"
    "1. Razona o calcula paso a paso en 1 o 2 líneas MUY breves (NO transcribas textos largos de opciones).\n"
    "2. Identifica el tipo de pregunta y aplica la regla exacta:\n"
    "   • OPCIÓN ÚNICA VISIBLE (vertical u horizontal con •, comas o espacios): mapea en orden 1ª=A, 2ª=B, 3ª=C, 4ª=D, 5ª=E, 6ª=F, 7ª=G, 8ª=H. Responde SOLO la letra: <R>C</R>. NUNCA pongas el texto ni el número de la opción.\n"
    "   • SELECCIÓN MÚLTIPLE VISIBLE (casillas cuadradas / varias correctas): escribe TODAS las letras correctas separadas por espacio: ej. <R>B F</R> o <R>A C D</R>.\n"
    "   • DESPLEGABLE CERRADO ('Elegir...' / opciones NO visibles en pantalla) o COMPLETAR UN ESPACIO / CÁLCULO SIN OPCIONES: NO inventes letras; escribe el valor numérico exacto o la palabra clave más distintiva (sin artículos El/La/Un, 1-2 palabras): ej. <R>Pasivo</R> o <R>1200</R>.\n"
    "   • EMPAREJAR / VARIOS DESPLEGABLES / VARIOS ESPACIOS / SUB-PREGUNTAS: responde en una línea: <R>1: [valor] | 2: [valor] | 3: [valor]</R>.\n"
    "   • ORDENAR PASOS O SECUENCIAS: responde el orden exacto de los ítems visibles usando '>': ej. <R>3 > 1 > 4 > 2</R> o <R>C > A > B</R>.\n"
    "   • VERDADERO / FALSO: escribe <R>VERDADERO</R> o <R>FALSO</R>.\n"
    "   • SÍ / NO: escribe <R>SI</R> o <R>NO</R>.\n"
    "   • CUALQUIER OTRA PREGUNTA SORPRESA O DIFERENTE (asiento contable, fórmula, definir, tabla, completar frase): responde de forma directa y entendible en 1 sola línea corta (sin relleno, máx. 8-10 palabras): ej. <R>Debe: Caja 500 / Haber: Capital 500</R>.\n"
    "3. Si la pregunta es muy compleja, tiene trampa, es ambigua o tienes menos del 90% de seguridad, NO adivines: responde únicamente <R>ESCALAR</R>.\n"
    "4. De lo contrario, cierra OBLIGATORIAMENTE en la última línea con: <R>RESPUESTA</R>."
)

ADVANCED_PROMPT = (
    "Eres un catedrático experto de máximo nivel. Resuelve con rigor absoluto esta pregunta difícil.\n"
    "Enfócate EXCLUSIVAMENTE en la pregunta activa (la última de abajo) e IGNORA cualquier opción ya marcada.\n"
    "Aplica el formato exacto al final:\n"
    "- Opción única visible (1ª=A .. 8ª=H): solo la letra <R>C</R>.\n"
    "- Selección múltiple visible (varias correctas): letras separadas por espacio <R>B F</R> o <R>A C D</R>.\n"
    "- Desplegable cerrado / sin opciones visibles: número exacto o palabra clave (1-2 palabras).\n"
    "- Emparejar / varios espacios: <R>1: valor | 2: valor</R>. Ordenar: <R>3 > 1 > 2</R>.\n"
    "- Verdadero/Falso: <R>VERDADERO</R> o <R>FALSO</R>. Sí/No: <R>SI</R> o <R>NO</R>.\n"
    "- Otro formato sorpresa: 1 sola línea corta y clara (máx. 10 palabras).\n"
    "Analiza con total precisión y cierra OBLIGATORIAMENTE en la última línea con: <R>RESPUESTA</R>."
)

FALLBACK_PROMPT = (
    "Resuelve el ejercicio académico mostrado en la imagen con 100% de precisión.\n"
    "- Si hay opciones visibles en orden (1ª=A .. 8ª=H): pon solo la letra (<R>C</R>) o varias separadas por espacio (<R>B F</R>).\n"
    "- Si las opciones NO son visibles (desplegable cerrado o completar): NO inventes letras; pon el número exacto o la palabra clave (1-2 palabras).\n"
    "- Si hay varios emparejamientos o espacios: <R>1: valor | 2: valor</R>. Si es ordenar: <R>3 > 1 > 2</R>.\n"
    "- Si es Verdadero/Falso: <R>VERDADERO</R> o <R>FALSO</R>. Si es Sí/No: <R>SI</R> o <R>NO</R>.\n"
    "- Para cualquier otro formato sorpresa: responde claro y directo en 1 línea corta (máx. 10 palabras).\n"
    "Termina siempre con la etiqueta <R>RESPUESTA</R>."
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

    # Respuesta combinada (emparejar / múltiples desplegables / sub-preguntas) o secuencia ordenada
    if ("|" in val and ":" in val) or ">" in val or "→" in val:
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

        # Pregunta abierta / desplegable cerrado / formato sorpresa: 1 línea limpia, corta y entendible
        clean_val = " ".join(val.strip(".,:;() ").split())
        if not clean_val:
            return "..."
        words = clean_val.split()
        if len(words) > 12:
            clean_val = " ".join(words[:12])
        return clean_val[:110]


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


def call_openai_vision(model_name: str, prompt: str, b64_img: str = None, mime_type: str = "image/png", is_reasoning: bool = False):
    content_list = [{"type": "text", "text": prompt}]
    if b64_img:
        content_list.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{b64_img}",
                "detail": "high"
            }
        })

    if is_reasoning:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": content_list}],
            "max_completion_tokens": 900,
            "reasoning_effort": "low"
        }
        timeout_s = 25
    else:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": content_list}],
            "max_tokens": 220,
            "temperature": 0.0
        }
        timeout_s = 14

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        r_data = json.loads(resp.read().decode("utf-8"))
    return (r_data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()


def _upload_to_gdrive(answer: str, reasoning: str, b64_img: str = None, mime_type: str = "image/jpeg"):
    if not GDRIVE_WEBHOOK_URL:
        return
    try:
        now_ec = datetime.datetime.utcnow() - datetime.timedelta(hours=5)
        safe_ans = re.sub(r"[^A-Za-z0-9áéíóúÁÉÍÓÚñÑ_-]+", "_", answer).strip("_")[:30] or "resp"
        ext = "jpg" if "jpeg" in (mime_type or "") else "png"
        filename = f"{now_ec.strftime('%H-%M-%S')}_{safe_ans}.{ext}" if b64_img else ""
        payload = {
            "secret": GDRIVE_SECRET,
            "date": now_ec.strftime("%Y-%m-%d"),
            "timestamp": now_ec.strftime("%Y-%m-%d %H:%M:%S"),
            "answer": answer,
            "reasoning": " ".join((reasoning or "").splitlines())[:500],
            "filename": filename,
            "mime": mime_type if b64_img else "",
            "image_b64": b64_img or ""
        }
        req = urllib.request.Request(
            GDRIVE_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Connection": "close"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            res_txt = resp.read().decode("utf-8", errors="replace").strip()
            if res_txt != "ok":
                print(f"[WARN] Drive respuesta inesperada: {res_txt[:100]}", flush=True)
    except Exception as e:
        print(f"[WARN] Error guardando en Drive: {e}", flush=True)


def save_to_gdrive_async(answer: str, reasoning: str, b64_img: str = None, mime_type: str = "image/jpeg"):
    if not GDRIVE_WEBHOOK_URL:
        return
    threading.Thread(
        target=_upload_to_gdrive,
        args=(answer, reasoning, b64_img, mime_type),
        daemon=True
    ).start()


def ask_ai(text_input=None, image_bytes=None, mime_type="image/png"):
    extra_ctx = ""
    if custom_context:
        extra_ctx += f"\nContexto especial del usuario: {custom_context}"
    if text_input:
        extra_ctx += f"\nTexto capturado de pantalla:\n{text_input}"

    b64_img = None
    if image_bytes:
        hd_bytes, mime_type = prepare_image(image_bytes)
        b64_img = base64.b64encode(hd_bytes).decode("utf-8")

    if OPENAI_API_KEY:
        # Nivel 1: GPT-4.1 (rápido y económico para ~95% de preguntas)
        escalate_reason = None
        try:
            ans = call_openai_vision("gpt-4.1", SYSTEM_PROMPT + extra_ctx, b64_img, mime_type, is_reasoning=False)
            if ans and not is_refusal(ans):
                if re.search(r"<R>\s*ESCALAR\s*(?:</R>|</R|$)", ans, re.I):
                    escalate_reason = "duda de gpt-4.1"
                else:
                    final_out = format_final_output(ans)
                    if final_out and final_out != "...":
                        print(f"[gpt-4.1 -> {final_out}] {ans.replace(chr(10), ' ')}", flush=True)
                        save_to_gdrive_async(final_out, ans, b64_img, mime_type)
                        return final_out
                    escalate_reason = "respuesta vacía"
            else:
                escalate_reason = "rechazo o vacío"
        except Exception as e:
            escalate_reason = f"error: {e}"

        print(f"[ESCALANDO -> gpt-5.4] Motivo: {escalate_reason}", flush=True)

        # Nivel 2: GPT-5.4 (IA avanzada con razonamiento profundo solo en casos difíciles)
        try:
            ans_adv = call_openai_vision("gpt-5.4", ADVANCED_PROMPT + extra_ctx, b64_img, mime_type, is_reasoning=True)
            if ans_adv and not is_refusal(ans_adv):
                final_out = format_final_output(ans_adv)
                print(f"[gpt-5.4 -> {final_out}] {ans_adv.replace(chr(10), ' ')}", flush=True)
                save_to_gdrive_async(final_out, f"[GPT-5.4] {ans_adv}", b64_img, mime_type)
                return final_out
        except Exception as e:
            print(f"[WARN] gpt-5.4 fallo: {e}", flush=True)

        # Nivel 3: Respaldo de emergencia gpt-4o
        try:
            ans_fb = call_openai_vision("gpt-4o", FALLBACK_PROMPT + extra_ctx, b64_img, mime_type, is_reasoning=False)
            if ans_fb and not is_refusal(ans_fb):
                final_out = format_final_output(ans_fb)
                print(f"[gpt-4o -> {final_out}] {ans_fb.replace(chr(10), ' ')}", flush=True)
                save_to_gdrive_async(final_out, f"[GPT-4o] {ans_fb}", b64_img, mime_type)
                return final_out
        except Exception as e:
            print(f"[WARN] gpt-4o fallo: {e}", flush=True)

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
