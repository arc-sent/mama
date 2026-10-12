import json

from plan_builder import build_plan


def test_format_matches_spec():
    items = [
        {"id": 2, "vk_group_id": 2343, "file_path": r"C:\v\video2.mp4"},
        {"id": 1, "vk_group_id": 2343, "file_path": r"C:\v\video1.mp4"},
        {"id": 3, "vk_group_id": 77, "file_path": "/x/video3.mp4"},
    ]
    plan = build_plan(1, items, "/c/video/")
    assert plan == {
        "id": 1,
        "groups": [
            {"id": 2343, "videos": [{"id": 1, "path": "/c/video/video1.mp4"},
                                    {"id": 2, "path": "/c/video/video2.mp4"}]},
            {"id": 77, "videos": [{"id": 3, "path": "/c/video/video3.mp4"}]},
        ],
    }
    json.dumps(plan)


def test_empty():
    assert build_plan(5, [], "/c/video") == {"id": 5, "groups": []}


def test_extended_plan_has_group_name_and_descriptions():
    items = [
        {"id": 1, "vk_group_id": 7, "group_name": "G", "file_path": "/v/video1.mp4",
         "rule_description": None, "video_description": "подпись из TikTok #тег"},
        {"id": 2, "vk_group_id": 7, "group_name": "G", "file_path": "/v/video2.mp4",
         "rule_description": "из правила", "video_description": "подпись"},
        {"id": 3, "vk_group_id": 7, "group_name": "G", "file_path": "/v/video3.mp4",
         "rule_description": None, "video_description": None},
    ]
    plan = build_plan(4, items, "/c/video", extended=True)
    g = plan["groups"][0]
    assert g["name"] == "G"
    assert g["videos"][0]["description"] == "подпись из TikTok #тег"   # своя подпись видео
    assert g["videos"][1]["description"] == "из правила"                # правило главнее
    assert "description" not in g["videos"][2]                          # нечего писать
    assert "name" not in build_plan(4, items, "/c/video")["groups"][0]   # формат Telegram прежний
