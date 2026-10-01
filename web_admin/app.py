"""
Веб-админ-панель: FastAPI + Tabler.
Управление ботом, стандартами, пользователями, аналитикой и логами.
"""
import hashlib
import hmac
import json
import os
import shutil
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Form, Query, HTTPException, Header
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import config
from storage.settings import settings_store
from telegram.handler import rebuild_chat_owner_map
from admin_bot.bot import restart_admin_bot
from web_admin.ai_matcher import find_matches

class TemplateBody(BaseModel):
    question: str
    answer: str


app = FastAPI(title="AI Bot Admin")

templates = Jinja2Templates(directory="web_admin/templates")

ADMIN_LOGIN = config.web_admin_login
ADMIN_PASS_HASH = hashlib.sha256(
    config.web_admin_password.encode()
).hexdigest()

_telethon_getter = None


def set_telethon_getter(getter) -> None:
    global _telethon_getter
    _telethon_getter = getter


def _check_auth(request: Request) -> bool:
    return request.session.get("admin_authenticated", False)


def _render(request: Request, name: str, **ctx):
    ctx.setdefault("active_page", "")
    ctx.setdefault("page_title", "")
    ctx.setdefault("flash", "")
    return templates.TemplateResponse(request, name, ctx)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ("/login", "/static") or path.startswith("/static"):
            return await call_next(request)
        if path.startswith("/webapp") or path.startswith("/api/webapp"):
            return await call_next(request)
        if path in ("/", "/app") or path.startswith("/auth/"):
            return await call_next(request)
        if path in ("/api/templates/search", "/api/templates/all"):
            return await call_next(request)
        if not _check_auth(request):
            return RedirectResponse("/login")
        return await call_next(request)


app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=config.web_secret)


# ── Auth ───────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login_submit(request: Request, login: str = Form(...), password: str = Form(...)):
    if login == ADMIN_LOGIN and hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASS_HASH:
        request.session["admin_authenticated"] = True
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": "Неверный логин или пароль"})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Telegram Login (public website) ────────────────────────────

def _verify_telegram_login(params: dict, bot_token: str) -> Optional[dict]:
    if not params.get("hash") or not params.get("auth_date") or not params.get("id"):
        return None
    check_hash = params.get("hash")
    data_check = []
    for key in sorted(params.keys()):
        if key != "hash":
            data_check.append(f"{key}={params[key]}")
    data_check_string = "\n".join(data_check)
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, check_hash):
        return None
    try:
        auth_date = int(params["auth_date"])
        if (datetime.now().timestamp() - auth_date) > 86400:
            return None
    except (ValueError, TypeError):
        return None
    return params


@app.get("/auth/telegram/callback")
async def telegram_login_callback(request: Request):
    bot_token = settings_store.get_bot_token() or config.admin_bot_token
    if not bot_token:
        return RedirectResponse("/?error=no_bot", status_code=302)
    user_data = _verify_telegram_login(dict(request.query_params), bot_token)
    if not user_data:
        return RedirectResponse("/?error=auth_failed", status_code=302)
    request.session["authenticated"] = True
    request.session["tg_user_id"] = int(user_data["id"])
    first = user_data.get("first_name", "")
    last = user_data.get("last_name", "")
    request.session["tg_user_name"] = f"{first} {last}".strip() or first or "User"
    settings_store.record_event("website_login", int(user_data["id"]))
    return RedirectResponse("/app", status_code=302)


# ── Public API (website) ───────────────────────────────────────

class SearchBody(BaseModel):
    query: str


@app.post("/api/templates/search")
async def search_templates(body: SearchBody):
    all_tpls = []
    for uid in settings_store.get_all_user_ids():
        for tpl in settings_store.get_user_templates(uid):
            tpl["owner_id"] = uid
            all_tpls.append(tpl)
    matches = find_matches(body.query, all_tpls, top_k=10)
    return JSONResponse(matches)


@app.get("/api/templates/all")
async def get_all_templates():
    all_tpls = []
    seen = set()
    for uid in settings_store.get_all_user_ids():
        for tpl in settings_store.get_user_templates(uid):
            key = (tpl["id"], uid)
            if key not in seen:
                seen.add(key)
                all_tpls.append(tpl)
    return JSONResponse(all_tpls)


# ── Dashboard ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {
        "bot_username": config.bot_username,
    })


@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    if not request.session.get("authenticated", False):
        return RedirectResponse("/", status_code=302)
    user_id = request.session.get("tg_user_id")
    user_name = request.session.get("tg_user_name", "")
    if not request.session.get("visit_tracked", False):
        settings_store.record_event("website_visit", user_id)
        request.session["visit_tracked"] = True
    return templates.TemplateResponse(request, "app_page.html", {
        "user_id": user_id,
        "user_name": user_name,
    })


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    today = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
    stats = settings_store.get_stats_summary(period_start=today)
    total_stats = settings_store.get_stats_summary()
    user_count = len(settings_store.get_activated_credentials())
    sub_count = len(settings_store.get_subscribers())
    recent_errors = settings_store.get_error_logs()[-10:]
    recent_errors.reverse()
    error_today = sum(1 for e in settings_store.get_error_logs() if e.get("ts", "") >= today)
    return _render(
        request, "dashboard.html",
        active_page="dashboard", page_title="Dashboard",
        stats=stats, total_stats=total_stats,
        user_count=user_count, sub_count=sub_count,
        error_today=error_today,
        recent_errors=recent_errors,
        assistant_on=settings_store.assistant_enabled,
    )


# ── Bot connection ─────────────────────────────────────────────

@app.get("/bot", response_class=HTMLResponse)
async def bot_page(request: Request, flash: str = "", flash_type: str = "info"):
    creds = settings_store.get_all_credentials()
    current_token = settings_store.get_bot_token() or ""
    return _render(
        request, "bot.html",
        active_page="bot", page_title="Подключение бота",
        config=config, creds=creds,
        assistant_on=settings_store.assistant_enabled,
        auto_reply=config.auto_reply,
        current_token=current_token,
        flash=flash,
        flash_type=flash_type,
    )


@app.post("/bot/toggle-assistant")
async def toggle_assistant(request: Request):
    settings_store.assistant_enabled = not settings_store.assistant_enabled
    state = "включён" if settings_store.assistant_enabled else "выключен"
    notify_token = settings_store.get_bot_token() or config.admin_bot_token
    if notify_token and config.admin_user_id:
        try:
            from aiogram import Bot
            notifier = Bot(token=notify_token)
            await notifier.send_message(
                config.admin_user_id,
                f"🤖 Помощник {state}",
            )
            await notifier.session.close()
        except Exception:
            pass
    return RedirectResponse("/bot", status_code=302)


@app.post("/bot/connect-token")
async def connect_bot_token(request: Request, bot_token: str = Form(...)):
    token = bot_token.strip()
    if not token:
        return RedirectResponse("/bot?flash=Токен не может быть пустым&flash_type=danger", status_code=302)
    settings_store.save_bot_token(token)
    if _telethon_getter:
        await restart_admin_bot(_telethon_getter)
    return RedirectResponse("/bot?flash=Бот подключён&flash_type=success", status_code=302)


# ── Standards ──────────────────────────────────────────────────

@app.get("/standards", response_class=HTMLResponse)
async def standards_page(request: Request):
    greetings = settings_store.get_standard_greetings()
    thanks = settings_store.get_standard_thanks()
    no_action = settings_store.get_standard_no_action_phrases()
    return _render(
        request, "standards.html",
        active_page="standards", page_title="Стандартные значения",
        greetings=greetings, thanks=thanks, no_action=no_action,
    )


@app.post("/standards/greeting/add")
async def add_std_greeting(request: Request, phrase: str = Form(...), reply: str = Form(...)):
    settings_store.add_standard_greeting(phrase, reply)
    return RedirectResponse("/standards", status_code=302)


@app.post("/standards/greeting/delete")
async def del_std_greeting(request: Request, greeting_id: int = Form(...)):
    settings_store.remove_standard_greeting(greeting_id)
    return RedirectResponse("/standards", status_code=302)


@app.post("/standards/thanks/add")
async def add_std_thanks(request: Request, phrase: str = Form(...)):
    settings_store.add_standard_thanks(phrase)
    return RedirectResponse("/standards", status_code=302)


@app.post("/standards/thanks/delete")
async def del_std_thanks(request: Request, thanks_id: int = Form(...)):
    settings_store.remove_standard_thanks(thanks_id)
    return RedirectResponse("/standards", status_code=302)


@app.post("/standards/no-action/add")
async def add_std_no_action(request: Request, phrase: str = Form(...)):
    settings_store.add_standard_no_action_phrase(phrase)
    return RedirectResponse("/standards", status_code=302)


@app.post("/standards/no-action/delete")
async def del_std_no_action(request: Request, phrase_id: int = Form(...)):
    settings_store.remove_standard_no_action_phrase(phrase_id)
    return RedirectResponse("/standards", status_code=302)


# ── Library ────────────────────────────────────────────────────

@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request, q: str = ""):
    items = settings_store.get_library()
    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i.get("title") or "").lower()]
    return _render(
        request, "library.html",
        active_page="library", page_title="Справочник",
        items=items, search=q,
    )


@app.post("/library/add")
async def add_library(request: Request, title: str = Form(...), text: str = Form(...)):
    settings_store.add_library_text(title, text)
    return RedirectResponse("/library", status_code=302)


@app.post("/library/delete")
async def del_library(request: Request, lib_id: int = Form(...)):
    settings_store.remove_library_text(lib_id)
    return RedirectResponse("/library", status_code=302)


@app.get("/library/export")
async def export_library(request: Request):
    items = settings_store.get_library()
    return JSONResponse(items)


# ── Users ──────────────────────────────────────────────────────

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, q: str = ""):
    all_creds = settings_store.get_all_credentials()
    if q:
        ql = q.lower()
        all_creds = [
            c for c in all_creds
            if ql in str(c.get("phone", "")) or ql in str(c.get("user_id", ""))
        ]
    users_with_stats = []
    for c in all_creds:
        uid = c.get("user_id", 0)
        stats = settings_store.get_user_stats(uid)
        chats = settings_store.get_user_watchlist(uid)
        groups_count = sum(1 for chat in chats.values() if chat.get("type") == "group")
        clients_count = sum(1 for chat in chats.values() if chat.get("type") == "client")
        tasks_count = len(settings_store.get_user_tasks(uid))
        users_with_stats.append({**c, "groups_count": groups_count, "clients_count": clients_count, "tasks_count": tasks_count, **stats})
    return _render(
        request, "users.html",
        active_page="users", page_title="Пользователи",
        users=users_with_stats, search=q,
    )


@app.post("/users/deactivate")
async def deactivate_user(request: Request, user_id: int = Form(...)):
    settings_store.deactivate_user(user_id)
    return RedirectResponse("/users", status_code=302)


@app.post("/users/clear-data")
async def clear_user_data(request: Request, user_id: int = Form(...)):
    settings_store.clear_user_data(user_id)
    rebuild_chat_owner_map()
    return RedirectResponse("/users", status_code=302)


@app.post("/users/delete-account")
async def delete_user_account(request: Request, user_id: int = Form(...)):
    settings_store.delete_user_account(user_id)
    rebuild_chat_owner_map()
    return RedirectResponse("/users", status_code=302)


@app.post("/users/reset-stats")
async def reset_user_stats(request: Request, user_id: int = Form(...)):
    settings_store.clear_user_analytics(user_id)
    return RedirectResponse("/users", status_code=302)


# ── Subscribers ────────────────────────────────────────────────

@app.get("/subscribers", response_class=HTMLResponse)
async def subscribers_page(request: Request, q: str = "", status: str = ""):
    subs = settings_store.get_subscribers()
    if q:
        ql = q.lower()
        subs = [
            s for s in subs
            if ql in (s.get("name") or "").lower()
            or ql in (s.get("username") or "").lower()
            or ql in str(s.get("telegram_id", ""))
        ]
    if status:
        subs = [s for s in subs if s.get("status") == status]
    return _render(
        request, "subscribers.html",
        active_page="subscribers", page_title="Подписчики",
        subs=subs, search=q, filter_status=status,
    )


@app.post("/subscribers/status")
async def update_sub_status(request: Request, telegram_id: int = Form(...), status: str = Form(...)):
    settings_store.update_subscriber_status(telegram_id, status)
    return RedirectResponse("/subscribers", status_code=302)


@app.post("/subscribers/delete")
async def delete_subscriber(request: Request, telegram_id: int = Form(...)):
    settings_store.remove_subscriber(telegram_id)
    return RedirectResponse("/subscribers", status_code=302)


@app.get("/subscribers/export")
async def export_subscribers(request: Request):
    subs = settings_store.get_subscribers()
    return JSONResponse(subs)


# ── Analytics ──────────────────────────────────────────────────

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    period: str = "today",
    start: str = "",
    end: str = "",
):
    now = datetime.now()
    if period == "today":
        ps = now.replace(hour=0, minute=0, second=0).isoformat()
        pe = None
    elif period == "yesterday":
        y = now - timedelta(days=1)
        ps = y.replace(hour=0, minute=0, second=0).isoformat()
        pe = y.replace(hour=23, minute=59, second=59).isoformat()
    elif period == "7days":
        ps = (now - timedelta(days=7)).isoformat()
        pe = None
    elif period == "30days":
        ps = (now - timedelta(days=30)).isoformat()
        pe = None
    elif period == "month":
        ps = now.replace(day=1, hour=0, minute=0, second=0).isoformat()
        pe = None
    elif period == "custom" and start:
        ps = start
        pe = end or None
    else:
        ps = None
        pe = None

    events = settings_store.get_analytics(period_start=ps, period_end=pe)
    summary = {
        "errors": sum(1 for e in events if e.get("type") == "error"),
        "website_logins": sum(1 for e in events if e.get("type") in ("website_login", "website_visit")),
        "incoming": sum(1 for e in events if e.get("type") == "incoming"),
        "auto_replies": sum(1 for e in events if e.get("type") == "auto_reply"),
    }

    user_count = len(settings_store.get_activated_credentials())
    sub_count = len(settings_store.get_subscribers())

    activity_data = []
    if period in ("7days", "30days"):
        days = 7 if period == "7days" else 30
        for i in range(days - 1, -1, -1):
            day = now - timedelta(days=i)
            day_start = day.replace(hour=0, minute=0, second=0).isoformat()
            day_end = day.replace(hour=23, minute=59, second=59).isoformat()
            day_events = [e for e in events if day_start <= e.get("ts", "") <= day_end]
            activity_data.append({
                "date": day.strftime("%d.%m"),
                "logins": sum(1 for e in day_events if e.get("type") in ("website_login", "website_visit")),
                "incoming": sum(1 for e in day_events if e.get("type") == "incoming"),
            })

    return _render(
        request, "analytics.html",
        active_page="analytics", page_title="Аналитика",
        period=period, start=start, end=end or "",
        summary=summary,
        user_count=user_count,
        sub_count=sub_count,
        activity_data=activity_data,
    )


@app.get("/analytics/export")
async def export_analytics(request: Request, period: str = "30days"):
    now = datetime.now()
    if period == "7days":
        ps = (now - timedelta(days=7)).isoformat()
    elif period == "30days":
        ps = (now - timedelta(days=30)).isoformat()
    else:
        ps = None
    events = settings_store.get_analytics(period_start=ps)
    return JSONResponse(events)


# ── Logs ───────────────────────────────────────────────────────

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, level: str = "", module: str = ""):
    logs = settings_store.get_error_logs(level=level or None, module=module or None)
    logs.reverse()
    modules = set(l.get("module", "") for l in settings_store.get_error_logs())
    return _render(
        request, "logs.html",
        active_page="logs", page_title="Ошибки и логи",
        logs=logs, filter_level=level, filter_module=module,
        modules=sorted(modules),
    )


@app.post("/logs/resolve")
async def resolve_log(request: Request, log_id: int = Form(...)):
    settings_store.mark_error_resolved(log_id)
    return RedirectResponse("/logs", status_code=302)


@app.post("/logs/clear")
async def clear_logs(request: Request, days: int = Form(30)):
    removed = settings_store.clear_old_error_logs(days)
    return RedirectResponse("/logs", status_code=302)


@app.get("/logs/export")
async def export_logs(request: Request):
    logs = settings_store.get_error_logs()
    safe_logs = []
    for l in logs:
        safe = {k: v for k, v in l.items() if k not in ("details",)}
        safe_logs.append(safe)
    return JSONResponse(safe_logs)


# ── Settings ───────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user_count = len(settings_store.get_all_user_ids())
    return _render(
        request, "settings.html",
        active_page="settings", page_title="Системные настройки",
        user_count=user_count,
    )


@app.post("/settings/backup")
async def create_backup(request: Request):
    src = settings_store.filepath
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = f"{src}.backup_{ts}"
    shutil.copy2(src, dst)
    return RedirectResponse("/settings", status_code=302)


@app.post("/settings/clear-analytics")
async def clear_analytics(request: Request):
    settings_store.clear_all_analytics()
    return RedirectResponse("/settings", status_code=302)


@app.post("/settings/clear-logs")
async def clear_all_logs(request: Request):
    settings_store.clear_all_error_logs()
    return RedirectResponse("/settings", status_code=302)


# ── Web App (Telegram) ─────────────────────────────────────────

def _verify_telegram_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Проверка Telegram WebApp initData. Возвращает данные пользователя или None."""
    if not init_data or not bot_token:
        return None
    try:
        parsed = parse_qs(init_data)
        received_hash = parsed.get("hash", [None])[0]
        if not received_hash:
            return None
        data_check = []
        for key in sorted(parsed.keys()):
            if key != "hash":
                val = parsed[key][0]
                data_check.append(f"{key}={val}")
        data_check_string = "\n".join(data_check)
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        user_json = parsed.get("user", [None])[0]
        if user_json:
            return json.loads(user_json)
    except Exception:
        pass
    return None


def _verify_write_auth(request: Request, user_id: int, init_data_header: Optional[str]) -> bool:
    bot_token = settings_store.get_bot_token() or config.admin_bot_token
    if init_data_header:
        user_data = _verify_telegram_init_data(init_data_header, bot_token)
        if user_data and user_data.get("id") == user_id:
            return True
    if request.session.get("authenticated") and request.session.get("tg_user_id") == user_id:
        return True
    return False


@app.get("/webapp/{user_id}", response_class=HTMLResponse)
async def webapp_page(request: Request, user_id: int):
    return templates.TemplateResponse(request, "webapp.html", {"user_id": user_id})


@app.get("/api/webapp/{user_id}/templates")
async def webapp_templates(user_id: int):
    tpls = settings_store.get_user_templates(user_id)
    return JSONResponse(tpls)


@app.post("/api/webapp/{user_id}/templates")
async def webapp_add_template(
    request: Request,
    user_id: int,
    body: TemplateBody,
    x_telegram_init_data: str = Header(None, alias="X-Telegram-Init-Data"),
):
    if not _verify_write_auth(request, user_id, x_telegram_init_data):
        raise HTTPException(status_code=403, detail="Unauthorized")
    tpl = settings_store.add_user_template(user_id, body.question, body.answer)
    return JSONResponse(tpl)


@app.put("/api/webapp/{user_id}/templates/{template_id}")
async def webapp_update_template(
    request: Request,
    user_id: int,
    template_id: int,
    body: TemplateBody,
    x_telegram_init_data: str = Header(None, alias="X-Telegram-Init-Data"),
):
    if not _verify_write_auth(request, user_id, x_telegram_init_data):
        raise HTTPException(status_code=403, detail="Unauthorized")
    ok = settings_store.update_user_template(user_id, template_id, body.question, body.answer)
    if not ok:
        raise HTTPException(status_code=404, detail="Template not found")
    return JSONResponse({"id": template_id, "question": body.question.strip(), "answer": body.answer.strip()})


@app.delete("/api/webapp/{user_id}/templates/{template_id}")
async def webapp_delete_template(
    request: Request,
    user_id: int,
    template_id: int,
    x_telegram_init_data: str = Header(None, alias="X-Telegram-Init-Data"),
):
    if not _verify_write_auth(request, user_id, x_telegram_init_data):
        raise HTTPException(status_code=403, detail="Unauthorized")
    ok = settings_store.remove_user_template(user_id, template_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Template not found")
    return JSONResponse({"deleted": True})
