"""Повтор выкладки: план только из видео, которые в прошлый раз не вышли. Чистые функции — без БД и сети.

Кнопка «🔁 Перевыложить не вышедшие» под уведомлением об итоге создаёт новый план (свой id,
``"retry_of": <id исходного плана>``) из тех же видео и тех же файлов. ПК раз в несколько минут
спрашивает ``GET /plan/retry`` и, если такой план есть, сразу выкладывает его.

Повторяются только видео, которым повтор может помочь: ``failed`` и ``not_attempted`` с ошибкой
выкладки. Не повторяются:
  * ``ok`` — уже выложено;
  * ``pending`` — «Опубликовать» нажато, но выход не подтверждён: клип мог выйти, повтор дал бы дубль;
  * ошибки самого файла (длиннее 3 минут, нет видеодорожки, нет файла) и группы вне списка
    разрешённых — повтор их не исправит.
"""

import copy

from plan_builder import plan_groups

RETRY_STATUSES = {"failed", "not_attempted"}
# по этим словам в тексте ошибки видно, что повтор не поможет
NOT_RETRYABLE = (
    "VK takes clips of 1 s to 3 min",           # длиннее 3 минут
    "no video stream",                          # только звук
    "allow-list",                               # группа не разрешена на ПК
    "no such file",                             # файла нет на ПК
)


def is_retryable(video: dict) -> bool:
    if video.get("status") not in RETRY_STATUSES:
        return False
    error = str(video.get("error") or "")
    return not any(marker in error for marker in NOT_RETRYABLE)


def retryable(result: dict) -> list[tuple]:
    """``[(id группы, id видео)]`` — что из итога имеет смысл выложить ещё раз."""
    return [(g.get("id"), v.get("id")) for g in result.get("groups") or []
            for v in g.get("videos") or [] if is_retryable(v)]


def build_retry_plan(body: dict, keys, new_id: int, source_id: int) -> dict:
    """Копия плана ``body`` только с видео из ``keys`` (пары группа/видео); пустые группы и аккаунты
    убираются. Поле ``retry_of`` — id исходного плана."""
    wanted = {(str(g), str(v)) for g, v in keys}
    plan = copy.deepcopy(body)
    plan["id"] = new_id
    plan["retry_of"] = source_id
    plan.pop("acked", None)
    if isinstance(plan.get("accounts"), list):
        for acc in plan["accounts"]:
            acc["groups"] = _filter_groups(acc.get("groups") or [], wanted)
        plan["accounts"] = [a for a in plan["accounts"] if a["groups"]]
    else:
        plan["groups"] = _filter_groups(plan.get("groups") or [], wanted)
    return plan


def _filter_groups(groups: list, wanted: set) -> list:
    out = []
    for g in groups:
        videos = [v for v in g.get("videos") or [] if (str(g.get("id")), str(v.get("id"))) in wanted]
        if videos:
            out.append({**g, "videos": videos})
    return out


def count_videos(plan: dict) -> int:
    return sum(len(g.get("videos") or []) for g in plan_groups(plan))
