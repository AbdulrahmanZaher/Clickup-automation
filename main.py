import os
import json
import time
from typing import Dict, Any, Optional

import requests
from fastapi import FastAPI, Request

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CLICKUP_TOKEN = os.getenv("CLICKUP_TOKEN", "")
DEFAULT_LIST_ID = os.getenv("CLICKUP_DEFAULT_LIST_ID", "")

LIST_ROUTING_JSON = os.getenv("LIST_ROUTING_JSON", "{}")
try:
    LIST_ROUTING = {k.lower(): str(v) for k, v in json.loads(LIST_ROUTING_JSON).items()}
except Exception:
    LIST_ROUTING = {}

# In-memory drafts: chat_id -> draft
DRAFTS: Dict[int, Dict[str, Any]] = {}

PRIORITIES = [("Urgent", 1), ("High", 2), ("Normal", 3), ("Low", 4)]
DUE_CHOICES = [
    ("No due date", None),
    ("Today", "today"),
    ("Tomorrow", "tomorrow"),
    ("This week", "thisweek"),
]

def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"

def clickup_headers() -> Dict[str, str]:
    return {"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"}

def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(tg_api("sendMessage"), json=payload, timeout=20)

def edit_message(chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None):
    payload: Dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(tg_api("editMessageText"), json=payload, timeout=20)

def answer_callback(callback_id: str):
    requests.post(tg_api("answerCallbackQuery"), json={"callback_query_id": callback_id}, timeout=20)

def create_clickup_task(list_id: str, name: str, description: str = "", due_date_ms: Optional[int] = None, priority: Optional[int] = None):
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    payload: Dict[str, Any] = {"name": name}
    if description:
        payload["description"] = description
    if due_date_ms is not None:
        payload["due_date"] = due_date_ms
    if priority is not None:
        payload["priority"] = priority

    r = requests.post(url, headers=clickup_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def resolve_list_id(project_key: Optional[str]) -> str:
    if project_key:
        k = project_key.strip().lower()
        if k in LIST_ROUTING:
            return LIST_ROUTING[k]
    return DEFAULT_LIST_ID

def due_choice_to_epoch_ms(choice: Optional[str]) -> Optional[int]:
    if choice is None:
        return None

    # Simple "end of day" timestamps in local time (approx)
    now = int(time.time())
    # We'll approximate day boundaries using UTC seconds; good enough for reminders.
    # If you want exact Asia/Riyadh midnight handling, we can add timezone libs later.
    day = 24 * 60 * 60

    if choice == "today":
        # end of today (approx): now + (remaining hours) is complex; simplest: +8 hours
        return (now + 8 * 60 * 60) * 1000
    if choice == "tomorrow":
        return (now + day + 8 * 60 * 60) * 1000
    if choice == "thisweek":
        return (now + 3 * day) * 1000

    return None

def menu_markup(chat_id: int) -> dict:
    draft = DRAFTS.get(chat_id, {})
    title = draft.get("title", "(none)")
    project = draft.get("project", "default")
    priority = draft.get("priority_label", "Normal")
    due = draft.get("due_label", "No due date")

    text = (
        f"🧾 Task Draft\n"
        f"• Title: {title}\n"
        f"• Project/List: {project}\n"
        f"• Priority: {priority}\n"
        f"• Due: {due}\n\n"
        f"Choose what to set:"
    )

    # Build buttons
    project_buttons = []
    if LIST_ROUTING:
        for k in list(LIST_ROUTING.keys())[:6]:
            project_buttons.append([{"text": f"📁 {k}", "callback_data": f"set_project:{k}"}])
    project_buttons.append([{"text": "📁 Default list", "callback_data": "set_project:default"}])

    priority_buttons = [[{"text": f"⭐ {label}", "callback_data": f"set_priority:{num}"}] for label, num in PRIORITIES]
    due_buttons = [[{"text": f"🗓 {label}", "callback_data": f"set_due:{val if val else 'none'}"}] for label, val in DUE_CHOICES]

    keyboard = []
    keyboard += [[{"text": "✏️ Set/Change Title", "callback_data": "ask_title"}]]
    keyboard += project_buttons
    keyboard += priority_buttons
    keyboard += due_buttons
    keyboard += [[{"text": "✅ Create Task", "callback_data": "confirm_create"}],
                 [{"text": "🗑 Cancel", "callback_data": "cancel"}]]

    return {"text": text, "reply_markup": {"inline_keyboard": keyboard}}

@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/telegram")
async def telegram_webhook(req: Request):
    update = await req.json()

    # Handle button presses
    if "callback_query" in update:
        cq = update["callback_query"]
        callback_id = cq["id"]
        data = cq.get("data", "")
        message = cq.get("message", {})
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]

        answer_callback(callback_id)

        if chat_id not in DRAFTS:
            DRAFTS[chat_id] = {"title": "", "project": "default", "priority": 3, "priority_label": "Normal", "due": None, "due_label": "No due date"}

        draft = DRAFTS[chat_id]

        if data == "ask_title":
            draft["awaiting_title"] = True
            edit_message(chat_id, message_id, "Reply with the task title (just send a message).")
            return {"ok": True}

        if data.startswith("set_project:"):
            proj = data.split(":", 1)[1]
            draft["project"] = proj
            edit_message(chat_id, message_id, **menu_markup(chat_id))
            return {"ok": True}

        if data.startswith("set_priority:"):
            num = int(data.split(":", 1)[1])
            label = next((l for l, n in PRIORITIES if n == num), "Normal")
            draft["priority"] = num
            draft["priority_label"] = label
            edit_message(chat_id, message_id, **menu_markup(chat_id))
            return {"ok": True}

        if data.startswith("set_due:"):
            val = data.split(":", 1)[1]
            if val == "none":
                draft["due"] = None
                draft["due_label"] = "No due date"
            else:
                draft["due"] = val
                draft["due_label"] = next((l for l, v in DUE_CHOICES if v == val), val)
            edit_message(chat_id, message_id, **menu_markup(chat_id))
            return {"ok": True}

        if data == "confirm_create":
            title = (draft.get("title") or "").strip()
            if not title:
                edit_message(chat_id, message_id, "❌ Title is empty. Tap “Set/Change Title”.")
                return {"ok": True}

            list_id = resolve_list_id(None if draft.get("project") == "default" else draft.get("project"))
            due_ms = due_choice_to_epoch_ms(draft.get("due"))
            prio = draft.get("priority")

            try:
                task = create_clickup_task(
                    list_id=list_id,
                    name=title[:200],
                    description=draft.get("description", ""),
                    due_date_ms=due_ms,
                    priority=prio,
                )
                edit_message(chat_id, message_id, f"✅ Created in ClickUp: {task.get('name')}")
                DRAFTS.pop(chat_id, None)
            except Exception as e:
                edit_message(chat_id, message_id, f"❌ Failed to create task: {e}")
            return {"ok": True}

        if data == "cancel":
            DRAFTS.pop(chat_id, None)
            edit_message(chat_id, message_id, "🗑 Cancelled.")
            return {"ok": True}

        return {"ok": True}

    # Handle normal messages
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if text in ["/start", "/new", "/task"]:
        DRAFTS[chat_id] = {"title": "", "project": "default", "priority": 3, "priority_label": "Normal", "due": None, "due_label": "No due date"}
        send_message(chat_id, **menu_markup(chat_id))
        return {"ok": True}

    # If bot is waiting for title input
    if chat_id in DRAFTS and DRAFTS[chat_id].get("awaiting_title"):
        DRAFTS[chat_id]["title"] = text
        DRAFTS[chat_id].pop("awaiting_title", None)
        send_message(chat_id, **menu_markup(chat_id))
        return {"ok": True}

    # Default behavior: treat any text as new task title and show menu
    DRAFTS[chat_id] = {
        "title": text,
        "description": text,
        "project": "default",
        "priority": 3,
        "priority_label": "Normal",
        "due": None,
        "due_label": "No due date",
    }
    send_message(chat_id, **menu_markup(chat_id))
    return {"ok": True}
