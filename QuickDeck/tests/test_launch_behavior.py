from quickdeck.ui.qt_app import should_start_hidden


def test_manual_gui_is_visible_with_default_tray_behavior():
    # 回归：手动执行 `python -m quickdeck gui` 时不应隐藏到托盘。
    assert should_start_hidden(False, "tray") is False


def test_minimized_flag_hides_window():
    assert should_start_hidden(True, "window") is True


def test_background_behavior_hides_window():
    assert should_start_hidden(False, "background") is True


def test_window_behavior_stays_visible():
    assert should_start_hidden(False, "window") is False
