import argparse
import os
import json
import sys
import time
import requests
from zoneinfo import ZoneInfo
from datetime import timedelta, datetime

def require_env(name):
    """Читает обязательную переменную окружения с внятной ошибкой вместо KeyError.

    Намеренно ленивая (не на уровне модуля): импорт этого файла не должен
    требовать телеграм-токенов, которые нужны только самой отправке.
    """
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Не задана переменная окружения {name}.\n"
            f"  Windows:  set {name}=<значение>\n"
            f"  bash:     export {name}=<значение>\n"
            f"(в GitHub Actions она приходит из secrets.{name})"
        )
    return value


# Highlightly: ключ передаётся в заголовке x-rapidapi-key даже при прямом
# использовании (не через RapidAPI). Заголовок x-rapidapi-host нужен только
# если вы реально ходите через хост RapidAPI, а не через soccer.highlightly.net.
def api_headers():
    return {"x-rapidapi-key": require_env("HIGHLIGHTLY_API_KEY")}


BASE_URL = "https://soccer.highlightly.net"

LOCAL_TZ = ZoneInfo("Europe/Minsk")
LOCAL_TZ_NAME = "Europe/Minsk"

# Не показываем матчи, которые стартуют раньше этого часа по Минску —
# слишком ранние матчи (ночь/раннее утро) неактуальны для ставок: мало кто
# заходит в это время. Порог можно менять одной строкой.
MIN_START_HOUR = 7

# --- Приоритеты турниров ---
# Одна таблица в config/league_weights.json: общий приоритет турнира для всех
# локалей плюс переопределения отдельной локали. Правится вкладкой
# «Приоритеты» заполнялки (coupon-filler), которая сама коммитит и пушит, —
# поэтому здесь нет разделов «сборные / еврокубки / топ-дивизионы»: редактору
# пришлось бы угадывать, в какой из них класть новый турнир. Разделы ни разу
# не пересекались, так что свёртка в одну таблицу отбор не изменила.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LEAGUE_WEIGHTS_PATH = os.path.join(REPO_ROOT, "config", "league_weights.json")
DATA_DIR = os.path.join(REPO_ROOT, "data")

# Сколько дней держим data/*.json, прежде чем удалить. Файлы коммитятся в
# репозиторий (см. workflow), а нужны только coupon-filler'у на день-два
# вперёд — без чистки папка растёт без предела на каждый прогон.
DATA_RETENTION_DAYS = 2

with open(LEAGUE_WEIGHTS_PATH, encoding="utf-8") as _f:
    _CFG = json.load(_f)

def priority_tables(entries):
    """(приоритеты, группы) из таблицы priorities конфига.

    Запись без числа — турнир, сброшенный во вкладке к умолчанию: в
    приоритеты он не идёт, а group у него остаётся. group — прежний раздел
    турнира; на отбор не влияет, только на подпись категории в телеграме
    (get_category): сборные и еврокубки подписываются International, а не
    страной из ответа Highlightly.
    """
    priorities = {int(lid): e["priority"] for lid, e in entries.items() if "priority" in e}
    groups = {int(lid): e.get("group") for lid, e in entries.items()}
    return priorities, groups


def excluded_leagues(entries):
    """Исключённые турниры: {id: причина}. Их матчи не попадают в список ни
    одной локали — формат не линкуется с админкой (не 11 на 11, изменённые
    правила вроде Kings League). Не приоритет 0: тот в бедный день всё равно
    добрал бы такие матчи в хвост списка."""
    return {int(lid): e["excluded"] for lid, e in entries.items() if e.get("excluded")}


PRIORITIES, GROUPS = priority_tables(_CFG["priorities"])
EXCLUDED = excluded_leagues(_CFG["priorities"])

# Всё, чего нет в таблице, — Oberliga, Regionalliga, резервы и т.д.
DEFAULT_PRIORITY = _CFG["default_priority"]

LOCALE_CONFIG = {
    key: {
        "label": conf["label"],
        "overrides": {int(lid): priority for lid, priority in conf["overrides"].items()},
    }
    for key, conf in _CFG["locales"].items()
}


# ---------------------------------------------------------------------------
# ДОНАБОР "ПЕРЕТЁКШИХ" МАТЧЕЙ
#
# У некоторых стран разница во времени с Минском настолько велика, что
# вечерний матч по их локальному времени попадает по Минску уже на
# ПОСЛЕЗАВТРА, а не на завтра. Бразилия отстаёт от Минска на 6 часов:
# матч в 22:00 по Бразилии — это 04:00 следующих суток по Минску. Такие
# матчи не помещаются в обычную выборку "завтра" (они физически лежат под
# датой "послезавтра" в API) и заодно попадали бы под общий фильтр
# MIN_START_HOUR, если бы не эта отдельная логика.
#
# Поэтому для перечисленных здесь локалей дополнительно запрашиваем начало
# суток "послезавтра" (все матчи до max_hour по Минску) и добавляем их в пул
# ИМЕННО этой локали — на другие локали и на Global это не влияет.
#
# Окно берётся целиком, без отбора по лигам: в него попадает не только
# домашний чемпионат, но и Либертадорес, Судамерикана, кубки КОНКАКАФ и
# соседние южноамериканские лиги, которые играются ровно в эти часы и по
# весу стоят выше местного чемпионата. Что из этого реально дойдёт до
# топ-30, решает скоринг, а не список лиг.
# ---------------------------------------------------------------------------
CARRY_OVER_CONFIG = {
    "brazil": {"max_hour": 6},  # матчи бразильских лиг с "послезавтра" до 06:00 по Минску
    "mexico": {"max_hour": 8},  # Мексика (UTC-6) отстаёт от Минска на 9 часов —
                                 # матч в 20:00-21:00 по Мексике = 05:00-06:00 по Минску
                                 # на следующие сутки; берём порог с запасом до 08:00.
}


_DAY_CACHE = {}


def fetch_matches(day):
    """Тянет все матчи на дату day (YYYY-MM-DD) с пагинацией.

    В Highlightly ответ /matches всегда обёрнут в {"data": [...], "pagination": {...}}
    и максимальный limit на страницу — 100, поэтому при большом количестве
    матчей за день нужно проходить через offset.

    Результат запоминается на время прогона: донабор "перетёкших" матчей
    (fetch_carry_over_matches) запрашивает одну и ту же дату отдельно для
    каждой локали из CARRY_OVER_CONFIG, и без кеша послезавтрашний день
    выкачивался целиком дважды — треть всех запросов уходила впустую.
    """
    if day in _DAY_CACHE:
        print(f"DEBUG: {day} — беру из кеша ({len(_DAY_CACHE[day])} матчей)", flush=True)
        return _DAY_CACHE[day]

    all_matches = []
    limit = 100
    offset = 0
    started = time.monotonic()

    while True:
        resp = requests.get(
            f"{BASE_URL}/matches",
            headers=api_headers(),
            params={
                "date": day,
                # timezone фильтрует и, судя по докам, локализует дату матчей —
                # аналог параметра timezone в API-Football, чтобы матчи 00:00-02:59
                # по Минску не терялись/не задваивались между сутками по UTC.
                "timezone": LOCAL_TZ_NAME,
                "limit": limit,
                "offset": offset,
            },
        )
        page_started = time.monotonic()
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("data", [])
        all_matches.extend(batch)
        # Постранично, чтобы в логах Actions было видно, какой именно запрос
        # тормозит. Раньше весь вывод буферизовался и печатался одним куском
        # в самом конце, поэтому 9-минутный прогон выглядел мгновенным.
        print(f"DEBUG: {day} offset={offset}: {len(batch)} матчей "
              f"за {time.monotonic() - page_started:.1f}s", flush=True)

        total = payload.get("pagination", {}).get("totalCount", len(all_matches))
        offset += limit
        if len(batch) < limit or offset >= total:
            break

    print(f"DEBUG: {day} — всего {len(all_matches)} матчей "
          f"за {time.monotonic() - started:.1f}s", flush=True)
    _DAY_CACHE[day] = all_matches
    return all_matches


def parse_local_dt(iso_date_str):
    """Highlightly отдаёт дату матча в ISO-формате (обычно с суффиксом Z / UTC).
    Конвертируем явно в Europe/Minsk, а не полагаемся на то, что API уже
    локализовал строку — так надёжнее вне зависимости от их поведения."""
    dt = datetime.fromisoformat(iso_date_str.replace("Z", "+00:00"))
    return dt.astimezone(LOCAL_TZ)


def is_after_cutoff(m, min_hour=MIN_START_HOUR):
    """True, если матч стартует в min_hour:00 по Минску или позже."""
    return parse_local_dt(m["date"]).hour >= min_hour


def fetch_carry_over_matches(day_str, max_hour):
    """Все матчи на дату day_str, стартующие по Минску РАНЬШЕ max_hour.
    Используется для "перетёкших" вечерних матчей (см. CARRY_OVER_CONFIG).

    Раньше здесь стоял дополнительный фильтр по лигам самой локали, и в пул
    Бразилии попадали только четыре её турнира. Из-за этого перетекали
    исключительно домашние матчи, а Либертадорес, Судамерикана, кубки
    КОНКАКАФ и соседние южноамериканские чемпионаты — то есть ровно то, что
    в это окно и играется, — не попадали никуда, хотя по весу они выше
    любого местного чемпионата.

    Фильтра по лигам тут больше нет намеренно: окно 00:00-08:00 по Минску —
    это 21:00-05:00 UTC, то есть вечер в обеих Америках и глухая ночь в
    Азии с Австралией, так что посторонних турниров в нём почти нет. А те,
    что есть, отсекает не список лиг, а скоринг: всё, чего нет в весах,
    получает DEFAULT_PRIORITY и до топ-30 не доходит.
    """
    try:
        day_matches = fetch_matches(day_str)
    except Exception as e:
        print(f"DEBUG: carry-over fetch failed for {day_str}: {e}")
        return []
    return [m for m in day_matches if parse_local_dt(m["date"]).hour < max_hour]


def score_match(m, overrides):
    """Приоритет матча для локали: её переопределение, иначе общий приоритет
    турнира, иначе DEFAULT_PRIORITY."""
    league_id = m["league"]["id"]
    if league_id in overrides:
        return overrides[league_id]
    return PRIORITIES.get(league_id, DEFAULT_PRIORITY)


def local_leagues(cfg):
    """Местные турниры локали — те, что она подняла выше общего приоритета.

    Раньше это был отдельный список местных лиг с ярусами; теперь он стал
    переопределениями. Но не всякое переопределение — местный турнир: опущенная
    для Ирана Лига чемпионов в «Местные матчи» телеграма попадать не должна.
    """
    return {league_id for league_id, priority in cfg["overrides"].items()
            if priority > PRIORITIES.get(league_id, DEFAULT_PRIORITY)}


def rank_matches(matches, overrides):
    """Топ-30 матчей в порядке убывания приоритета. Исключённые турниры
    отсекаются здесь — одно место на ежедневный отчёт, --lists-only и
    донабор «перетёкших», и переопределение локали их не возвращает."""
    allowed = [m for m in matches if m["league"]["id"] not in EXCLUDED]
    return sorted(allowed, key=lambda m: score_match(m, overrides), reverse=True)[:22]


def split_widgets(ranked):
    """Делит топ-30 на две пачки под виджеты админки: (top_matches, top_events).

    Раскладка намеренно неоднородная: места 1-10 и 21-30 по приоритету идут
    в top_events, а места 11-20 — в top_matches. То есть top_events получает
    и самые сильные матчи, и хвост, а top_matches — середину.

    Правило живёт только здесь: и data/*.json, и текст для телеграма берут
    раскладку из этой функции. Если развести их по разным местам, они
    разъедутся при следующей правке — так уже было, когда телеграм резал
    список своими срезами параллельно с build_lists.

    ranked короче 30 не ломает: срезы просто отдадут меньше, суммарно все
    матчи всё равно распределятся без потерь.
    """
    return ranked[12:22], ranked[:12] 


def get_category(m):
    league_id = m["league"]["id"]
    country = m.get("country", {}).get("name", "")

    if GROUPS.get(league_id) == "national_teams":
        return "International"
    if GROUPS.get(league_id) == "international_clubs":
        return "International Clubs"
    return country


def format_list(matches, report_date):
    """report_date — дата (date), на которую формируется отчёт ("завтра").
    Если матч фактически лежит на другой календарной дате (например,
    "перетёкший" бразильский матч с послезавтра) — показываем дату явно,
    чтобы это не выглядело как обычный ранний матч "завтра"."""
    if not matches:
        return "нет данных на эту дату"
    lines = []
    for i, m in enumerate(matches, 1):
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        league = m["league"]["name"]
        category = get_category(m)
        local_dt = parse_local_dt(m["date"])
        if local_dt.date() != report_date:
            time_str = local_dt.strftime("%d.%m %H:%M")
        else:
            time_str = local_dt.strftime("%H:%M")
        lines.append(f"{i}. {time_str} | {category} | {league} | {home} — {away}")
    return "\n".join(lines)


# Telegram режет сообщения длиннее 4096 символов. Берём с запасом на разметку.
TELEGRAM_MESSAGE_LIMIT = 4000


def pack_sections(sections, limit=TELEGRAM_MESSAGE_LIMIT):
    """Склеивает секции в как можно меньшее число сообщений, не разрывая
    секцию посередине.

    Обычно всё умещается в одно: global занимает ~2000 символов, а локали с
    парой местных матчей — по 60-250. Но в насыщенный день, когда своё есть
    у всех семи, сумма подходит к лимиту, поэтому разбиение нужно на всякий
    случай, а не как исключение.
    """
    chunk = []
    length = 0
    for section in sections:
        addition = len(section) + (2 if chunk else 0)
        if chunk and length + addition > limit:
            yield "\n\n".join(chunk)
            chunk, length = [], 0
            addition = len(section)
        chunk.append(section)
        length += addition
    if chunk:
        yield "\n\n".join(chunk)


def build_locale_message(locale_key, cfg, matches, report_date, display_date):
    """Текст для телеграма по одной локали.

    Локальные сообщения дублировали друг друга почти полностью: у Индии,
    Ирана и Нигерии 29 из 30 матчей совпадали с global, то есть в шести чатах
    лежала одна и та же простыня. Поэтому global по-прежнему отдаёт полный
    топ-30, а локали — матчи СВОИХ турниров (local_leagues)
    плюс "перетёкшие" из окна CARRY_OVER_CONFIG. Вторых в global нет по
    построению, так что на дублирование они не работают.

    На data/*.json это не влияет: там остаётся полный список, потому что
    виджеты в админке должны заполняться целиком для каждой локали.
    """
    header = f"*{cfg['label']} — {display_date}*"

    # Global — по ключу, а не по пустым переопределениям: их можно завести и
    # ему, и тогда общий топ превратился бы в «местные матчи».
    if locale_key == "global":
        # matches приходит в порядке убывания приоритета (см. main()), а на
        # блоки его режет та же split_widgets, что раскладывает data/*.json —
        # чтобы отчёт в телеграме показывал ровно то же, что уйдёт в виджеты.
        # Состав блоков неоднородный, поэтому места указаны прямо в заголовке:
        # иначе непонятно, почему в "ивентах" и лидеры, и хвост разом.
        block_matches, block_events = split_widgets(matches)
        return (
            f"{header}\n\n"
            f"*Топ ивенты :*\n"
            f"{format_list(block_events, report_date)}\n\n"
            f"*Топ матчи :*\n"
            f"{format_list(block_matches, report_date)}"
        )

    # Кроме своих турниров показываем ещё и "перетёкшие" матчи — те, что лежат
    # на следующей календарной дате (см. CARRY_OVER_CONFIG). Для Бразилии и
    # Мексики в это окно попадают Либертадорес, Судамерикана и кубки КОНКАКАФ:
    # местному читателю они интереснее собственного второго дивизиона, а
    # раньше отсекались, потому что турнир не входит в whitelist локали.
    # С global это не задваивается: carry-over добавляется только в пул этих
    # локалей, в общий топ-30 такие матчи не попадают вовсе.
    # Отличаем их по дате, а не по отдельному списку: перетёкший матч по
    # определению лежит не на report_date, и format_list ниже по той же
    # причине печатает у него дату явно.
    def is_carried(m):
        return parse_local_dt(m["date"]).date() != report_date

    local = local_leagues(cfg)
    selected = [m for m in matches
                if m["league"]["id"] in local or is_carried(m)]
    if not selected:
        return f"{header}\n\nМестных матчей на эту дату нет."

    carried_count = sum(1 for m in selected if is_carried(m))
    title = "Местные матчи" if not carried_count else "Местные и перетёкшие матчи"
    return f"{header}\n\n*{title} ({len(selected)}):*\n{format_list(selected, report_date)}"


def match_to_dict(m):
    """Компактное представление матча для data/{locale}_{date}.json —
    именно эти файлы читает вторая половина проекта — coupon-filler (через
    mapping_io), поэтому имена ключей здесь должны совпадать с тем, что
    ожидает её fill_widget: league_id, home_team_id, away_team_id
    (league_name, home_team_name, away_team_name).

    ВАЖНО: поле "id" у homeTeam/awayTeam здесь предполагается по аналогии
    с m["league"]["id"], но в текущем коде match_report.py оно нигде ещё
    не использовалось (только "name"). Стоит один раз распечатать сырой
    matches[0] через json.dumps и свериться со структурой реального ответа
    Highlightly, прежде чем полагаться на это в проде.
    """
    return {
        "league_id": m["league"]["id"],
        "league_name": m["league"]["name"],
        "home_team_id": m["homeTeam"]["id"],
        "home_team_name": m["homeTeam"]["name"],
        "away_team_id": m["awayTeam"]["id"],
        "away_team_name": m["awayTeam"]["name"],
        "date": m["date"],
    }


def cleanup_old_data(today):
    """Удаляет data/{locale}_{YYYY-MM-DD}.json, если с даты в имени файла
    прошло DATA_RETENTION_DAYS дней или больше (today — дата по Минску).

    Имя файла — это единственный источник даты: mtime тут не подходит,
    потому что после git clone/checkout время модификации — момент чекаута,
    а не момент, когда отчёт реально был сгенерирован.

    Удаление — это просто os.remove: пометить его как git-изменение должен
    следующий git add в workflow (git add <pathspec> подхватывает и удаления
    файлов внутри этого пути, отдельный git rm не нужен).
    """
    if not os.path.isdir(DATA_DIR):
        return

    removed = 0
    for name in os.listdir(DATA_DIR):
        if not name.endswith(".json"):
            continue
        date_part = name[:-len(".json")].rsplit("_", 1)[-1]
        try:
            file_date = datetime.fromisoformat(date_part).date()
        except ValueError:
            print(f"DEBUG: cleanup — не распознал дату в имени файла {name}, пропускаю")
            continue
        if (today - file_date).days >= DATA_RETENTION_DAYS:
            os.remove(os.path.join(DATA_DIR, name))
            removed += 1
            print(f"DEBUG: cleanup удалил {name} (дата {file_date}, "
                  f"{(today - file_date).days} дн. назад)")

    print(f"DEBUG: cleanup — удалено {removed} файлов старше "
          f"{DATA_RETENTION_DAYS} дн. (today={today})", flush=True)


def send_telegram(text):
    started = time.monotonic()
    resp = requests.post(
        f"https://api.telegram.org/bot{require_env('TELEGRAM_BOT_TOKEN')}/sendMessage",
        json={
            "chat_id": require_env("TELEGRAM_CHAT_ID"),
            "text": text,
            "parse_mode": "Markdown",
        },
    )
    # Без этой проверки упавшая отправка (протухший токен, слишком длинное
    # сообщение, битый Markdown) проходила молча: скрипт завершался успешно,
    # data/*.json коммитились, а в чат ничего не приходило.
    print(f"DEBUG: telegram {resp.status_code} за {time.monotonic() - started:.1f}s", flush=True)
    if not resp.ok:
        raise RuntimeError(f"Telegram вернул {resp.status_code}: {resp.text}")


def fetch_carry_over_pools(day_date):
    """Донабор "перетёкших" матчей (см. CARRY_OVER_CONFIG) для дня day_date:
    ранние по Минску матчи следующих суток, по локалям."""
    next_day_str = (day_date + timedelta(days=1)).isoformat()
    pools = {}
    for locale_key, carry_cfg in CARRY_OVER_CONFIG.items():
        carried = fetch_carry_over_matches(next_day_str, carry_cfg["max_hour"])
        print(f"DEBUG: carried over {len(carried)} matches for locale '{locale_key}' "
              f"from {next_day_str} (before {carry_cfg['max_hour']}:00 Minsk)")
        pools[locale_key] = carried
    return pools


def write_locale_lists(out_dir, locale_key, cfg, day_str, matches, carry_over_pools):
    """Ранжирует матчи локали и пишет {out_dir}/{локаль}_{дата}.json —
    вход для coupon-filler. Возвращает ранжированный список: он же нужен
    тексту для телеграма.

    Одна функция на ежедневный отчёт и на --lists-only: отбор, который
    coupon-filler добывает на произвольную дату, обязан совпадать с тем, что
    бот публикует на завтра, иначе купон разойдётся с отчётом в чате.
    """
    # Папку создаём заранее: в свежем чекауте data/ может не быть, и
    # open(..., "w") упал бы на первом запуске.
    os.makedirs(out_dir, exist_ok=True)

    pool = matches + carry_over_pools.get(locale_key, [])
    ranked = rank_matches(pool, cfg["overrides"])
    top_matches, top_events = split_widgets(ranked)

    # Здесь полный список: виджеты должны заполняться целиком для каждой
    # локали, урезается только то, что уходит в телеграм.
    data_path = os.path.join(out_dir, f"{locale_key}_{day_str}.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "top_events": [match_to_dict(m) for m in top_events],
                "top_matches": [match_to_dict(m) for m in top_matches],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"DEBUG: wrote {data_path} ({len(top_events)} top_events, {len(top_matches)} top_matches)",
          flush=True)
    return ranked


# Код выхода --lists-only, когда хотя бы на одну дату матчей нет: файлы на
# неё не пишутся. Отдельный от ошибки, чтобы вызывающий мог сказать «матчей
# нет», а не «добыча упала» — это разные вещи, и чинятся они по-разному.
EXIT_NO_MATCHES = 3


def write_lists(day_str, out_dir):
    """Отбор на одну дату без отчёта: только {локаль}_{дата}.json в out_dir.

    Ни телеграма, ни чистки data/: этот режим зовёт coupon-filler, чтобы
    добыть списки на дату, которую бот не публикует, и ежедневный отчёт тут
    ни при чём. Возвращает False, если на дату матчей нет — тогда файлов нет
    вовсе: пустой список заполнялка приняла бы за готовый.
    """
    day_date = datetime.fromisoformat(day_str).date()
    matches = [m for m in fetch_matches(day_str) if is_after_cutoff(m)]
    if not matches:
        print(f"=== на {day_str} матчей нет (или все раньше {MIN_START_HOUR}:00 "
              f"по Минску) — списки не записаны ===", flush=True)
        return False

    carry_over_pools = fetch_carry_over_pools(day_date)
    for locale_key, cfg in LOCALE_CONFIG.items():
        write_locale_lists(out_dir, locale_key, cfg, day_str, matches, carry_over_pools)
    return True


def lists_only(start, end, out_dir):
    """--lists-only на отрезок дат включительно. Возвращает код выхода:
    EXIT_NO_MATCHES, если хоть на одну дату матчей не нашлось — остальные
    даты при этом всё равно записаны."""
    first = datetime.fromisoformat(start).date()
    last = datetime.fromisoformat(end or start).date()
    if last < first:
        raise SystemExit(f"--end {last} раньше --start {first}")

    all_written = True
    day = first
    while day <= last:
        all_written = write_lists(day.isoformat(), out_dir) and all_written
        day += timedelta(days=1)
    return 0 if all_written else EXIT_NO_MATCHES


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Ежедневный отчёт на завтра; с --lists-only — только списки "
                    "для coupon-filler на выбранные даты.")
    parser.add_argument("--lists-only", action="store_true",
                        help="только {локаль}_{дата}.json, без телеграма и без чистки data/")
    # Абсолютные даты, а не смещения от сегодня: вызывающий посчитал дату у
    # себя, и если между этим и запуском прошла полночь, «+1» указал бы уже
    # на другой день.
    parser.add_argument("--start", metavar="ГГГГ-ММ-ДД", help="первая дата (с --lists-only)")
    parser.add_argument("--end", metavar="ГГГГ-ММ-ДД",
                        help="последняя дата включительно (по умолчанию = --start)")
    parser.add_argument("--out", default=DATA_DIR,
                        help="куда писать списки (по умолчанию data/ рядом со скриптом)")
    args = parser.parse_args(argv)
    if args.lists_only and not args.start:
        parser.error("--lists-only требует --start")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.lists_only:
        sys.exit(lists_only(args.start, args.end, args.out))
    daily_report()


def daily_report():
    minsk_now = datetime.now(LOCAL_TZ)
    tomorrow_date = (minsk_now + timedelta(days=1)).date()
    tomorrow_str = tomorrow_date.isoformat()
    display_date = tomorrow_date.strftime("%d.%m.%Y")

    print(f"DEBUG: minsk_now={minsk_now}, tomorrow_str={tomorrow_str}")

    # Чистим старые data/*.json в начале прогона, а не в конце: так чистка
    # отрабатывает даже если дальше API упадёт или матчей не найдётся и
    # функция выйдет раньше через return.
    cleanup_old_data(minsk_now.date())

    try:
        all_matches = fetch_matches(tomorrow_str)
        print(json.dumps(all_matches[0], ensure_ascii=False, indent=2))
        print(f"DEBUG: fetched {len(all_matches)} matches for {tomorrow_str}")
    except Exception as e:
        send_telegram(f"⚠️ Ошибка при получении данных: {e}")
        return

    if not all_matches:
        send_telegram(f"⚠️ Внимание: список матчей пуст на дату {tomorrow_str}. Проверь вручную.")
        return

    before_filter = len(all_matches)
    all_matches = [m for m in all_matches if is_after_cutoff(m)]
    print(f"DEBUG: {before_filter} matches before time filter, {len(all_matches)} after "
          f"(cutoff {MIN_START_HOUR}:00 Minsk)")

    if not all_matches:
        send_telegram(
            f"⚠️ На {tomorrow_str} матчи есть, но все стартуют раньше {MIN_START_HOUR}:00 по Минску. "
            f"Проверь вручную."
        )
        return

    carry_over_pools = fetch_carry_over_pools(tomorrow_date)

    sections = []
    for locale_key, cfg in LOCALE_CONFIG.items():
        ranked = write_locale_lists(DATA_DIR, locale_key, cfg, tomorrow_str,
                                    all_matches, carry_over_pools)
        sections.append(build_locale_message(locale_key, cfg, ranked,
                                              tomorrow_date, display_date))

    # Раньше уходило семь отдельных сообщений подряд. Теперь всё собирается в
    # одно — на практике это ~2500 символов, — а разбивается только если в
    # лимит не влезло.
    for message in pack_sections(sections):
        send_telegram(message)


if __name__ == "__main__":
    main()