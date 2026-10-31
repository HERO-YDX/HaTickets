# -*- coding: UTF-8 -*-
"""Restock watching for DamaiBot: keep re-checking a sold-out event for returned tickets.

Returned tickets mostly appear 15-30 minutes after sale opens, when unpaid
orders time out. The loop refreshes at a deliberately low, jittered interval
(see ``Config.restock_interval_s``) to stay clear of risk control, and stops
on success, timeout, or anything that needs a human (captcha, login, ...).
"""

from __future__ import annotations

import json
import random
import sys
import time

from . import logger

try:
    from mobile.proc_utils import run_captured
except ImportError:  # pragma: no cover
    from proc_utils import run_captured  # type: ignore[no-redef]


def notify_user(title, message):
    """Best-effort desktop notification with sound (macOS only)."""
    if sys.platform != "darwin":
        return
    script = (
        f"display notification {json.dumps(message, ensure_ascii=False)} "
        f'with title {json.dumps(title, ensure_ascii=False)} sound name "Glass"'
    )
    try:
        run_captured(["osascript", "-e", script], timeout=5, check=False)
    except Exception:
        logger.debug("桌面通知发送失败", exc_info=True)


class RestockWatcherMixin:
    """Mixin contributing the sold-out → returned-ticket watch loop to ``DamaiBot``."""

    def _refresh_for_restock(self):
        """Close the SKU panel so the next purchase entry re-fetches inventory."""
        probe = self.probe_current_page(fast=True)
        if probe["state"] == "sku_page":
            self._press_keycode_safe(4, context="盯回流：关闭票档页刷新库存")
            time.sleep(0.5)

    def watch_restock(self):
        """Re-run the purchase flow periodically until tickets come back.

        Returns True on success; False on timeout or a stop condition.
        """
        interval_s = self.config.restock_interval_s
        deadline = time.monotonic() + self.config.restock_max_minutes * 60
        rounds = 0
        logger.info(
            f"进入盯回流模式：每 ~{interval_s}s 刷新一次，最长 {self.config.restock_max_minutes} 分钟"
        )

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, interval_s * random.uniform(0.8, 1.2)))
            if time.monotonic() >= deadline:
                break

            rounds += 1
            self._attempts_made = getattr(self, "_attempts_made", 0) + 1
            logger.info(f"盯回流第 {rounds} 轮")
            self._refresh_for_restock()
            if self.run_ticket_grabbing():
                self._log_success_outcome(f"盯回流第 {rounds} 轮成功：")
                return True

            if self._last_run_outcome == "captcha":
                logger.error("盯回流遇到验证码，已停止，请手动处理")
                notify_user("HaTickets 需要你处理", "大麦弹出验证码，盯回流已停止")
                self._set_terminal_failure("captcha")
                return False
            if self._terminal_failure_reason:
                logger.error(f"盯回流遇到不可重试失败，已停止: {self._terminal_failure_reason}")
                notify_user(
                    "HaTickets 盯回流已停止", f"原因: {self._terminal_failure_reason}"
                )
                return False

        logger.warning(f"盯回流已到最长时间，共 {rounds} 轮，未抢到")
        notify_user("HaTickets 盯回流结束", f"共刷新 {rounds} 轮，未抢到")
        return False
