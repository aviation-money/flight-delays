#!/usr/bin/env python3
"""Готовит сайт для поисковиков. Запускается после collect.py, перед публикацией.

* вписывает в docs/index.html готовую таблицу, короткую сводку, мета-теги и ссылки на аэропорты,
  чтобы поисковик видел цифры без выполнения JavaScript;
* создаёт отдельную страницу на каждый аэропорт: docs/airport/<iata>.html;
* создаёт docs/sitemap.xml.

Эти файлы не сохраняются в репозитории, они собираются заново при каждой публикации.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MSK = ZoneInfo("Europe/Moscow")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

e = lambda s: html.escape(str(s if s is not None else ""), quote=True)


def num(v) -> str:
    return f"{int(round(v)):,}".replace(",", " ")


def rub(v) -> str:
    if v >= 1e9:
        return f"{v / 1e9:.2f}".replace(".", ",") + " млрд ₽"
    if v >= 1e6:
        return f"{v / 1e6:.1f}".replace(".", ",") + " млн ₽"
    if v >= 1e3:
        return num(v / 1e3) + " тыс ₽"
    return num(v) + " ₽"


def plural(n, a, b, c):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return a
    if 2 <= n % 10 <= 4 and not 10 <= n % 100 < 20:
        return b
    return c


def human_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MSK)
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M} МСК"


def local_hm(iso, tz) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(tz)).strftime("%H:%M")


def put(s: str, name: str, content: str) -> str:
    pat = re.compile(rf"<!--SEO:{name}-->.*?<!--/SEO:{name}-->", re.S)
    if not pat.search(s):
        raise SystemExit(f"В index.html нет метки SEO:{name}")
    return pat.sub(lambda _: f"<!--SEO:{name}-->{content}<!--/SEO:{name}-->", s, count=1)


def head_tags(site, title, desc, url, extra_ld=None) -> str:
    img = site["url"] + "og.png"
    tags = [
        f'<link rel="canonical" href="{e(url)}">',
        f'<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{e(site["name"])}">',
        f'<meta property="og:locale" content="ru_RU">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(desc)}">',
        f'<meta property="og:url" content="{e(url)}">',
        f'<meta property="og:image" content="{e(img)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
    ]
    if site.get("google_verification"):
        tags.append(f'<meta name="google-site-verification" content="{e(site["google_verification"])}">')
    if site.get("yandex_verification"):
        tags.append(f'<meta name="yandex-verification" content="{e(site["yandex_verification"])}">')
    mid = str(site.get("yandex_metrika_id") or "").strip()
    if mid.isdigit():
        tags.append(
            '<script>(function(m,e,t,r,i,k,a){m[i]=m[i]||function(){(m[i].a=m[i].a||[]).push(arguments)};'
            'm[i].l=1*new Date();for(var j=0;j<document.scripts.length;j++){if(document.scripts[j].src===r){return;}}'
            'k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)})'
            "(window,document,'script','https://mc.yandex.ru/metrika/tag.js','ym');"
            f"ym({mid},'init',{{clickmap:true,trackLinks:true,accurateTrackBounce:true,webvisor:true}});</script>")
    if extra_ld:
        tags.append('<script type="application/ld+json">' + json.dumps(extra_ld, ensure_ascii=False) + "</script>")
    return "\n" + "\n".join(tags) + "\n"


def render_index(site, data):
    path = DOCS / "index.html"
    s = path.read_text(encoding="utf-8")
    rows = sorted(data["airports"], key=lambda r: -r["delay_min"])
    when = human_time(data["generated_at"])
    total_min = sum(r["delay_min"] for r in rows)
    total_rub = sum(r["loss_total"] for r in rows)
    top = ", ".join(f'{r["name"]} ({num(r["delay_min"])} мин)' for r in rows[:3] if r["delay_min"] > 0)

    title = "Задержки рейсов в аэропортах России сегодня — Цена задержки"
    desc = (f"На {when}: суммарная задержка вылетов в {len(rows)} аэропортах России {num(total_min)} минут, "
            f"потери около {rub(total_rub)}. Обновление каждые 2 часа.")
    ld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "Задержки вылетов в аэропортах России за сегодня",
        "description": "Суммарные минуты задержки вылетов и оценка потерь в рублях по аэропортам России. Обновление каждые 2 часа.",
        "url": site["url"], "inLanguage": "ru", "dateModified": data["generated_at"],
        "spatialCoverage": "Россия", "isAccessibleForFree": True,
        "distribution": {"@type": "DataDownload", "encodingFormat": "application/json",
                         "contentUrl": site["url"] + "data/latest.json"},
    }
    s = put(s, "head", head_tags(site, title, desc, site["url"], ld))

    summary = (f'<p class="summary">Данные на {e(when)}: суммарная задержка вылетов — '
               f'{num(total_min)} минут, потери около {rub(total_rub)}.'
               + (f" Больше всего задержек: {e(top)}." if top else "") + "</p>")
    s = put(s, "summary", summary)

    max_min = max([1] + [r["delay_min"] for r in rows])
    max_rub = max([1] + [r["loss_total"] for r in rows])
    trs = []
    for i, r in enumerate(rows, 1):
        pct = round(r["delayed"] / r["departed"] * 100) if r["departed"] else 0
        trs.append(
            f'<tr class="row"><td class="rank num">{i}</td>'
            f'<td><span class="iata code">{e(r["iata"])}</span> '
            f'<a class="apname" href="airport/{e(r["iata"].lower())}.html">{e(r["name"])}</a>'
            f'<div class="apcity">{e(r["city"])}</div></td>'
            f'<td class="r num opt">{num(r["departed"])}</td>'
            f'<td class="r num opt">{num(r["delayed"])}{f'<span class="badge{" red" if pct >= 30 else ""}">{pct}%</span>' if pct else ""}</td>'
            f'<td class="r num opt">{r["cancelled"] or "—"}</td>'
            f'<td class="barcell min"><div class="bar"><span class="val num">{num(r["delay_min"])}</span>'
            f'<span class="track"><i style="width:{r["delay_min"] / max_min * 100:.1f}%"></i></span></div></td>'
            f'<td class="barcell rub"><div class="bar"><span class="val num">{rub(r["loss_total"])}</span>'
            f'<span class="track"><i style="width:{r["loss_total"] / max_rub * 100:.1f}%"></i></span></div></td></tr>')
    s = put(s, "rows", "".join(trs))

    links = sorted(data["airports"], key=lambda r: r["city"])
    s = put(s, "links", " ".join(
        f'<a href="airport/{e(r["iata"].lower())}.html">{e(r["city"])}'
        f'{"" if r["name"] == r["city"] else " — " + e(r["name"])}</a>' for r in links))
    path.write_text(s, encoding="utf-8")
    return s


def period_totals(daily, iata, match):
    keys = ("departed", "delayed", "cancelled", "delay_min", "loss_total")
    t = dict.fromkeys(keys, 0)
    days = 0
    for d, rows in daily.items():
        if match(d) and iata in rows:
            days += 1
            for k in keys:
                t[k] += rows[iata].get(k, 0)
    return t, days


def past_section(daily, r) -> str:
    from datetime import date, timedelta
    today = date.fromisoformat(r["date"])
    yest = (today - timedelta(days=1)).isoformat()
    month = r["date"][:7]
    pm = (today.replace(day=1) - timedelta(days=1))
    prev = pm.isoformat()[:7]
    names = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь",
             "октябрь", "ноябрь", "декабрь"]
    periods = [("Вчера", lambda d: d == yest),
               (f"{names[today.month - 1].capitalize()} {today.year}", lambda d: d.startswith(month)),
               (f"{names[pm.month - 1].capitalize()} {pm.year}", lambda d: d.startswith(prev))]
    trs = []
    for label, match in periods:
        t, days = period_totals(daily, r["iata"], match)
        if not days:
            continue
        trs.append(f'<tr><td>{e(label)}</td><td class="r num">{days}</td><td class="r num">{num(t["departed"])}</td>'
                   f'<td class="r num">{num(t["delayed"])}</td><td class="r num" style="color:var(--amber);font-weight:700">{num(t["delay_min"])}</td>'
                   f'<td class="r num">{rub(t["loss_total"])}</td></tr>')
    if not trs:
        return ""
    return ('<section class="method"><h2>Задержки за прошлые периоды</h2><div class="board"><table class="flights">'
            '<thead><tr><th>Период</th><th class="r">Дней</th><th class="r">Вылетов</th><th class="r">Задержано</th>'
            '<th class="r">Минуты</th><th class="r">Потери</th></tr></thead><tbody>' + "".join(trs) +
            '</tbody></table></div><p class="src">Сумма по суткам, за которые есть данные. Текущий месяц включает сегодняшний день.</p></section>')


def render_airport(site, data, r, style, daily=None):
    when = human_time(data["generated_at"])
    slug = r["iata"].lower()
    url = f'{site["url"]}airport/{slug}.html'
    label = r["name"] if r["name"] == r["city"] else f'{r["name"]} ({r["city"]})'
    title = f"Задержки рейсов {r['name']} {r['iata']} сегодня — Цена задержки"
    desc = (f"Аэропорт {label}, {r['iata']}: на {when} задержка вылетов {num(r['delay_min'])} минут, "
            f"задержано {num(r['delayed'])} из {num(r['departed'])} вылетов, отменено {r['cancelled']}, "
            f"потери около {rub(r['loss_total'])}.")
    ld = {"@context": "https://schema.org", "@type": "Airport", "name": f"Аэропорт {label}",
          "iataCode": r["iata"], "address": {"@type": "PostalAddress", "addressLocality": r["city"], "addressCountry": "RU"}}
    pct = round(r["delayed"] / r["departed"] * 100) if r["departed"] else 0
    avg = r["loss_total"] / r["departed"] if r["departed"] else 0
    worst = "".join(
        f'<tr><td class="code">{e(f["number"])}</td><td>{e(f.get("to_name") or f.get("to") or "")}</td>'
        f'<td class="num">{local_hm(f["scheduled"], r["tz"])}</td><td class="num">{local_hm(f["actual"], r["tz"])}</td>'
        f'<td class="r num" style="color:var(--amber);font-weight:700">+{num(f["delay_min"])}{"" if f["final"] else "*"}</td>'
        f'<td class="r num">{rub(f["loss"])}</td></tr>' for f in r.get("worst", []))
    body = f"""<div class="wrap">
  <header>
    <div>
      <p style="margin:0 0 8px"><a href="../">← Все аэропорты России</a></p>
      <h1>Задержки рейсов: {e(label)}<span class="tick">.</span></h1>
      <p class="lede">Задержки вылетов из аэропорта {e(label)}, код {e(r['iata'])}, за сегодня по местному времени. Данные на {e(when)}, обновление каждые 2 часа.</p>
    </div>
  </header>
  <section class="totals" aria-label="Итого за сегодня">
    <div><div class="k">Задержка, всего</div><div class="v min num">{num(r['delay_min'])} мин</div><div class="s">{num(r['pax_hours'])} пассажиро-часов</div></div>
    <div><div class="k">Потери, всего</div><div class="v rub num">{rub(r['loss_total'])}</div><div class="s">пассажиры {rub(r['loss_pax'])} · авиакомпании {rub(r['loss_airline'])}</div></div>
    <div><div class="k">Задержано ≥ 15 мин</div><div class="v num">{num(r['delayed'])}</div><div class="s">из {num(r['departed'])} вылетов по плану{f" ({pct}%)" if r['departed'] else ""}</div></div>
    <div><div class="k">Отменено</div><div class="v num">{r['cancelled']}</div><div class="s">в среднем {rub(avg)} на рейс</div></div>
  </section>
  <section class="method">
    <h2>Самые долгие задержки сегодня</h2>
    {f'<div class="board"><table class="flights"><thead><tr><th>Рейс</th><th>Куда</th><th>План</th><th>Факт</th><th class="r">Мин</th><th class="r">Потери</th></tr></thead><tbody>{worst}</tbody></table></div><p class="src">Время местное. * рейс ещё не вылетел, задержка растёт.</p>' if worst else '<p>Задержек сегодня пока нет.</p>'}
  </section>
  {past_section(daily or {}, r)}
  <section class="method">
    <h2>Как считаются потери</h2>
    <p>Учитываются вылеты из аэропорта {e(r['name'])} за местные сутки. Минуты задержки = фактическое время отправления минус время по расписанию. Потери складываются из стоимости времени пассажиров и расходов авиакомпании на минуту простоя. Подробная методика и все коэффициенты — <a href="../#method">на главной странице</a>.</p>
  </section>
  <footer><a href="../">Задержки во всех аэропортах России</a></footer>
</div>"""
    page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<meta name="robots" content="index, follow">
<link rel="icon" href="../favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="../apple-touch-icon.png">{head_tags(site, title, desc, url, ld)}<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap">
{style}
</head>
<body>
{body}
</body>
</html>
"""
    out = DOCS / "airport" / f"{slug}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return url


def main():
    site = json.loads((ROOT / "config" / "site.json").read_text(encoding="utf-8"))
    if not site["url"].endswith("/"):
        site["url"] += "/"
    data = json.loads((DOCS / "data" / "latest.json").read_text(encoding="utf-8"))
    daily_path = DOCS / "data" / "daily.json"
    daily = json.loads(daily_path.read_text(encoding="utf-8")) if daily_path.exists() else {}
    index = render_index(site, data)
    style = re.search(r"<style>.*?</style>", index, re.S).group(0)
    urls = [site["url"]] + [render_airport(site, data, r, style, daily) for r in data["airports"]]
    lastmod = data["generated_at"]
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for i, u in enumerate(urls):
        sm.append(f"  <url><loc>{e(u)}</loc><lastmod>{lastmod}</lastmod>"
                  f"<changefreq>hourly</changefreq><priority>{'1.0' if i == 0 else '0.8'}</priority></url>")
    sm.append("</urlset>")
    (DOCS / "sitemap.xml").write_text("\n".join(sm) + "\n", encoding="utf-8")
    print(f"OK: главная + {len(urls) - 1} страниц аэропортов, sitemap.xml")


if __name__ == "__main__":
    main()
