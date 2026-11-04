"""Считает статистику канала: что набирает просмотры, а что нет."""
from datetime import datetime, timedelta, timezone
from statistics import mean, median

try:
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # нет базы часовых поясов
    MSK = timezone(timedelta(hours=3))

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _len_bucket(text: str) -> str:
    n = len(text)
    if n == 0:
        return "без текста"
    if n < 200:
        return "короткий (<200 зн.)"
    if n < 800:
        return "средний (200–800 зн.)"
    return "длинный (>800 зн.)"


def _hour_bucket(h: int) -> str:
    if 6 <= h < 11:
        return "утро 6–11"
    if 11 <= h < 16:
        return "день 11–16"
    if 16 <= h < 21:
        return "вечер 16–21"
    return "ночь 21–6"


def _group(posts, key, base):
    groups = {}
    for p in posts:
        groups.setdefault(key(p), []).append(p["views"])
    out = []
    for k, vals in groups.items():
        avg = mean(vals)
        out.append({"key": k, "count": len(vals), "avg": round(avg), "vs_median": round(avg / base, 2) if base else 0})
    out.sort(key=lambda g: g["avg"], reverse=True)
    return out


def analyze(info: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    all_posts = [p for p in info["posts"] if p.get("date")]
    # свежие посты ещё набирают просмотры — не сравниваем их
    posts = [p for p in all_posts if now - p["date"] >= timedelta(hours=24) and p["views"] > 0]
    if len(posts) < 5:
        posts = [p for p in all_posts if p["views"] > 0]
    if not posts:
        return {"ok": False, "reason": "нет постов с просмотрами"}

    views = [p["views"] for p in posts]
    med = median(views)
    for p in posts:
        p["local"] = p["date"].astimezone(MSK)

    dates = sorted(p["date"] for p in posts)
    span_days = max((dates[-1] - dates[0]).total_seconds() / 86400, 1)
    per_week = round(len(posts) / span_days * 7, 1)

    subs = info.get("subscribers")
    top = sorted(posts, key=lambda p: p["views"], reverse=True)[:5]
    worst = sorted(posts, key=lambda p: p["views"])[:3]

    return {
        "ok": True,
        "username": info.get("username"),
        "title": info.get("title"),
        "description": info.get("description"),
        "subscribers": subs,
        "posts_analyzed": len(posts),
        "period_days": round(span_days),
        "posts_per_week": per_week,
        "avg_views": round(mean(views)),
        "median_views": round(med),
        "reach_pct": round(med / subs * 100, 1) if subs else None,
        "by_type": _group(posts, lambda p: p["type"], med),
        "by_length": _group(posts, lambda p: _len_bucket(p["text"]), med),
        "by_weekday": _group(posts, lambda p: WEEKDAYS[p["local"].weekday()], med),
        "by_time": _group(posts, lambda p: _hour_bucket(p["local"].hour), med),
        "top": [_brief(p, med) for p in top],
        "worst": [_brief(p, med) for p in worst],
        "forwards_share": round(sum(p["forward"] for p in posts) / len(posts) * 100),
    }


def _brief(p, med):
    return {
        "id": p["id"],
        "views": p["views"],
        "x_median": round(p["views"] / med, 1) if med else 0,
        "type": p["type"],
        "date": p["local"].strftime("%d.%m %H:%M"),
        "text": (p["text"][:300] + "…") if len(p["text"]) > 300 else p["text"],
    }


def fmt_num(n) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 100_000:
        return f"{n / 1000:.0f}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


def plural(n, one, few, many) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _diff(x: float) -> str:
    pct = round((x - 1) * 100)
    return f"на {pct}% больше" if pct > 0 else f"на {-pct}% меньше"


def stats_text(a: dict) -> str:
    """Короткая сводка цифрами — отправляется пользователю до/без ИИ."""
    lines = [f"📊 {a['title'] or '@' + a['username']} (@{a['username']})"]
    if a["subscribers"]:
        lines.append(f"Подписчиков: {fmt_num(a['subscribers'])}")
    n = a["posts_analyzed"]
    lines.append(
        f"Разобрал {n} {plural(n, 'пост', 'поста', 'постов')} за {a['period_days']} дн. · "
        f"{a['posts_per_week']} в неделю"
    )
    reach = f" — это {a['reach_pct']}% подписчиков" if a["reach_pct"] is not None else ""
    m = a["median_views"]
    lines.append(f"Обычный пост набирает {fmt_num(m)} {plural(m, 'просмотр', 'просмотра', 'просмотров')}{reach}")

    def best(groups, min_count=2):
        g = [x for x in groups if x["count"] >= min_count]
        return g[0] if g else None

    t = best(a["by_type"])
    if t and len([x for x in a["by_type"] if x["count"] >= 2]) > 1:
        if t["vs_median"] >= 1.15:
            lines.append(f"Лучший формат: {t['key']} — {_diff(t['vs_median'])} просмотров, чем обычно")
        else:
            lines.append("Формат (видео/фото/текст) на просмотры почти не влияет")
    d = best(a["by_weekday"])
    h = best(a["by_time"])
    if d and h:
        lines.append(f"Лучшие день и время: {d['key']}, {h['key']} ({_diff(max(d['vs_median'], h['vs_median']))} просмотров)")
    top = a["top"][0]
    v = top["views"]
    lines.append(
        f"Самый сильный пост: {fmt_num(v)} {plural(v, 'просмотр', 'просмотра', 'просмотров')} "
        f"(×{top['x_median']} к обычному) — t.me/{a['username']}/{top['id']}"
    )
    return "\n".join(lines)


def compact_for_ai(a: dict) -> str:
    """Плотная выжимка для промпта ИИ."""
    def g(groups):
        return "; ".join(f"{x['key']}: {x['count']} шт, ср. {x['avg']} (×{x['vs_median']})" for x in groups)

    out = [
        f"Канал @{a['username']} «{a['title']}»",
        f"Описание: {a['description'] or '—'}",
        f"Подписчики: {a['subscribers'] or 'неизвестно'}; постов разобрано: {a['posts_analyzed']} за {a['period_days']} дн.; "
        f"частота: {a['posts_per_week']}/нед; медиана просмотров: {a['median_views']}; средние: {a['avg_views']}; "
        f"охват медианы: {a['reach_pct']}%; репостов из других каналов: {a['forwards_share']}%",
        f"По формату: {g(a['by_type'])}",
        f"По длине текста: {g(a['by_length'])}",
        f"По дням недели: {g(a['by_weekday'])}",
        f"По времени (МСК): {g(a['by_time'])}",
        "ЛУЧШИЕ ПОСТЫ:",
    ]
    for p in a["top"]:
        out.append(f"- {p['views']} просм. (×{p['x_median']}), {p['type']}, {p['date']}: {p['text'] or '[без текста]'}")
    out.append("СЛАБЫЕ ПОСТЫ:")
    for p in a["worst"]:
        out.append(f"- {p['views']} просм. (×{p['x_median']}), {p['type']}, {p['date']}: {p['text'] or '[без текста]'}")
    return "\n".join(out)
