"""壁纸：静态 / 轮换 / API。校验是安全边界，不能放松。"""

from __future__ import annotations

from relayhub.gateway import wallpaper


def test_rejects_injection_shapes():
    for bad in ("javascript:alert(1)", "data:image/png;base64,AAA", "https://a/b');x=1",
                "https://a/b c", 'https://a/"x"', "ftp://a/b"):
        assert wallpaper.clean_image(bad) == "", bad


def test_accepts_plain_http_and_https():
    assert wallpaper.clean_image("https://p.example/a.jpg")
    assert wallpaper.clean_image("http://192.168.1.2/a.jpg")


def test_api_url_requires_https():
    assert wallpaper.clean_api_url("https://api.example/wallpaper")
    assert wallpaper.clean_api_url("http://api.example/wallpaper") == ""


def test_images_deduped_and_capped():
    urls = [f"https://p/{i}.jpg" for i in range(20)] + ["https://p/0.jpg"]
    images = wallpaper.clean_images(urls)
    assert len(images) == wallpaper.MAX_IMAGES
    assert len(set(images)) == len(images)


def test_seconds_clamped():
    assert wallpaper.clamp_seconds(0) == wallpaper.MIN_SECONDS
    assert wallpaper.clamp_seconds(99999) == wallpaper.MAX_SECONDS
    assert wallpaper.clamp_seconds("abc") == wallpaper.DEFAULT_SECONDS


def test_rotate_needs_two_images_else_falls_back():
    one = wallpaper.normalize({"bg_mode": "rotate", "bg_images": ["https://p/1.jpg"]})
    assert one["mode"] == "static"
    two = wallpaper.normalize({"bg_mode": "rotate",
                               "bg_images": ["https://p/1.jpg", "https://p/2.jpg"]})
    assert two["mode"] == "rotate"
    assert wallpaper.rotate_script(two).startswith("<script>")


def test_api_mode_requires_url():
    assert wallpaper.normalize({"bg_mode": "api"})["mode"] == "static"
    ok = wallpaper.normalize({"bg_mode": "api", "bg_api_url": "https://api.example/w"})
    assert ok["mode"] == "api"
    script = wallpaper.rotate_script(ok)
    # 走本站代理：外部地址不出现在页面里
    assert "fetch(url" in script and "/panel/wallpaper" in script


def test_static_mode_has_no_script():
    assert wallpaper.rotate_script(wallpaper.normalize({"bg_images": ["https://p/1.jpg"]})) == ""


def test_script_clears_previous_timer():
    """手改配置后不能出现两条轮换同时跑。"""
    wp = wallpaper.normalize({"bg_mode": "rotate",
                              "bg_images": ["https://p/1.jpg", "https://p/2.jpg"]})
    assert "clearInterval(window.__rhWpTimer)" in wallpaper.rotate_script(wp)


def test_pick_url_handles_three_shapes():
    assert wallpaper.pick_url('{"url": "https://p/a.jpg"}') == "https://p/a.jpg"
    assert wallpaper.pick_url('{"image": "https://p/b.jpg"}') == "https://p/b.jpg"
    assert wallpaper.pick_url("https://p/c.jpg") == "https://p/c.jpg"


def test_pick_url_rejects_non_https_and_garbage():
    assert wallpaper.pick_url('{"url": "http://p/a.jpg"}') == ""
    assert wallpaper.pick_url('{"url": "javascript:alert(1)"}') == ""
    assert wallpaper.pick_url("not a url") == ""


def test_api_key_never_reaches_the_page():
    """API 模式：页面里只能出现本站代理路径，绝不能出现上游 Key。"""
    wp = wallpaper.normalize({"bg_mode": "api", "bg_api_url": "https://api.example/w",
                              "bg_api_key": "SUPER-SECRET-KEY"})
    script = wallpaper.rotate_script(wp)
    assert "/panel/wallpaper" in script
    assert "SUPER-SECRET-KEY" not in script
    assert "api.example" not in script          # 连上游域名都不给浏览器
