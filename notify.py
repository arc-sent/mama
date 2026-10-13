"""Текст уведомления в Telegram об итоге выкладки. Чистая функция — без БД и сети.

Итог присылает ПК (result_<id>.json от krug_upload + список не скачанных видео ``missing``):

    {"id": 3, "p_human": 86, "verdict": "человек",
     "feed_breaks": [{"minutes": 2.1, "likes": 1, "shares": 1}],
     "groups": [{"id": 7, "name": "Группа", "videos": [
         {"id": 5, "status": "ok", "site_video_id": 10088}, {"id": 6, "status": "failed", "error": "…"}]}],
     "missing": [{"id": 8, "reason": "не скачалось"}]}
"""


def _minutes(m) -> str:
    return f"{m:.1f}".rstrip("0").rstrip(".").replace(".", ",") if isinstance(m, (int, float)) else str(m)


def format_result(plan_id: int, result: dict) -> str:
    """Сводка для чата: сколько выложено, что не вышло, оценка детектора, ленты, не скачанное."""
    groups = result.get("groups") or []
    videos = [(g, v) for g in groups for v in g.get("videos", [])]
    ok = [(g, v) for g, v in videos if v.get("status") == "ok"]
    bad = [(g, v) for g, v in videos if v.get("status") != "ok"]
    missing = result.get("missing") or []
    total = len(videos) + len(missing)

    if total and len(ok) == total:
        head = "✅"
    elif ok:
        head = "⚠️"
    else:
        head = "❌"
    lines = [f"{head} Выкладка плана №{plan_id} завершена", f"Выложено: {len(ok)} из {total}"]

    for g, v in ok:
        site = f", id на сайте {v['site_video_id']}" if v.get("site_video_id") else ""
        lines.append(f"✅ {g.get('name') or g.get('id')}: видео {v.get('id')}{site}")
    for g, v in bad:
        lines.append(f"❌ {g.get('name') or g.get('id')}: видео {v.get('id')} — {str(v.get('error') or 'не вышло')[:160]}")
    for m in missing:
        lines.append(f"⚠️ Не скачалось на ПК: видео {m.get('id')} — {str(m.get('reason') or '')[:120]}")

    if result.get("p_human") is not None:
        lines.append(f"Оценка человекоподобности: {result['p_human']}% ({result.get('verdict') or '?'})")
    breaks = result.get("feed_breaks") or []
    if breaks:
        mins = sum(b.get("minutes", 0) for b in breaks)
        likes = sum(b.get("likes", 0) for b in breaks)
        shares = sum(b.get("shares", 0) for b in breaks)
        lines.append(f"Лента между видео: {len(breaks)} перерыв(а), {_minutes(mins)} мин, "
                     f"лайков {likes}, репостов {shares}")
    if result.get("finished_at"):
        lines.append(f"Завершено: {str(result['finished_at']).replace('T', ' ')}")
    return "\n".join(lines)
