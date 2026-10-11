from teams_vision import extract_json, map_pixel_to_point


def test_extract_json_tolerates_fences():
    assert extract_json('```json\n{"a":1}\n```')["a"] == 1
    assert extract_json('here you go {"chat_title":"x","messages":[]} ok')["chat_title"] == "x"


def test_map_pixel_to_point_retina():
    # window at (100,200) points, 800pt wide; screenshot captured at 1600px (2x retina),
    # then downscaled to 1400 for the API. A model point at x=700 in the downscaled image:
    px_w, win_w = 1600, 800.0
    down = 1600 / 1400
    scale_x = down * (win_w / px_w)   # downscaled-px -> points
    x, y = map_pixel_to_point(700, 0, 100.0, 200.0, scale_x, scale_x)
    # 700 downscaled-px -> 800 physical-px -> 400 points -> +100 offset = 500
    assert x == 500 and y == 200


def test_map_pixel_identity_non_retina():
    x, y = map_pixel_to_point(50, 60, 10.0, 20.0, 1.0, 1.0)
    assert x == 60 and y == 80
