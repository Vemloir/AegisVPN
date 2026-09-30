from src.handlers.user.devices import _clean_label


def test_clean_label_strips_only_build_numbers():
    assert _clean_label("Android 17800541067281831514 · Happ") == "Android · Happ"
    assert _clean_label("Honor 200 · Happ") == "Honor 200 · Happ"
    assert _clean_label("Nokia 3310 · v2RayTun") == "Nokia 3310 · v2RayTun"


def test_clean_label_escapes_client_supplied_html():
    assert _clean_label("<b>Pixel</b> & co · Happ") == "&lt;b&gt;Pixel&lt;/b&gt; &amp; co · Happ"
