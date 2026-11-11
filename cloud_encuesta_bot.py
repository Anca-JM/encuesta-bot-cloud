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

try:
    from PIL import Image, ImageChops
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

CODE_WORDS = {
    "A": ["Árbol", "Ámbar"],
    "1": ["Árbol", "Ámbar"],
    "B": ["Barco", "Bosque"],
    "2": ["Barco", "Bosque"],
    "C": ["Casa", "Cielo"],
    "3": ["Casa", "Cielo"],
    "D": ["Dedo", "Duna"],
    "4": ["Dedo", "Duna"],
}

SYSTEM_PROMPT = (
    "Analiza la pregunta PRINCIPAL de la imagen (ignora miniaturas o barras del navegador).\n"
    "- Si las opciones tienen letra (A, B, C, D), usa esa letra. Si NO tienen letra, cuéntalas de arriba a abajo: 1=A, 2=B, 3=C, 4=D.\n"
    "- En 1 sola frase corta (máximo 15 palabras) identifica el concepto académico exacto descartando trampas y cierra obligatoriamente con <R>LETRA</R> (ej. <R>A</R>, <R>B</R>, <R>C</R> o <R>D</R>)."
)


def prepare_image_smart_crop(image_bytes: bytes):
    """
    Recorta automáticamente las franjas negras vacías de la captura y ajusta a 800px.
    Ahorra hasta 85% de tokens de imagen y hace que las letras pequeñas se vean mucho más grandes y claras.
    """
    if not HAS_PIL:
        return image_bytes, "image/png"
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        # Recortar barras superiores/inferiores negras si existen
        bg = Image.new("RGB", img.size, (0, 0, 0))
        diff = ImageChops.difference(img, bg).convert("L").point(lambda x: 255 if x > 20 else 0)
        bbox = diff.getbbox()
        if bbox:
            # Dejar un pequeño margen de 10px
            left = max(0, bbox[0] - 10)
            top = max(0, bbox[1] - 10)
            right = min(img.width, bbox[2] + 10)
            bottom = min(img.height, bbox[3] + 10)
            if (right - left) > 150 and (bottom - top) > 100:
                img = img.crop((left, top, right, bottom))

        w, h = img.size
        if max(w, h) > 800:
            scale = 800.0 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=82, optimize=False)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, "image/png"


def extract_letter_from_reasoning(raw_text: str):
    if not raw_text:
        return None
    m = re.search(r"<R>\s*([A-D1-4])\s*</R>", raw_text, re.I)
    if m:
        ch = m.group(1).upper()
        return {"1": "A", "2": "B", "3": "C", "4": "D"}.get(ch, ch)

    m2 = re.findall(r"(?:correcta\s+es|respuesta\s*[:es]*)\s*(?:la\s+|opci[oó]n\s+)?([A-D1-4])\b", raw_text, re.I)
    if m2:
        ch = m2[-1].upper()
        return {"1": "A", "2": "B", "3": "C", "4": "D"}.get(ch, ch)

    cleaned = raw_text.strip().upper()
    if cleaned in ("A", "B", "C", "D", "1", "2", "3", "4"):
        return {"1": "A", "2": "B", "3": "C", "4": "D"}.get(cleaned, cleaned)
    return None


def letter_to_codeword(letter: str) -> str:
    global toggle_idx
    with lock:
        words = CODE_WORDS.get(letter.upper(), ["..."])
        word = words[toggle_idx % len(words)]
        toggle_idx += 1
        return word


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
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception as e:
        print(f"[WARN] Error enviando Telegram: {e}", flush=True)


def ask_ai(text_input=None, image_bytes=None, mime_type="image/png"):
    prompt = SYSTEM_PROMPT
    if custom_context:
        prompt += f"\nContexto: {custom_context}"
    if text_input:
        prompt += f"\nTexto:\n{text_input}"

    b64_img = None
    if image_bytes:
        cropped_bytes, mime_type = prepare_image_smart_crop(image_bytes)
        b64_img = base64.b64encode(cropped_bytes).decode("utf-8")

    # 1. Un solo llamado ultra-eficiente a OpenAI gpt-4.1-mini (ahorra 85% de tokens y responde en ~1.8s)
    if OPENAI_API_KEY:
        content_list = [{"type": "text", "text": prompt}]
        if b64_img:
            content_list.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{b64_img}",
                    "detail": "auto"
                }
            })
        for model_name in ["gpt-4.1-mini", "gpt-4o-mini"]:
            try:
                payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": content_list}],
                    "max_tokens": 50,
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
                ans = r_data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                letter = extract_letter_from_reasoning(ans)
                if letter:
                    codeword = letter_to_codeword(letter)
                    used = r_data.get("usage", {}).get("total_tokens", "?")
                    print(f"[OK {model_name} | {used} tokens -> {letter} -> {codeword}] {ans}", flush=True)
                    return codeword
            except Exception as e:
                print(f"[WARN] {model_name}: {e}", flush=True)

    # 2. Respaldo gratuito automatico: Gemini 3.5 Flash
    if GEMINI_API_KEY:
        parts = [{"text": prompt}]
        if b64_img:
            parts.append({"inline_data": {"mime_type": mime_type, "data": b64_img}})
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 256}, "temperature": 0.0}
        }
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=14) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if candidates:
                parts_out = candidates[0].get("content", {}).get("parts", [])
                ans = "\n".join(p.get("text", "") for p in parts_out).strip()
                letter = extract_letter_from_reasoning(ans)
                if letter:
                    return letter_to_codeword(letter)
        except Exception as e:
            print(f"[WARN] Gemini fallback: {e}", flush=True)

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
            print(f"[OK] Respondido ({ans}) en {round(time.time() - t0, 2)}s", flush=True)
        except Exception as e:
            print(f"[ERR] Procesando attachment ntfy: {e}", flush=True)
    else:
        msg_text = (ev.get("message") or "").strip()
        if not msg_text or msg_text == "triggered":
            return
        ans = ask_ai(text_input=msg_text)
        send_telegram(DEFAULT_CHAT_ID, ans)
        print(f"[OK] Respondido texto ({ans}) en {round(time.time() - t0, 2)}s", flush=True)


def init_ntfy_history():
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
    print("[STREAM] Escuchando ntfy en Modo Inteligente Ahorro de Tokens...", flush=True)
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
            time.sleep(0.2)


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
                    ans = ask_ai(text_input=msg.get("caption", ""), image_bytes=img_bytes, mime_type="image/jpeg")
                    send_telegram(chat_id, ans)

                elif "text" in msg:
                    txt = msg["text"].strip()
                    if txt.lower().startswith("/contexto "):
                        custom_context = txt[10:].strip()
                        send_telegram(chat_id, f"✅ Contexto: {custom_context}")
                    elif txt.lower() in ("/start", "hola", "/estado", "estado"):
                        send_telegram(chat_id, "🧠 Activo (Ahorro de Tokens + Anti-Trampa)")
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
