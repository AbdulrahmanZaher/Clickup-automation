import os
import requests
from fastapi import FastAPI, Request

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CLICKUP_TOKEN = os.getenv("CLICKUP_TOKEN")
CLICKUP_LIST_ID = os.getenv("CLICKUP_LIST_ID")

def send_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)

def create_clickup_task(name: str, description: str):
    url = f"https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task"
    headers = {
        "Authorization": CLICKUP_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "name": name,
        "description": description,
    }
    r = requests.post(url, json=payload, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

@app.post("/telegram")
async def telegram_webhook(req: Request):
    update = await req.json()
    msg = update.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    if not text:
        return {"ok": True}

    try:
        task = create_clickup_task(name=text[:100], description=text)
        send_message(chat_id, f"✅ Created task in ClickUp: {task['name']}")
    except Exception as e:
        send_message(chat_id, f"❌ Failed to create task: {e}")

    return {"ok": True}

@app.get("/")
def health():
    return {"status": "ok"}
