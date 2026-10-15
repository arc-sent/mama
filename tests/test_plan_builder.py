import json

from plan_builder import build_plan, plan_groups


def test_format_matches_spec():
    items = [
        {"id": 2, "vk_group_id": 2343, "file_path": r"C:\v\video2.mp4", "account_vk_id": 11, "profile_id": "682d1e9fb6f4"},
        {"id": 1, "vk_group_id": 2343, "file_path": r"C:\v\video1.mp4", "account_vk_id": 11, "profile_id": "682d1e9fb6f4"},
        {"id": 3, "vk_group_id": 77, "file_path": "/x/video3.mp4", "account_vk_id": 22, "profile_id": None},
        {"id": 4, "vk_group_id": 55, "file_path": "/x/video4.mp4", "account_vk_id": 11, "profile_id": "682d1e9fb6f4"},
    ]
    plan = build_plan(1, items, "/c/video/")
    assert plan == {
        "id": 1,
        "accounts": [
            {"id": 11, "profile": "682d1e9fb6f4", "groups": [
                {"id": 2343, "videos": [{"id": 1, "path": "/c/video/video1.mp4"},
                                        {"id": 2, "path": "/c/video/video2.mp4"}]},
                {"id": 55, "videos": [{"id": 4, "path": "/c/video/video4.mp4"}]},
            ]},
            {"id": 22, "profile": None, "groups": [
                {"id": 77, "videos": [{"id": 3, "path": "/c/video/video3.mp4"}]},
            ]},
        ],
    }
    json.dumps(plan)
    assert [g["id"] for g in plan_groups(plan)] == [2343, 55, 77]


def test_groups_without_account_go_to_null_account():
    items = [{"id": 1, "vk_group_id": 7, "file_path": "/v/video1.mp4"}]          # нет аккаунта
    assert build_plan(2, items, "/c/video") == {
        "id": 2, "accounts": [{"id": None, "profile": None, "groups": [{"id": 7, "videos": [
            {"id": 1, "path": "/c/video/video1.mp4"}]}]}]}


def test_empty():
    assert build_plan(5, [], "/c/video") == {"id": 5, "accounts": []}


def test_plan_groups_reads_the_old_format_too():
    old = {"id": 1, "groups": [{"id": 1, "videos": []}]}
    assert plan_groups(old) is old["groups"]
    assert plan_groups({"id": 1}) == []


def test_extended_plan_has_names_and_descriptions():
    common = {"vk_group_id": 7, "group_name": "G", "account_vk_id": 11, "account_name": "Павел",
              "profile_id": "682d1e9fb6f4", "profile_name": "Профиль 1"}
    items = [
        {"id": 1, "file_path": "/v/video1.mp4", "rule_description": None,
         "video_description": "подпись из TikTok #тег", **common},
        {"id": 2, "file_path": "/v/video2.mp4", "rule_description": "из правила",
         "video_description": "подпись", **common},
        {"id": 3, "file_path": "/v/video3.mp4", "rule_description": None, "video_description": None, **common},
    ]
    plan = build_plan(4, items, "/c/video", extended=True)
    acc = plan["accounts"][0]
    assert (acc["id"], acc["profile"], acc["name"], acc["profile_name"]) == (11, "682d1e9fb6f4", "Павел", "Профиль 1")
    g = acc["groups"][0]
    assert g["name"] == "G"
    assert g["videos"][0]["description"] == "подпись из TikTok #тег"   # своя подпись видео
    assert g["videos"][1]["description"] == "из правила"                # правило главнее
    assert "description" not in g["videos"][2]                          # нечего писать
    short = build_plan(4, items, "/c/video")                            # формат Telegram: без имён
    assert "name" not in short["accounts"][0] and "name" not in short["accounts"][0]["groups"][0]
