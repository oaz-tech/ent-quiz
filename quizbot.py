"""ЕНТ/ҰБТ 2027 — квиз-бот для 30 предметных каналов.

Каждый день сам выбирает для каждого канала 5 случайных разных времён (09:00–21:00 по Казахстану)
и публикует викторины (Telegram quiz) в формате ЕНТ. Никаких согласований — всё автоматически.

Как устроены задания:
- Основа составляется на русском (Claude), второй проход заново решает вопрос — если ответ не совпал, вопрос выбрасывается.
- Для казахского канала того же предмета эти же задания переводятся на казахский.
- Казахский язык, русский язык, литература и қазақ әдебиеті — у каждого канала свои, отдельные задания.
- Часть заданий (история, география, биология, литература…) — «по картинке»: фото с Wikimedia Commons,
  которое сначала проверяется (Claude смотрит на фото и подтверждает, что там именно нужный объект).

Запуск:            python3 quizbot.py            (работает постоянно)
Проверка прав:     python3 quizbot.py check
Сразу один квиз:   python3 quizbot.py now ent_math_ru
Заполнить запас:   python3 quizbot.py fill
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import zlib
from pathlib import Path

import anthropic
import httpx

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs"
LOG.mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG / "quizbot.log", encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("quiz")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не писать токен в лог


def _load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")
_load_env(ROOT.parent / "entbot" / "tokens.txt")  # ANTHROPIC_API_KEY берём из основного бота

_raw_tok = os.environ.get("QUIZ_BOT_TOKEN", "")
_m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", _raw_tok)
TOKEN = _m.group(0) if _m else _raw_tok.strip()
_raw_key = os.environ.get("ANTHROPIC_API_KEY", "")
_k = re.search(r"sk-ant-[A-Za-z0-9_-]+", _raw_key)
os.environ["ANTHROPIC_API_KEY"] = _k.group(0) if _k else _raw_key.strip()
ROOT_STATE = ROOT
API = f"https://api.telegram.org/bot{TOKEN}/"
MODEL = os.environ.get("QUIZ_MODEL", "claude-sonnet-5-5")
KZ = dt.timezone(dt.timedelta(hours=5))   # Казахстан, единое время UTC+5
PER_DAY = int(os.environ.get("QUIZ_PER_DAY", "5"))
DAY_START, DAY_END = (9, 0), (20, 45)
BLOCK_GAP_SEC = int(os.environ.get("BLOCK_GAP_SEC", "30"))
MIN_GAP_MIN = 75
IMG_DIR = ROOT / "images"
IMG_DIR.mkdir(exist_ok=True)
UA = {"User-Agent": "ENTQuizBot/1.0 (https://t.me/EHTREGIONKZ) python-httpx/0.27"}

# ---------- предметы ----------
# picture: доля заданий «по картинке» (0 — без картинок)
SUBJECTS = {
    "mathliteracy": dict(ru="Математическая грамотность", kk="Математикалық сауаттылық", picture=0,
        scope="логика, проценты, пропорции, чтение таблиц и графиков, задачи из жизни, без сложных формул"),
    "reading": dict(ru="Грамотность чтения", kk="Оқу сауаттылығы", picture=0,
        scope="короткий текст + вопрос на понимание: главная мысль, вывод, факт/мнение, значение слова в контексте"),
    "kzhistory": dict(ru="История Казахстана", kk="Қазақстан тарихы", picture=0.35,
        scope="от древности до независимости: саки, гунны, тюрки, Золотая Орда, Казахское ханство, ханы и бии, "
              "восстания, Алаш, советский период, независимость; даты, личности, памятники, причины и следствия"),
    "math": dict(ru="Математика", kk="Математика", picture=0,
        scope="профильная математика ЕНТ: уравнения, неравенства, функции, логарифмы, тригонометрия, производная, "
              "геометрия, прогрессии; задачи с числами, ответ числом"),
    "physics": dict(ru="Физика", kk="Физика", picture=0.15,
        scope="механика, термодинамика, электричество, магнетизм, оптика, атомная физика; задачи с вычислением; "
              "по картинке — приборы и учёные-физики"),
    "chemistry": dict(ru="Химия", kk="Химия", picture=0.15,
        scope="строение атома, периодический закон, реакции, растворы, неорганика, органика, расчёты по уравнениям; "
              "по картинке — лабораторная посуда, минералы, учёные-химики"),
    "biology": dict(ru="Биология", kk="Биология", picture=0.35,
        scope="клетка, генетика (задачи), ботаника, зоология, анатомия человека, экология, эволюция; "
              "по картинке — растения и животные (в т.ч. Казахстана), органоиды, учёные"),
    "geography": dict(ru="География", kk="География", picture=0.35,
        scope="физическая и экономическая география мира и Казахстана, климат, население, ресурсы; "
              "по картинке — природные объекты, города, достопримечательности"),
    "worldhistory": dict(ru="Всемирная история", kk="Дүниежүзі тарихы", picture=0.35,
        scope="древний мир, средние века, новое и новейшее время, мировые войны; даты, личности, памятники"),
    "english": dict(ru="Английский язык", kk="Ағылшын тілі", picture=0,
        scope="grammar (tenses, conditionals, passive, articles, prepositions), vocabulary, reading"),
    "informatics": dict(ru="Информатика", kk="Информатика", picture=0,
        scope="системы счисления, логика, алгоритмы, Python, базы данных, сети, кодирование информации"),
    "law": dict(ru="Основы права", kk="Құқық негіздері", picture=0,
        scope="Конституция РК, государство и право, гражданское, трудовое, семейное, административное, "
              "уголовное право, права человека"),
    # отдельные (не переводятся)
    "russian_ru": dict(ru="Русский язык", kk="", picture=0,
        scope="орфография, пунктуация, морфология, синтаксис, лексика, стилистика (для школ с русским языком обучения)"),
    "russian_kk": dict(ru="Русский язык (для казахских школ)", kk="Орыс тілі", picture=0,
        scope="русский язык для школ с казахским языком обучения: лексика, грамматика, падежи, глаголы, понимание текста"),
    "literature": dict(ru="Русская литература", kk="", picture=0.3,
        scope="русская литература школьной программы: авторы, произведения, герои, теория литературы; "
              "по картинке — портреты писателей"),
    "kazlit": dict(ru="Казахская литература", kk="Қазақ әдебиеті", picture=0.3,
        scope="ауыз әдебиеті, жыраулар, Абай, Ыбырай, Шоқан, Мағжан, Мұхтар Әуезов, XX ғасыр әдебиеті, әдебиет теориясы; "
              "суретпен — жазушылардың портреттері"),
    "kazakh_kk": dict(ru="Казахский язык", kk="Қазақ тілі", picture=0,
        scope="фонетика, лексика, морфология, синтаксис, тыныс белгілері (қазақ мектептері үшін)"),
    "kazakh_ru": dict(ru="Казахский язык (для русских школ)", kk="", picture=0,
        scope="казахский язык для школ с русским языком обучения: лексика, грамматика, падежи (септіктер), "
              "переводы, понимание короткого текста"),
}

# канал: (предмет, язык, режим)  режим: base — генерируем на русском; tr — переводим базу; own — свои задания
CHANNELS = {
    "ent_mathliteracy_ru": ("mathliteracy", "ru", "base"), "ubt_mathliteracy_kz": ("mathliteracy", "kk", "tr"),
    "ent_reading_ru": ("reading", "ru", "base"), "ubt_reading_kz": ("reading", "kk", "tr"),
    "ent_kzhistory_ru": ("kzhistory", "ru", "base"), "ubt_kzhistory_kz": ("kzhistory", "kk", "tr"),
    "ent_math_ru": ("math", "ru", "base"), "ubt_math_kz": ("math", "kk", "tr"),
    "ent_physics_ru": ("physics", "ru", "base"), "ubt_physics_kz": ("physics", "kk", "tr"),
    "ent_chemistry_ru": ("chemistry", "ru", "base"), "ubt_chemistry_kz": ("chemistry", "kk", "tr"),
    "ent_biology_ru": ("biology", "ru", "base"), "ubt_biology_kz": ("biology", "kk", "tr"),
    "ent_geography_ru": ("geography", "ru", "base"), "ubt_geography_kz": ("geography", "kk", "tr"),
    "ent_worldhistory_ru": ("worldhistory", "ru", "base"), "ubt_worldhistory_kz": ("worldhistory", "kk", "tr"),
    "ent_english_ru": ("english", "ru", "base"), "ubt_english_kz": ("english", "kk", "tr"),
    "ent_informatics_ru": ("informatics", "ru", "base"), "ubt_informatics_kz": ("informatics", "kk", "tr"),
    "ent_law_ru": ("law", "ru", "base"), "ubt_law_kz": ("law", "kk", "tr"),
    "ent_russian_ru": ("russian_ru", "ru", "own"), "ubt_russian_kz": ("russian_kk", "kk", "own"),
    "ent_literature_ru": ("literature", "ru", "own"), "ubt_kazlit_kz": ("kazlit", "kk", "own"),
    "ent_kazakh_ru": ("kazakh_ru", "ru", "own"), "ubt_kazakh_kz": ("kazakh_kk", "kk", "own"),
}
PAIR = {}  # русский базовый канал -> казахский канал-перевод
for _c, (_s, _l, _m) in CHANNELS.items():
    if _m == "tr":
        PAIR[[k for k, v in CHANNELS.items() if v[0] == _s and v[2] == "base"][0]] = _c
BASE_OF = {v: k for k, v in PAIR.items()}

# ---------- база ----------
DB = sqlite3.connect(os.environ.get("QUIZ_DB") or (ROOT / "quiz.sqlite3"))
DB.executescript("""
CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY, channel TEXT, h TEXT UNIQUE, data TEXT,
                              created TEXT, used TEXT);
CREATE TABLE IF NOT EXISTS plan (day TEXT, channel TEXT, ts TEXT, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS plan2 (day TEXT, channel TEXT, ts TEXT, done INTEGER DEFAULT 0);
""")


def pool_size(ch: str) -> int:
    return DB.execute("SELECT count(*) FROM q WHERE channel=? AND used IS NULL", (ch,)).fetchone()[0]


def recent_qs(ch: str, n: int = 80) -> list:
    rows = DB.execute("SELECT data FROM q WHERE channel=? ORDER BY id DESC LIMIT ?", (ch, n)).fetchall()
    return [json.loads(r[0])["q"] for r in rows]


def save(ch: str, items: list) -> int:
    added = 0
    now = dt.datetime.now(KZ).isoformat()
    for it in items:
        h = hashlib.sha1((ch + it["q"]).encode()).hexdigest()
        try:
            DB.execute("INSERT INTO q(channel,h,data,created) VALUES(?,?,?,?)",
                       (ch, h, json.dumps(it, ensure_ascii=False), now))
            added += 1
        except sqlite3.IntegrityError:
            pass
    DB.commit()
    return added


# ---------- Telegram ----------
http = httpx.AsyncClient(timeout=60)


async def tg(method: str, files=None, **params):
    for attempt in range(4):
        if files:
            r = await http.post(API + method, data={k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                                                    for k, v in params.items()}, files=files)
        else:
            r = await http.post(API + method, json=params)
        d = r.json()
        if d.get("ok"):
            return d["result"]
        if d.get("error_code") == 429:
            await asyncio.sleep(d.get("parameters", {}).get("retry_after", 5) + 1)
            continue
        raise RuntimeError(f"{method}: {d.get('description')}")
    raise RuntimeError(f"{method}: too many retries")


# ---------- Claude ----------
ai = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


def _txt(resp) -> str:
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()


def _json(s: str):
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip(), flags=re.M)
    a, b = s.find("["), s.rfind("]")
    a2 = s.find("{")
    if a == -1 or (a2 != -1 and a2 < a and s.rfind("}") > b):
        a, b = a2, s.rfind("}")
    return json.loads(s[a:b + 1])


async def ask(user: str, system: str = "", max_tokens: int = 6000, content=None):
    msgs = [{"role": "user", "content": content if content is not None else user}]
    kw = dict(model=MODEL, max_tokens=max_tokens, messages=msgs)
    if system:
        kw["system"] = system
    return _txt(await ai.messages.create(**kw))


# ---------- база реальных заданий ЕНТ (пробники НЦТ, официальные образцы, ЕНТ-каналы) ----------
# refs.bin зашифрован ключом, производным от ANTHROPIC_API_KEY (в открытом репозитории материалы не читаются)
REFS_FILE_ID = "BQACAgIAAxkBAAMDawU_wFT3diAmjnqJuLU7dBHkpJEAAjqsAAI3OzBIMRDqk0bmkJs9BA"
REF_ALIAS = {"russian_kk": "russian_ru", "kazakh_kk": "kazakh_ru"}
_REFS = None


def refs() -> dict:
    global _REFS
    if _REFS is not None:
        return _REFS
    _REFS = {}
    p = ROOT / "refs.bin"
    if not p.exists():
        try:  # файл хранится в Telegram (отправлен боту), в репозиторий не кладётся
            f = httpx.get(API + "getFile", params={"file_id": REFS_FILE_ID}, timeout=30).json()["result"]
            p.write_bytes(httpx.get(f"https://api.telegram.org/file/bot{TOKEN}/{f['file_path']}", timeout=60).content)
        except Exception as e:
            log.warning("refs.bin download failed: %s", type(e).__name__)
            return _REFS
    try:
        k = hashlib.sha256(("entref-v1:" + os.environ.get("ANTHROPIC_API_KEY", "")).encode()).digest()
        enc = p.read_bytes()
        ks = bytearray()
        i = 0
        while len(ks) < len(enc):
            ks += hashlib.sha256(k + i.to_bytes(8, "big")).digest()
            i += 1
        raw = bytes(a ^ b for a, b in zip(enc, ks))
        if not raw.startswith(b"ENTREF1\n"):
            log.warning("refs.bin: key mismatch")
            return _REFS
        _REFS = json.loads(zlib.decompress(raw[8:]))
        log.info("refs loaded: %s", {s: len(v["examples"]) for s, v in _REFS.items()})
    except Exception as e:
        log.warning("refs.bin: %s", e)
    return _REFS


def ref_block(subj: str) -> str:
    R = refs().get(REF_ALIAS.get(subj, subj)) or {}
    ex = R.get("examples", [])
    lim = 2200 if subj == "reading" else 900
    groups = [("[ОФИЦ", 3), ("[ПРОБН", 7), ("[ВИКТ", 4)]
    if subj == "reading":
        groups = [("[ОФИЦ", 1), ("[ПРОБН", 2), ("[ВИКТ", 0)]
    pick = []
    for tag, n in groups:
        g = [e for e in ex if e.startswith(tag)]
        pick += random.sample(g, min(n, len(g)))
    out = ""
    if R.get("spec"):
        out += "Официальная спецификация НЦТ по предмету (разделы, темы, уровни трудности):\n" + R["spec"][:3500] + "\n\n"
    if pick:
        out += ("Примеры реальных заданий ЕНТ (пробное тестирование НЦТ, официальные образцы, ЕНТ-каналы; текст распознан "
                "с экрана — возможны опечатки распознавания, кружки «О» перед вариантами — это интерфейс):\n---\n"
                + "\n---\n".join(e[:lim] for e in pick) + "\n---\n")
    if out:
        out += ("Составь НОВЫЕ задания строго в таком же стиле, как примеры: такие же короткие сухие формулировки, как в тесте "
                "НЦТ (часто незаконченное предложение с двоеточием), такой же уровень и типы заданий, темы — из спецификации "
                "(каждое задание — другая тема); сложность примерно 50% базовый (A), 30% средний (B), 20% высокий (C). "
                "Не копируй примеры дословно — другие факты, числа, формулировки.\n")
    return out


SYSTEM = """Ты — опытный методист ЕНТ/ҰБТ (Казахстан, экзамен 2027). Составляешь тестовые задания для Telegram-викторин.
Правила:
- Формат и сложность как в реальном ЕНТ по этому предмету: один правильный ответ из 4 вариантов.
- Только достоверные факты и однозначно верный ответ. Если сомневаешься — бери другое задание.
- Ограничения: вопрос до 250 символов, каждый вариант до 80 символов, пояснение до 170 символов.
- Варианты без букв «A)» и без нумерации. Неверные варианты правдоподобные.
- Пояснение — коротко, почему верен ответ (формула, дата, правило).
- Без эмодзи и ссылок. Не называть задания «сливом» или «реальными заданиями экзамена».
- Темы внутри предмета разнообразь, не повторяй уже бывшие задания.
- Задание «по картинке»: вопрос опирается на фото (например «Кто изображён на фото?», «Какой памятник на фото?»,
  «Какое растение изображено?»). Для него поле image — точное название объекта на английском так, как он
  назван в Wikimedia Commons / Википедии (например "Kenesary Kasymov", "Khoja Ahmed Yasawi Mausoleum",
  "Charyn Canyon", "Abai Kunanbaiuly", "Mitochondrion"). Выбирай только известные объекты, у которых точно есть фото.
  Для обычных заданий image = "".
Верни СТРОГО JSON-массив:
[{"passage": "", "q": "...", "options": ["...","...","...","..."], "correct": 0, "explanation": "...", "image": ""}]"""


def lang_rule(subj: str, lang: str) -> str:
    if subj == "english":
        return "Вопрос и варианты — на английском. Пояснение — на русском."
    if subj == "russian_kk":
        return "Задания и варианты — на русском (это русский язык). Пояснение — на казахском языке."
    if subj == "kazakh_ru":
        return ("Примеры, слова и варианты — на казахском, формулировка вопроса и пояснение — на русском.")
    if lang == "kk":
        return "Всё (вопрос, варианты, пояснение, текст) — на грамотном казахском литературном языке, без русизмов."
    return "Всё — на русском языке."


async def compose(subj: str, lang: str, seen: list, n: int = 8) -> list:
    S = SUBJECTS[subj]
    if subj == "reading":
        extra = ("Для каждого задания дай свой короткий текст в поле passage (400–700 символов, научно-популярный или "
                 "публицистический, про Казахстан/мир/науку), вопрос — по этому тексту.")
    elif subj == "mathliteracy":
        extra = ("Примерно у половины заданий дай условие-ситуацию в passage (до 500 символов: таблица строками, тариф, "
                 "расписание, данные графика словами), вопрос — короткий по условию. У остальных passage=\"\".")
    else:
        extra = "Поле passage оставь пустым \"\"."
    pics = round(n * S["picture"])
    pic_rule = (f"Ровно {pics} заданий из {n} сделай «по картинке» (с полем image)." if pics
                else "Заданий по картинке не нужно: image = \"\" у всех.")
    name = S["ru"] + (f" ({S['kk']})" if S["kk"] else "")
    user = (ref_block(subj) + f"Предмет: {name}. Содержание: {S['scope']}.\n{lang_rule(subj, lang)}\n{extra}\n{pic_rule}\n"
            f"Составь {n} заданий, разные темы и уровни сложности.\nУже были (не повторять):\n"
            + ("\n".join("- " + q[:90] for q in seen) or "(нет)"))
    items = None
    for _ in range(3):
        raw = await ask(user, SYSTEM)
        try:
            items = _json(raw)
            break
        except Exception:
            try:
                items = _json(await ask("Ниже JSON с синтаксической ошибкой. Верни ТОЛЬКО исправленный валидный JSON "
                                        "(кавычки внутри строк экранируй как \\\", переносы как \\n):\n\n" + raw))
                break
            except Exception:
                continue
    if items is None:
        return []
    if isinstance(items, dict):
        items = items.get("questions", [])
    return [x for x in (_clean(it) for it in items) if x]


def _clean(it: dict):
    try:
        opts = [str(o).strip() for o in it["options"]][:4]
        c = int(it["correct"])
        q = str(it["q"]).strip()
        if len(opts) != 4 or not 0 <= c < 4 or not q or len(q) > 300 or any(not o or len(o) > 100 for o in opts):
            return None
        if len(set(opts)) < 4:
            return None
        return {"passage": str(it.get("passage") or "").strip()[:1500], "q": q, "options": opts, "correct": c,
                "explanation": str(it.get("explanation") or "").strip()[:195],
                "image": str(it.get("image") or "").strip(), "img": it.get("img", "")}
    except Exception:
        return None


async def verify(items: list) -> list:
    """Второй проход: модель решает задания заново, без подсказки. Оставляем только совпавшие ответы."""
    if not items:
        return []
    block = []
    for i, it in enumerate(items):
        p = f"Текст: {it['passage']}\n" if it["passage"] else ""
        im = f"(К заданию приложено фото: {it['image']})\n" if it.get("image") else ""
        opts = "\n".join(f"  {j}) {o}" for j, o in enumerate(it["options"]))
        block.append(f"#{i}\n{p}{im}Вопрос: {it['q']}\n{opts}")
    user = ("Реши каждое тестовое задание самостоятельно и внимательно. Если нет однозначно верного ответа, "
            "верных несколько или есть фактическая ошибка — ответ -1.\n"
            "Верни СТРОГО JSON-массив чисел: номер верного варианта для каждого задания по порядку.\n\n"
            + "\n\n".join(block))
    try:
        ans = _json(await ask(user, max_tokens=1500))
    except Exception:
        log.warning("verify failed")
        return []
    return [it for it, a in zip(items, ans) if isinstance(a, int) and a == it["correct"]]


TR_SYSTEM = ("Ты — профессиональный переводчик учебных материалов ЕНТ/ҰБТ на казахский язык. Переводи точно, "
             "грамотным литературным казахским языком, с правильной терминологией казахстанских учебников. "
             "Числа, формулы, даты и порядок вариантов не меняй. Английские фразы в заданиях по английскому не переводи.")


async def translate(items: list, subj: str) -> list:
    src = [{"passage": it["passage"], "q": it["q"], "options": it["options"], "explanation": it["explanation"]}
           for it in items]
    note = ("Это английский язык: переводи только пояснение (explanation), вопрос и варианты оставь на английском."
            if subj == "english" else "Переведи все поля.")
    user = (f"{note} Ограничения: вопрос до 280 символов, вариант до 95, пояснение до 190.\n"
            "Верни СТРОГО JSON-массив той же длины и структуры.\n\n" + json.dumps(src, ensure_ascii=False))
    try:
        out = _json(await ask(user, TR_SYSTEM, max_tokens=8000))
    except Exception:
        log.warning("translate failed")
        return []
    res = []
    for it, t in zip(items, out):
        t = dict(t)
        t.update(correct=it["correct"], image=it.get("image", ""), img=it.get("img", ""))
        c = _clean(t)
        if c:
            res.append(c)
    return res


# ---------- картинки (Wikimedia Commons, свободные лицензии) ----------
async def find_image(name: str):
    params = {"action": "query", "format": "json", "generator": "search",
              "gsrsearch": f"filetype:bitmap {name}", "gsrnamespace": 6, "gsrlimit": 8,
              "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": 1280}
    try:
        async with httpx.AsyncClient(timeout=30, headers=UA, follow_redirects=True) as c:
            pages = (await c.get("https://commons.wikimedia.org/w/api.php", params=params)).json() \
                .get("query", {}).get("pages", {})
            cands = sorted(pages.values(), key=lambda p: p.get("index", 99))
            for p in cands[:5]:
                ii = (p.get("imageinfo") or [{}])[0]
                if ii.get("mime") not in ("image/jpeg", "image/png") or ii.get("width", 0) < 400:
                    continue
                lic = ii.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "")
                if not lic or "non-free" in lic.lower():
                    continue
                r = await c.get(ii.get("thumburl") or ii["url"])
                if r.status_code != 200 or len(r.content) < 15000:
                    continue
                if await image_ok(r.content, ii["mime"], name):
                    fn = IMG_DIR / (hashlib.sha1(r.content).hexdigest()[:16] + (".png" if "png" in ii["mime"] else ".jpg"))
                    fn.write_bytes(r.content)
                    return {"path": fn.name, "license": lic}
    except Exception as e:
        log.warning("image search %s: %s", name, e)
    return None


async def image_ok(data: bytes, mime: str, name: str) -> bool:
    """Claude смотрит на фото: это действительно «name», без лишних подписей-подсказок?"""
    if len(data) > 4_500_000:
        return False
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": f"На этом фото изображён «{name}»? Фото подходит для тестового вопроса "
                                 f"(объект хорошо виден, на фото нет надписи с ответом)? Ответь одним словом: YES или NO."},
    ]
    try:
        ans = await ask("", content=content, max_tokens=10)
        return ans.strip().upper().startswith("Y")
    except Exception:
        return False


async def attach_images(items: list) -> list:
    out = []
    for it in items:
        if it.get("image"):
            img = await find_image(it["image"])
            if not img:
                continue  # без подходящего фото задание по картинке не публикуем
            it["img"] = img["path"]
            it["license"] = img["license"]
        out.append(it)
    return out


# ---------- пополнение запаса ----------
async def generate(ch: str):
    subj, lang, mode = CHANNELS[ch]
    if mode == "tr":
        return await generate(BASE_OF[ch])
    items = await compose(subj, lang, recent_qs(ch))
    items = await verify(items)
    items = await attach_images(items)
    n = save(ch, items)
    log.info("generated %s: %d", ch, n)
    if mode == "base" and items:
        kz = PAIR[ch]
        tr = await translate(items, subj)
        for t, orig in zip(tr, items):
            t["license"] = orig.get("license", "")
        log.info("translated %s: %d", kz, save(kz, tr))


async def ensure_pool(ch: str, minimum: int):
    for _ in range(3):
        if pool_size(ch) >= minimum:
            return
        try:
            await generate(ch)
        except Exception as e:
            log.exception("generate %s failed: %s", ch, e)
            await asyncio.sleep(20)


def migrate_pool():
    """Один раз после появления базы реальных заданий: старый неиспользованный пул уходит в архив."""
    DB.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    if DB.execute("SELECT 1 FROM meta WHERE k='refs_v1'").fetchone() or not refs():
        return
    n = DB.execute("UPDATE q SET used='old' WHERE used IS NULL").rowcount
    DB.execute("INSERT INTO meta VALUES('refs_v1', ?)", (dt.datetime.now(KZ).isoformat(),))
    DB.commit()
    log.info("pool migrated: %d old questions archived", n)


async def refill_all():
    migrate_pool()
    deadline = dt.datetime.now(KZ) + dt.timedelta(minutes=int(os.environ.get("FILL_MINUTES", "0") or 10**6))
    for ch in CHANNELS:
        if dt.datetime.now(KZ) > deadline:
            log.info("fill: time limit reached")
            break
        if CHANNELS[ch][2] == "tr":
            continue
        targets = [ch] + ([PAIR[ch]] if ch in PAIR else [])
        while any(pool_size(c) < PER_DAY * 2 + 2 for c in targets):
            before = [pool_size(c) for c in targets]
            try:
                await generate(ch)
            except Exception as e:
                log.exception("generate %s failed: %s", ch, e)
                break
            if [pool_size(c) for c in targets] == before:
                break
            await asyncio.sleep(3)


# ---------- публикация ----------
async def post_one(ch: str) -> bool:
    """Публикует одно следующее задание канала. Если Telegram отклонил задание — помечаем его и берём следующее."""
    for _attempt in range(4):
        if pool_size(ch) < 1:
            try:
                await generate(ch)
            except Exception as e:
                log.exception("generate %s: %s", ch, e)
        row = DB.execute("SELECT id, data FROM q WHERE channel=? AND used IS NULL ORDER BY id LIMIT 1", (ch,)).fetchone()
        if not row:
            log.error("no questions for %s", ch)
            return False
        qid, data = row
        it = json.loads(data)
        chat = "@" + ch
        try:
            if it.get("img") and (IMG_DIR / it["img"]).exists():
                with open(IMG_DIR / it["img"], "rb") as f:
                    await tg("sendPhoto", files={"photo": (it["img"], f)}, chat_id=chat)
                await asyncio.sleep(2)
            if it.get("passage"):
                await tg("sendMessage", chat_id=chat, text=it["passage"][:4000], link_preview_options={"is_disabled": True})
                await asyncio.sleep(2)
            opts = list(enumerate(it["options"]))
            random.shuffle(opts)
            correct = [i for i, (orig, _) in enumerate(opts) if orig == it["correct"]][0]
            params = dict(chat_id=chat, question=it["q"][:300], options=[{"text": o[:100]} for _, o in opts],
                          type="quiz", correct_option_id=correct, is_anonymous=True)
            if it.get("explanation"):
                params["explanation"] = it["explanation"][:200]
            await tg("sendPoll", **params)
        except RuntimeError as e:
            log.error("post %s q#%d rejected: %s", ch, qid, e)
            DB.execute("UPDATE q SET used=? WHERE id=?", ("bad:" + str(e)[:80], qid))
            DB.commit()
            continue
        DB.execute("UPDATE q SET used=? WHERE id=?", (dt.datetime.now(KZ).isoformat(), qid))
        DB.commit()
        log.info("posted to %s: %s", ch, it["q"][:70])
        return True
    return False


def posted_today(ch: str) -> int:
    day = dt.datetime.now(KZ).date().isoformat()
    return DB.execute("SELECT count(*) FROM q WHERE channel=? AND used LIKE ?", (ch, day + "%")).fetchone()[0]


async def post_block(ch: str) -> bool:
    """Серия из PER_DAY заданий подряд в один канал, с паузой ~30 секунд."""
    n = max(0, PER_DAY - posted_today(ch))
    ok_any = False
    for i in range(n):
        if await post_one(ch):
            ok_any = True
        if i < n - 1:
            await asyncio.sleep(BLOCK_GAP_SEC)
    return ok_any or n == 0


def channels_for(day: dt.date) -> list:
    """Через день: один день — все русские каналы, следующий — все казахские."""
    lang = "ru" if day.toordinal() % 2 == 0 else "kk"
    return [c for c, v in CHANNELS.items() if v[1] == lang]


def make_plan(day: dt.date):
    """Каждому каналу — одно своё время в день; каналы идут по очереди через ~20–25 минут, порядок каждый день новый."""
    if DB.execute("SELECT 1 FROM plan2 WHERE day=? LIMIT 1", (day.isoformat(),)).fetchone():
        return
    start = dt.datetime(day.year, day.month, day.day, *DAY_START, tzinfo=KZ)
    end = dt.datetime(day.year, day.month, day.day, *DAY_END, tzinfo=KZ)
    now = dt.datetime.now(KZ)
    if now > start:
        start = now + dt.timedelta(minutes=3)
    chans = channels_for(day)
    random.shuffle(chans)
    if start >= end:
        return
    step = (end - start) / len(chans)
    for i, ch in enumerate(chans):
        jitter = dt.timedelta(seconds=random.randint(0, max(0, int(step.total_seconds() * 0.4))))
        ts = start + step * i + jitter
        DB.execute("INSERT INTO plan2(day,channel,ts) VALUES(?,?,?)", (day.isoformat(), ch, ts.isoformat()))
    DB.commit()
    log.info("plan for %s created", day)


async def loop():
    me = await tg("getMe")
    log.info("started as @%s, %d quizzes/day per channel", me["username"], PER_DAY)
    last_refill = None
    refilling = None
    while True:
        now = dt.datetime.now(KZ)
        make_plan(now.date())
        due = DB.execute("SELECT rowid, channel, ts FROM plan2 WHERE done=0 AND day=? ORDER BY ts",
                         (now.date().isoformat(),)).fetchall()
        for rowid, ch, ts in due:
            t = dt.datetime.fromisoformat(ts)
            if t > now:
                continue
            ok = False
            if now - t < dt.timedelta(hours=3):   # если Mac спал дольше — слот пропускаем
                try:
                    ok = await post_block(ch)
                except Exception as e:
                    log.exception("post %s failed: %s", ch, e)
            DB.execute("UPDATE plan2 SET done=? WHERE rowid=?", (1 if ok else 2, rowid))
            DB.commit()
            await asyncio.sleep(random.randint(3, 9))
        if (refilling is None or refilling.done()) and (last_refill is None or now - last_refill > dt.timedelta(hours=1)):
            last_refill = now
            refilling = asyncio.create_task(refill_all())
        await asyncio.sleep(30)


async def tick():
    """Запуск для GitHub Actions: публикует серии, время которых подошло, и ждёт серии ближайшего часа,
    чтобы каждая вышла точно в своё время (GitHub запускает расписание примерно раз в час)."""
    lookahead = dt.timedelta(minutes=int(os.environ.get("LOOKAHEAD_MIN", "62")))
    started = dt.datetime.now(KZ)
    make_plan(started.date())
    rows = DB.execute("SELECT rowid, channel, ts FROM plan2 WHERE done=0 AND day=? ORDER BY ts",
                      (started.date().isoformat(),)).fetchall()
    for rowid, ch, ts in rows:
        if ch not in channels_for(started.date()):
            DB.execute("UPDATE plan2 SET done=3 WHERE rowid=?", (rowid,))
            DB.commit()
            continue
        t = dt.datetime.fromisoformat(ts)
        if t > started + lookahead:
            break
        now = dt.datetime.now(KZ)
        if t > now:
            await asyncio.sleep((t - now).total_seconds())
        now = dt.datetime.now(KZ)
        ok = False
        if now - t < dt.timedelta(hours=3):
            try:
                ok = await post_block(ch)
            except Exception as e:
                log.exception("post %s failed: %s", ch, e)
        DB.execute("UPDATE plan2 SET done=? WHERE rowid=?", (1 if ok else 2, rowid))
        DB.commit()


async def check():
    print("token looks ok:", bool(re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", TOKEN)), "| claude key looks ok:",
          os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"))
    print("refs:", {k: len(v["examples"]) for k, v in refs().items()} or "НЕ ЗАГРУЖЕНЫ")
    me = await tg("getMe")
    print("bot:", me["username"])
    for ch in CHANNELS:
        try:
            m = await tg("getChatMember", chat_id="@" + ch, user_id=me["id"])
            print(f"{ch:24} {m['status']:14} post={m.get('can_post_messages')}  запас={pool_size(ch)}")
        except Exception as e:
            print(f"{ch:24} ERROR {e}")


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    try:
        if cmd == "check":
            await check()
        elif cmd == "now":
            await post_one(sys.argv[2])
        elif cmd == "gen":
            await generate(sys.argv[2])
        elif cmd == "fill":
            await refill_all()
        elif cmd == "tick":
            await tick()
        else:
            await loop()
    finally:
        await http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
