"""GUI routes for autosearches, hidden objects, notifications and conversion."""

import asyncio
import re

from fastapi import APIRouter, Request

from app.hh_mobile_transport import MobileAPIError, mobile_request
from app.instances import bot
from app.mobile_autosearch import delete_autosearch, fetch_autosearches, update_autosearch
from app.mobile_discovery import fetch_bell_notifications, fetch_hidden, restore_hidden
from app.storage import get_applied_list, get_interviews_list


router = APIRouter()


@router.get("/api/account/{idx}/vacancy_contact/{vid}")
async def api_vacancy_contact(idx: int, vid: str):
    """Read vacancy details only. No contact-unlock or chat-creation calls."""
    if not re.fullmatch(r"[0-9]{1,20}", vid):
        return {"ok": False, "error": "Некорректный ID вакансии"}
    acc = _acc(idx)
    if not acc:
        return {"ok": False, "error": "Аккаунт не найден"}
    try:
        data = await asyncio.to_thread(mobile_request, acc, "GET", f"/vacancies/{vid}")
    except Exception:
        return {"ok": False, "error": "Не удалось проверить контакты в HH"}
    if not isinstance(data, dict) or str(data.get("id") or "") != vid:
        return {"ok": False, "error": "HH не вернул подтверждённые данные вакансии"}
    contacts = data.get("contacts")
    available = isinstance(contacts, dict) and bool(contacts.get("email") or contacts.get("phones"))
    return {"ok": True, "contact_available": available,
            "url": f"https://hh.ru/vacancy/{vid}",
            "message": "Есть опубликованные контакты. Доступность чата проверьте на странице HH."
            if available else "Контакты не получены. Это не доказывает, что чат недоступен; проверьте страницу HH."}


def _acc(idx: int):
    return bot._get_apply_acc(idx) if idx >= 0 else None


def _state(idx: int):
    if 0 <= idx < len(bot.account_states):
        return bot.account_states[idx]
    return bot.temp_states.get(idx - len(bot.account_states))


async def _run(func, *args):
    try:
        return await asyncio.get_event_loop().run_in_executor(None, func, *args)
    except MobileAPIError as exc:
        return {"ok": False, "error": f"HH API: {exc.status_code}"}


@router.get("/api/account/{idx}/autosearches")
async def api_autosearches(idx: int):
    acc = _acc(idx)
    return await _run(fetch_autosearches, acc) if acc else {"ok": False, "error": "Аккаунт не найден"}


@router.put("/api/account/{idx}/autosearches/{search_id}")
async def api_autosearch_update(idx: int, search_id: str, request: Request):
    acc = _acc(idx)
    if not acc:
        return {"ok": False, "error": "Аккаунт не найден"}
    body = await request.json()
    return await _run(
        lambda: update_autosearch(acc, search_id, name=body.get("name"),
                                  email_subscription=body.get("email_subscription")))


@router.delete("/api/account/{idx}/autosearches/{search_id}")
async def api_autosearch_delete(idx: int, search_id: str):
    acc = _acc(idx)
    return await _run(delete_autosearch, acc, search_id) if acc else {"ok": False, "error": "Аккаунт не найден"}


@router.get("/api/account/{idx}/hidden")
async def api_hidden(idx: int):
    acc = _acc(idx)
    return await _run(fetch_hidden, acc) if acc else {"ok": False, "error": "Аккаунт не найден"}


@router.delete("/api/account/{idx}/hidden/{kind}/{object_id}")
async def api_hidden_restore(idx: int, kind: str, object_id: str):
    acc = _acc(idx)
    return await _run(restore_hidden, acc, kind, object_id) if acc else {"ok": False, "error": "Аккаунт не найден"}


@router.get("/api/account/{idx}/bell_notifications")
async def api_bell_notifications(idx: int):
    acc = _acc(idx)
    return await _run(fetch_bell_notifications, acc) if acc else {"ok": False, "error": "Аккаунт не найден"}


@router.get("/api/account/{idx}/conversion")
async def api_conversion(idx: int):
    acc = _acc(idx)
    if not acc:
        return {"ok": False, "error": "Аккаунт не найден"}
    name = str(acc.get("name") or "")
    applied = [x for x in get_applied_list(5000) if x.get("account") == name]
    interviews = get_interviews_list(acc=name, limit=5000)
    invited = {
        str(x.get("vacancy_id") or x.get("vid") or "") for x in interviews
        if x.get("vacancy_id") or x.get("vid")
    }
    applied_ids = {str(x.get("vacancy_id") or "") for x in applied}
    # Old interview records did not persist vacancy_id. The account worker's
    # HH counter is authoritative in that case; use exact local matching only
    # when it is available.
    matched_local = len(applied_ids & invited)
    state = _state(idx)
    matched = max(matched_local, int(getattr(state, "hh_interviews", 0) or 0))
    matched = min(matched, len(applied_ids))
    return {
        "ok": True, "applied": len(applied_ids), "interviews": matched,
        "conversion_percent": round(matched * 100 / len(applied_ids), 1) if applied_ids else 0,
    }
