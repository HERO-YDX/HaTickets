# -*- coding: UTF-8 -*-
"""Tests for price tier fallback and the sold-out restock watch loop."""

import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, Mock, patch

import pytest

import mobile.damai_app.__main__ as main_mod
from mobile.config import Config
from mobile.damai_app import DamaiBot
from mobile.damai_app import restock_watcher
from mobile.damai_app.run_report import EXIT_SUCCESS, EXIT_TERMINAL_FAILURE
from mobile.price_selector import PriceSelector, SoldOutError

_FLOW_ID = "cn.damai:id/project_detail_perform_price_flowlayout"


def _config(**overrides):
    kwargs = dict(
        keyword="test",
        users=["UserA"],
        city=None,
        date=None,
        price="680元",
        price_index=0,
        if_commit_order=False,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _cards_xml(*cards):
    """Each card is a tuple of texts, e.g. ("680元", "缺货登记")."""
    body = ""
    for i, texts in enumerate(cards):
        inner = "".join(f'<node text="{t}" />' for t in texts)
        body += (
            f'<node class="android.widget.FrameLayout" clickable="true" '
            f'bounds="[{i * 200},0][{i * 200 + 180},100]">{inner}</node>'
        )
    return ET.fromstring(
        f'<hierarchy><node resource-id="{_FLOW_ID}" bounds="[0,0][1080,400]">{body}</node></hierarchy>'
    )


def _selector(config, xml_root):
    selector = PriceSelector(device=MagicMock(), config=config, probe=MagicMock())
    bot = Mock()
    bot._dump_hierarchy_xml = Mock(return_value=xml_root)
    bot._parse_bounds = DamaiBot._parse_bounds
    bot._click_visible_price_option = Mock(return_value=True)
    selector.set_bot(bot)
    selector._click_price_card_element = Mock(return_value=True)
    return selector


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfigFields:
    def test_defaults(self):
        cfg = _config()
        assert cfg.price_fallback_indexes is None
        assert cfg.restock_watch is False
        assert cfg.restock_interval_s == 45
        assert cfg.restock_max_minutes == 120
        assert cfg.to_dict()["restock_watch"] is False

    @pytest.mark.parametrize("value", [None, "any", [], [2, 1]])
    def test_valid_fallback(self, value):
        assert _config(price_fallback_indexes=value).price_fallback_indexes == value

    @pytest.mark.parametrize("value", ["all", [1, -1], [True], 3, ["1"]])
    def test_invalid_fallback(self, value):
        with pytest.raises(ValueError, match="price_fallback_indexes"):
            _config(price_fallback_indexes=value)

    @pytest.mark.parametrize(
        "field, value",
        [
            ("restock_watch", "yes"),
            ("restock_interval_s", 5),
            ("restock_interval_s", True),
            ("restock_max_minutes", 0),
            ("restock_max_minutes", 1.5),
        ],
    )
    def test_invalid_restock_fields(self, field, value):
        with pytest.raises(ValueError, match=field):
            _config(**{field: value})

    def test_load_config_reads_new_fields(self, tmp_path):
        path = tmp_path / "c.jsonc"
        path.write_text(
            '{"keyword": "k", "users": ["A"], "city": null, "date": null,'
            ' "price": "680元", "price_index": 0, "if_commit_order": false,'
            ' "price_fallback_indexes": "any", "restock_watch": true,'
            ' "restock_interval_s": 30, "restock_max_minutes": 60}',
            encoding="utf-8",
        )
        cfg = Config.load_config(str(path))
        assert cfg.price_fallback_indexes == "any"
        assert cfg.restock_watch is True
        assert cfg.restock_interval_s == 30
        assert cfg.restock_max_minutes == 60


# ---------------------------------------------------------------------------
# Price tier fallback
# ---------------------------------------------------------------------------


class TestPriceFallback:
    def test_disabled_by_default(self):
        selector = _selector(_config(), None)
        assert selector._fallback_enabled() is False

    def test_restock_watch_alone_enables_availability_check(self):
        selector = _selector(_config(restock_watch=True), None)
        assert selector._fallback_enabled() is True
        assert selector._fallback_candidate_indexes(3) == [0]

    def test_candidates_dedupe_and_range(self):
        selector = _selector(_config(price_index=1, price_fallback_indexes=[1, 3, 0, 9]), None)
        assert selector._fallback_candidate_indexes(4) == [1, 3, 0]

    def test_candidates_any(self):
        selector = _selector(_config(price_index=2, price_fallback_indexes="any"), None)
        assert selector._fallback_candidate_indexes(4) == [2, 0, 1, 3]

    def test_primary_available_is_clicked(self):
        selector = _selector(
            _config(price_fallback_indexes=[1]), _cards_xml(("680元",), ("880元",))
        )
        assert selector._select_price_with_fallback() is True
        selector._click_price_card_element.assert_called_once_with(0)

    def test_falls_back_when_primary_sold_out(self):
        selector = _selector(
            _config(price_fallback_indexes=[2, 1]),
            _cards_xml(("680元", "缺货登记"), ("880元",), ("1280元", "售罄")),
        )
        assert selector._select_price_with_fallback() is True
        selector._click_price_card_element.assert_called_once_with(1)

    def test_coordinate_click_fallback(self):
        selector = _selector(_config(price_fallback_indexes=[]), _cards_xml(("680元",)))
        selector._click_price_card_element = Mock(return_value=False)
        assert selector._select_price_with_fallback() is True
        selector._bot._click_visible_price_option.assert_called_once_with(0)

    def test_click_failure_returns_false(self):
        selector = _selector(_config(price_fallback_indexes=[]), _cards_xml(("680元",)))
        selector._click_price_card_element = Mock(return_value=False)
        selector._bot._click_visible_price_option = Mock(return_value=False)
        assert selector._select_price_with_fallback() is False

    def test_all_sold_out_raises(self):
        selector = _selector(
            _config(price_fallback_indexes="any"),
            _cards_xml(("680元", "缺货登记"), ("880元", "无票")),
        )
        with pytest.raises(SoldOutError):
            selector._select_price_with_fallback()
        selector._click_price_card_element.assert_not_called()

    def test_unreadable_cards_return_none(self):
        assert _selector(_config(price_fallback_indexes=[]), None)._select_price_with_fallback() is None
        empty = ET.fromstring("<hierarchy><node /></hierarchy>")
        assert _selector(_config(price_fallback_indexes=[]), empty)._select_price_with_fallback() is None

    def test_select_price_option_uses_fallback_result(self):
        selector = _selector(
            _config(price_fallback_indexes=[1]),
            _cards_xml(("680元", "售罄"), ("880元",)),
        )
        assert selector._select_price_option() is True
        selector._bot._select_price_option_fast.assert_not_called()

    def test_select_price_option_falls_through_when_unreadable(self):
        selector = _selector(_config(price_fallback_indexes=[1]), None)
        selector._bot._select_price_option_fast = Mock(return_value=True)
        assert selector._select_price_option() is True
        selector._bot._select_price_option_fast.assert_called_once()


# ---------------------------------------------------------------------------
# Restock watch loop
# ---------------------------------------------------------------------------


@pytest.fixture
def watch_bot():
    bot = DamaiBot(
        config=_config(restock_watch=True, restock_interval_s=10, restock_max_minutes=1),
        setup_driver=False,
    )
    bot.d = Mock()
    bot.driver = bot.d
    bot._refresh_for_restock = Mock()
    bot._log_success_outcome = Mock()
    return bot


class _Clock:
    """Fake monotonic clock advanced by the patched sleep."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    clk = _Clock()
    with patch.object(restock_watcher.time, "monotonic", clk.monotonic), patch.object(
        restock_watcher.time, "sleep", clk.sleep
    ):
        yield clk


def _run_results(bot, results):
    """run_ticket_grabbing side effect: (return value, outcome, terminal reason) per round."""

    def fake_run():
        ok, outcome, terminal = results.pop(0)
        bot._last_run_outcome = outcome
        bot._terminal_failure_reason = terminal
        return ok

    bot.run_ticket_grabbing = Mock(side_effect=fake_run)


class TestWatchRestock:
    def test_succeeds_after_sold_out_rounds(self, watch_bot, clock):
        _run_results(
            watch_bot,
            [(False, "sold_out", None), (False, "sold_out", None), (True, "order_submitted", None)],
        )
        assert watch_bot.watch_restock() is True
        assert watch_bot.run_ticket_grabbing.call_count == 3
        assert watch_bot._refresh_for_restock.call_count == 3
        watch_bot._log_success_outcome.assert_called_once()

    def test_interval_is_jittered_around_config(self, watch_bot, clock):
        _run_results(watch_bot, [(True, "order_submitted", None)])
        watch_bot.watch_restock()
        assert 8.0 <= clock.now <= 12.0

    def test_stops_on_captcha(self, watch_bot, clock):
        _run_results(watch_bot, [(False, "captcha", None)])
        with patch.object(restock_watcher, "notify_user") as notify:
            assert watch_bot.watch_restock() is False
        assert watch_bot._terminal_failure_reason == "captcha"
        notify.assert_called_once()

    def test_stops_on_terminal_failure(self, watch_bot, clock):
        _run_results(watch_bot, [(False, None, "reservation_only")])
        with patch.object(restock_watcher, "notify_user") as notify:
            assert watch_bot.watch_restock() is False
        assert watch_bot.run_ticket_grabbing.call_count == 1
        notify.assert_called_once()

    def test_times_out(self, watch_bot, clock):
        _run_results(watch_bot, [(False, "sold_out", None)] * 20)
        with patch.object(restock_watcher, "notify_user") as notify:
            assert watch_bot.watch_restock() is False
        assert clock.now <= 60.0
        assert 4 <= watch_bot.run_ticket_grabbing.call_count <= 8
        notify.assert_called_once()


class TestRefreshForRestock:
    def test_closes_sku_panel(self, watch_bot):
        del watch_bot._refresh_for_restock  # use the real method
        watch_bot.probe_current_page = Mock(return_value={"state": "sku_page"})
        watch_bot._press_keycode_safe = Mock(return_value=True)
        with patch.object(restock_watcher.time, "sleep"):
            watch_bot._refresh_for_restock()
        watch_bot._press_keycode_safe.assert_called_once()

    def test_leaves_detail_page_alone(self, watch_bot):
        del watch_bot._refresh_for_restock
        watch_bot.probe_current_page = Mock(return_value={"state": "detail_page"})
        watch_bot._press_keycode_safe = Mock()
        watch_bot._refresh_for_restock()
        watch_bot._press_keycode_safe.assert_not_called()


class TestNotifyUser:
    def test_darwin_runs_osascript(self):
        with patch.object(restock_watcher.sys, "platform", "darwin"), patch.object(
            restock_watcher, "run_captured"
        ) as run:
            restock_watcher.notify_user("标题", '含"引号"')
        cmd = run.call_args[0][0]
        assert cmd[:2] == ["osascript", "-e"]
        assert '"标题"' in cmd[2] and '\\"引号\\"' in cmd[2]

    def test_other_platforms_noop(self):
        with patch.object(restock_watcher.sys, "platform", "linux"), patch.object(
            restock_watcher, "run_captured"
        ) as run:
            restock_watcher.notify_user("t", "m")
        run.assert_not_called()

    def test_errors_are_swallowed(self):
        with patch.object(restock_watcher.sys, "platform", "darwin"), patch.object(
            restock_watcher, "run_captured", side_effect=OSError("no osascript")
        ):
            restock_watcher.notify_user("t", "m")


# ---------------------------------------------------------------------------
# Main entry wiring
# ---------------------------------------------------------------------------


def _main_bot(run_result, restock_watch, watch_result=False, terminal=None, commit=True):
    bot = Mock()
    bot.driver = Mock()
    bot._terminal_failure_reason = terminal
    bot._last_run_outcome = "order_submitted"
    bot._attempts_made = 1
    bot._execution_mode_key = Mock(return_value="commit")
    bot.config = Mock()
    bot.config.serial = "x"
    bot.config.restock_watch = restock_watch
    bot.config.if_commit_order = commit
    bot.run_with_retry = Mock(return_value=run_result)
    bot.watch_restock = Mock(return_value=watch_result)
    return bot


class TestMainRestockWiring:
    @pytest.fixture(autouse=True)
    def _cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def test_watch_runs_after_failed_retries(self):
        bot = _main_bot(run_result=False, restock_watch=True, watch_result=True)
        with patch.object(main_mod, "DamaiBot", return_value=bot), patch.object(
            main_mod, "notify_user"
        ) as notify:
            assert main_mod.main([]) == EXIT_SUCCESS
        bot.watch_restock.assert_called_once()
        notify.assert_called_once()

    def test_watch_skipped_on_terminal_failure(self):
        bot = _main_bot(run_result=False, restock_watch=True, terminal="reservation_only")
        with patch.object(main_mod, "DamaiBot", return_value=bot):
            assert main_mod.main([]) == EXIT_TERMINAL_FAILURE
        bot.watch_restock.assert_not_called()

    def test_no_watch_or_notify_when_disabled_and_validation(self):
        bot = _main_bot(run_result=True, restock_watch=False, commit=False)
        with patch.object(main_mod, "DamaiBot", return_value=bot), patch.object(
            main_mod, "notify_user"
        ) as notify:
            assert main_mod.main([]) == EXIT_SUCCESS
        bot.watch_restock.assert_not_called()
        notify.assert_not_called()
