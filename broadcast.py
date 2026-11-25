"""Рассылка постов по каналам через бота.

Пишете боту в личку пост (текст, фото, видео, альбом, файл или пересланное сообщение) —
бот спрашивает, куда отправить: во все 30 каналов, только в русские (15) или только в казахские (15).
После отправки под отчётом есть кнопки: удалить пост из всех каналов сейчас или через 24 часа.
Принимает посты только от администраторов каналов. Запускается по расписанию в GitHub Actions (broadcast.yml), раз в ~10 минут.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

from quizbot import API, CHANNELS  # тот же токен и тот же список каналов, что у викторин

RU = [c for c, v in CHANNELS.items() if v[1] == "ru"]
KK = [c for c, v in CHANNELS.items() if v[1] == "kk"]
TARGETS = {"all": RU + KK, "ru": RU, "kk": KK}
TITLES = {"all": "все 30 каналов", "ru": "русские каналы (15)", "kk": "казахские каналы (15)"}

cli = httpx.Client(timeout=70)
KZ = dt.timezone(dt.timedelta(hours=5))
STATE = Path(__file__).resolve().parent / "broadcasts.json"   # что куда разослано и когда удалить
AUTO_HOURS = 24
MARKUP = {}  # (chat_id, message_id) -> кнопки-ссылки исходного сообщения


def source_markup(chat_id: int, mid: int):
    """Кнопки-ссылки под исходным постом (copyMessage сам их не переносит)."""
    if (chat_id, mid) in MARKUP:
        return MARKUP[(chat_id, mid)]
    try:  # бот перезапускался — узнаём кнопки через временную пересылку и сразу её удаляем
        f = tg("forwardMessage", chat_id=chat_id, from_chat_id=chat_id, message_id=mid, disable_notification=True)
        tg("deleteMessage", chat_id=chat_id, message_id=f["message_id"])
        return f.get("reply_markup")
    except Exception as e:
        print("markup", e)
        return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st: dict):
    """Сохраняем в репозиторий, чтобы после перезапуска бот помнил посты и время удаления."""
    week = time.time() - 7 * 86400
    for k in [k for k, v in st.items() if v.get("sent", 0) < week]:
        del st[k]
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=0), encoding="utf-8")


def tg(method: str, **params):
    for _ in range(5):
        try:
            d = cli.post(API + method, json=params).json()
        except httpx.HTTPError:
            time.sleep(3)
            continue
        if d.get("ok"):
            return d["result"]
        if d.get("error_code") == 429:
            time.sleep(d.get("parameters", {}).get("retry_after", 5) + 1)
            continue
        raise RuntimeError(d.get("description", "error"))
    raise RuntimeError("network")


def admins() -> set:
    ids = set()
    for ch in CHANNELS:
        try:
            for m in tg("getChatAdministrators", chat_id="@" + ch):
                if not m["user"].get("is_bot"):
                    ids.add(m["user"]["id"])
        except Exception as e:
            print("admins", ch, e)
    return ids


def ask_target(chat_id: int, first: int, last: int):
    rng = f"{first}-{last}"
    kb = {"inline_keyboard": [
        [{"text": "Во все 30 каналов", "callback_data": f"b:all:{rng}"}],
        [{"text": "Русские (15)", "callback_data": f"b:ru:{rng}"},
         {"text": "Казахские (15)", "callback_data": f"b:kk:{rng}"}],
        [{"text": "Отмена", "callback_data": f"b:no:{rng}"}],
    ]}
    tg("sendMessage", chat_id=chat_id, text="Куда отправить этот пост?", reply_to_message_id=last,
       reply_markup=kb)


def kz_time(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, KZ).strftime("%d.%m %H:%M")


def del_kb(key: str, scheduled: bool) -> dict:
    rows = [[{"text": "Удалить сейчас из всех каналов", "callback_data": f"d:ask:{key}"}]]
    if scheduled:
        rows.append([{"text": "Отменить автоудаление", "callback_data": f"d:keep:{key}"}])
    else:
        rows.append([{"text": f"Удалить через {AUTO_HOURS} ч", "callback_data": f"d:auto:{key}"}])
    return {"inline_keyboard": rows}


def send(st: dict, chat_id: int, target: str, first: int, last: int, note_id: int):
    chans = TARGETS[target]
    tg("editMessageText", chat_id=chat_id, message_id=note_id, text=f"Отправляю в {TITLES[target]}…")
    ids = list(range(first, last + 1))
    kb = source_markup(chat_id, ids[0]) if len(ids) == 1 else None
    extra = {"reply_markup": kb} if kb else {}
    ok, bad, sent = 0, [], {}
    for ch in chans:
        try:
            if len(ids) == 1:
                r = tg("copyMessage", chat_id="@" + ch, from_chat_id=chat_id, message_id=ids[0], **extra)
                sent[ch] = [r["message_id"]]
            else:
                r = tg("copyMessages", chat_id="@" + ch, from_chat_id=chat_id, message_ids=ids)
                sent[ch] = [x["message_id"] for x in r]
            ok += 1
        except Exception as e:
            bad.append(f"@{ch}: {e}")
        time.sleep(0.7)
    key = f"{chat_id}_{note_id}"
    text = f"Готово: отправлено в {ok} из {len(chans)} ({TITLES[target]})."
    if bad:
        text += "\nНе получилось:\n" + "\n".join(bad)
    st[key] = {"chat": chat_id, "note": note_id, "sent": time.time(), "target": target, "msgs": sent,
               "del_at": None, "text": text[:3000]}
    save_state(st)
    tg("editMessageText", chat_id=chat_id, message_id=note_id, text=text[:3500],
       reply_markup=del_kb(key, False) if sent else {"inline_keyboard": []})


def delete_post(st: dict, key: str, why: str = ""):
    b = st.get(key)
    if not b or b.get("deleted"):
        return
    ok, bad = 0, []
    for ch, ids in b["msgs"].items():
        try:
            tg("deleteMessages", chat_id="@" + ch, message_ids=ids)
            ok += 1
        except Exception as e:
            bad.append(f"@{ch}: {e}")
        time.sleep(0.5)
    b["deleted"] = True
    b["del_at"] = None
    save_state(st)
    text = f"{b.get('text', '')}\n\nУдалено{why} из {ok} из {len(b['msgs'])} каналов."
    if bad:
        text += "\nНе удалось (Telegram даёт удалять только посты младше 48 часов):\n" + "\n".join(bad)
    try:
        tg("editMessageText", chat_id=b["chat"], message_id=b["note"], text=text[:4000],
           reply_markup={"inline_keyboard": []})
    except Exception as e:
        print("edit after delete", e)


def on_delete_button(st: dict, action: str, key: str):
    b = st.get(key)
    if not b or b.get("deleted"):
        return
    chat, mid = b["chat"], b["note"]
    base = b.get("text", "")
    if action == "ask":
        tg("editMessageText", chat_id=chat, message_id=mid, text=base + "\n\nУдалить этот пост из всех каналов?",
           reply_markup={"inline_keyboard": [[{"text": "Да, удалить", "callback_data": f"d:yes:{key}"},
                                              {"text": "Нет", "callback_data": f"d:back:{key}"}]]})
    elif action == "yes":
        delete_post(st, key)
    elif action == "auto":
        b["del_at"] = time.time() + AUTO_HOURS * 3600
        save_state(st)
        tg("editMessageText", chat_id=chat, message_id=mid,
           text=base + f"\n\nАвтоудаление: {kz_time(b['del_at'])} (время Астаны).", reply_markup=del_kb(key, True))
    elif action == "keep":
        b["del_at"] = None
        save_state(st)
        tg("editMessageText", chat_id=chat, message_id=mid, text=base, reply_markup=del_kb(key, False))
    elif action == "back":
        extra = f"\n\nАвтоудаление: {kz_time(b['del_at'])} (время Астаны)." if b.get("del_at") else ""
        tg("editMessageText", chat_id=chat, message_id=mid, text=base + extra,
           reply_markup=del_kb(key, bool(b.get("del_at"))))


HELP = ("Пришлите сюда пост — текст, фото, видео, альбом, файл или пересланное сообщение. "
        "Я спрошу, куда его отправить: во все 30 каналов, только в русские (15) или только в казахские (15). "
        "После отправки пост можно удалить из всех каналов кнопкой — сразу или через 24 часа. "
        "Я проверяю сообщения примерно раз в 10 минут, поэтому отвечаю не мгновенно.")


def handle(st: dict, allowed: set, u: dict, groups: dict) -> set:
    if "callback_query" in u:
        q = u["callback_query"]
        try:
            tg("answerCallbackQuery", callback_query_id=q["id"])
        except Exception:
            pass  # кнопку нажали давно — ответ уже не нужен
        if q["from"]["id"] not in allowed:
            return allowed
        kind, target, rng = q["data"].split(":")
        if kind == "d":
            on_delete_button(st, target, rng)
            return allowed
        first, last = map(int, rng.split("-"))
        note = q["message"]
        if target == "no":
            tg("editMessageText", chat_id=note["chat"]["id"], message_id=note["message_id"], text="Отменено.")
        else:
            tg("editMessageReplyMarkup", chat_id=note["chat"]["id"], message_id=note["message_id"],
               reply_markup={"inline_keyboard": []})
            send(st, note["chat"]["id"], target, first, last, note["message_id"])
        return allowed
    m = u.get("message")
    if not m or m["chat"]["type"] != "private":
        return allowed
    if m["from"]["id"] not in allowed:
        allowed = admins()
        if m["from"]["id"] not in allowed:
            tg("sendMessage", chat_id=m["chat"]["id"], text="Этот бот только для администраторов каналов.")
            return allowed
    if m.get("text", "").startswith("/"):
        tg("sendMessage", chat_id=m["chat"]["id"], text=HELP)
        return allowed
    if m.get("reply_markup"):
        MARKUP[(m["chat"]["id"], m["message_id"])] = m["reply_markup"]
    g = m.get("media_group_id")
    if g:
        cur = groups.setdefault(g, [m["chat"]["id"], m["message_id"], m["message_id"]])
        cur[1] = min(cur[1], m["message_id"])
        cur[2] = max(cur[2], m["message_id"])
    else:
        ask_target(m["chat"]["id"], m["message_id"], m["message_id"])
    return allowed


def main():
    """Один проход: разобрать накопившиеся сообщения и кнопки, выполнить автоудаления и выйти.
    Запускается по расписанию GitHub Actions (broadcast.yml), постоянно не работает."""
    st = load_state()
    allowed = None
    offset = None
    groups = {}
    while True:
        ups = tg("getUpdates", offset=offset, timeout=0, allowed_updates=["message", "callback_query"])
        if not ups:
            break
        if allowed is None:
            allowed = admins()
        for u in ups:
            offset = u["update_id"] + 1
            try:
                allowed = handle(st, allowed, u, groups)
            except Exception as e:
                print("update", e)
    for chat_id, first, last in groups.values():
        try:
            ask_target(chat_id, first, last)
        except Exception as e:
            print("album", e)
    for key, b in list(st.items()):
        if b.get("del_at") and not b.get("deleted") and time.time() >= b["del_at"]:
            try:
                delete_post(st, key, " по таймеру")
            except Exception as e:
                print("auto delete", e)
    if offset:
        tg("getUpdates", offset=offset, timeout=0)  # подтвердить обработанные
    save_state(st)
    print("done")


if __name__ == "__main__":
    main()
