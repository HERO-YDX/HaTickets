# -*- coding: UTF-8 -*-
"""Tests for PageSnapshot and the polling loops that use it."""

import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

import pytest

from mobile.config import Config
from mobile.damai_app import DamaiBot
from mobile.ui_primitives import PageSnapshot

_BAR_ID = "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"


def _snapshot(xml):
    return PageSnapshot(ET.fromstring(xml))


def _bar_xml(*texts):
    children = "".join(
        f'<node text="{text}" bounds="[10,2010][500,2090]" />' for text in texts
    )
    return (
        '<hierarchy><node bounds="[0,0][1080,2400]">'
        f'<node resource-id="{_BAR_ID}" text="" bounds="[0,2000][1080,2100]">{children}</node>'
        '<node text="已售罄" bounds="[0,100][300,200]" />'
        "</node></hierarchy>"
    )


@pytest.fixture
def bot():
    cfg = Config(
        app_package="cn.damai",
        app_activity=".launcher.splash.SplashMainActivity",
        keyword="test",
        users=["UserA"],
        city="深圳",
        date="12.06",
        price="799元",
        price_index=1,
        if_commit_order=True,
        probe_only=False,
        rush_mode=True,
    )
    bot = DamaiBot(config=cfg, setup_driver=False)
    bot.d = Mock()
    bot.driver = bot.d
    bot._has_element = Mock(side_effect=AssertionError("no u2 lookups expected"))
    return bot


class TestPageSnapshot:
    def test_collects_ids_and_texts(self):
        snap = _snapshot(
            '<hierarchy><node resource-id="a:id/x" text=" 立即提交 " />'
            '<node resource-id="" text="" /></hierarchy>'
        )
        assert snap.has_id("a:id/x")
        assert not snap.has_id("a:id/y")
        assert snap.texts == ["立即提交"]

    def test_find_text_returns_first_matching_needle(self):
        snap = _snapshot('<hierarchy><node text="请确认购买信息" /></hierarchy>')
        assert snap.find_text(("立即提交", "确认购买")) == "确认购买"
        assert snap.find_text(("立即提交",)) is None
        assert snap.has_text(("确认",))

    def test_texts_inside_id_keeps_only_nodes_within_bounds(self):
        snap = _snapshot(_bar_xml("立即购买"))
        assert snap.texts_inside_id(_BAR_ID) == ["立即购买"]

    def test_texts_inside_id_missing_container(self):
        snap = _snapshot('<hierarchy><node text="x" bounds="[0,0][1,1]" /></hierarchy>')
        assert snap.texts_inside_id(_BAR_ID) == []

    def test_page_snapshot_none_when_dump_fails(self, bot):
        bot.d.dump_hierarchy = Mock(side_effect=RuntimeError("boom"))
        assert bot._page_snapshot() is None

    def test_page_snapshot_parses_dump(self, bot):
        bot.d.dump_hierarchy = Mock(
            return_value='<hierarchy><node resource-id="a:id/x" /></hierarchy>'
        )
        assert bot._page_snapshot().has_id("a:id/x")


class TestRushPollingUsesSnapshot:
    def test_purchase_entry_detects_sku_page(self, bot):
        bot._page_snapshot = Mock(
            return_value=_snapshot(
                '<hierarchy><node resource-id="cn.damai:id/layout_sku" /></hierarchy>'
            )
        )
        bot.is_reservation_sku_mode = Mock(return_value=False)
        result = bot._wait_for_purchase_entry_result(timeout=0.5)
        assert result["state"] == "sku_page"
        assert result["reservation_mode"] is False

    def test_purchase_entry_detects_confirm_page(self, bot):
        bot._page_snapshot = Mock(
            return_value=_snapshot(
                '<hierarchy><node resource-id="cn.damai:id/checkbox" /></hierarchy>'
            )
        )
        result = bot._wait_for_purchase_entry_result(timeout=0.5)
        assert result == {"state": "order_confirm_page", "submit_button": True}

    def test_purchase_entry_times_out_without_fallback_probe(self, bot):
        bot._page_snapshot = Mock(return_value=_snapshot("<hierarchy><node /></hierarchy>"))
        result = bot._wait_for_purchase_entry_result(
            timeout=0.05, poll_interval=0.01, fallback_probe_on_timeout=False
        )
        assert result is None

    def test_purchase_entry_falls_back_to_u2_when_dump_fails(self, bot):
        bot._page_snapshot = Mock(return_value=None)
        bot._has_element = Mock(side_effect=[False, True])
        result = bot._wait_for_purchase_entry_result(timeout=0.5)
        assert result["state"] == "order_confirm_page"

    def test_submit_ready_snapshot(self, bot):
        bot._page_snapshot = Mock(
            side_effect=[
                _snapshot("<hierarchy><node /></hierarchy>"),
                _snapshot('<hierarchy><node resource-id="cn.damai:id/checkbox" /></hierarchy>'),
            ]
        )
        assert bot._wait_for_submit_ready(timeout=0.5, poll_interval=0.01) is True

    def test_submit_ready_falls_back_to_u2_when_dump_fails(self, bot):
        bot._page_snapshot = Mock(return_value=None)
        bot._has_element = Mock(return_value=True)
        assert bot._wait_for_submit_ready(timeout=0.5) is True


class TestIsSaleReadySnapshot:
    def test_ready_text_anywhere(self, bot):
        bot._page_snapshot = Mock(
            return_value=_snapshot('<hierarchy><node text="立即购买" /></hierarchy>')
        )
        assert bot._is_sale_ready() is True
        assert bot._last_sale_ready_text == "立即购买"

    def test_price_flowlayout_respects_reservation_mode(self, bot):
        bot._page_snapshot = Mock(
            return_value=_snapshot(
                '<hierarchy><node resource-id="cn.damai:id/project_detail_perform_price_flowlayout" /></hierarchy>'
            )
        )
        bot.is_reservation_sku_mode = Mock(return_value=True)
        assert bot._is_sale_ready() is False

    @pytest.mark.parametrize(
        "texts, expected",
        [(("预定",), True), (("即将开抢", "预定"), False), ((), False)],
    )
    def test_purchase_bar_texts(self, bot, texts, expected):
        bot._page_snapshot = Mock(return_value=_snapshot(_bar_xml(*texts)))
        # Empty the page-wide ready texts so the purchase-bar branch decides.
        with patch("mobile.damai_app.sale_waiter.SALE_READY_TEXTS", ()):
            assert bot._is_sale_ready() is expected

    def test_nothing_on_page(self, bot):
        bot._page_snapshot = Mock(return_value=_snapshot("<hierarchy><node /></hierarchy>"))
        assert bot._is_sale_ready() is False
