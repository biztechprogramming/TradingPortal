"""Cancel one working order, or change its price, from the dashboard.

These deliberately do not take the execution lock: they exist so a working
order can be changed while a command is still watching it. A command that is
watching the order is told about the change (see ``execution.note_*``) and
follows the replacement instead of losing track of a live order.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import audit as A
from .audit import new_command_id
from .errors import OrderValidationError, TradingError
from .execution import (
    check_price_deviation,
    note_cancel,
    note_replace_failed,
    note_replace_started,
    note_replacement,
)
from .models import Order
from .pricing import round_to_tick

if TYPE_CHECKING:
    from .brokers.base import Broker
    from .orchestrator import TradingService

log = logging.getLogger("trading.order_actions")


def _open_order(service: TradingService, broker_name: str, order_id: str) -> tuple[Broker, Order]:
    broker = service.tradable_brokers.get(broker_name)
    if broker is None:
        raise OrderValidationError(f"no tradable account named {broker_name!r}")
    order_id = str(order_id or "").strip()
    if not order_id:
        raise OrderValidationError("order id is required")
    if not broker.connected:
        broker.connect()
    symbols = {s.upper() for s in service.settings.symbols}
    for order in broker.get_open_orders_for(symbols):
        if str(order.order_id) == order_id:
            return broker, order
    raise OrderValidationError(
        f"{broker_name}: order {order_id} is not a working "
        f"{'/'.join(sorted(symbols))} order (it may have just filled or ended)"
    )


def cancel_one_order(
    service: TradingService, broker_name: str, order_id: str, source: str = "api",
) -> dict[str, Any]:
    command_id = new_command_id()
    service.audit.record(
        A.COMMAND_RECEIVED, command_id=command_id, command="CANCEL_ORDER",
        source=source, broker=broker_name, order_id=order_id,
    )
    broker, order = _open_order(service, broker_name, order_id)
    note_cancel(broker.name, order.order_id)
    broker.cancel_order(order.order_id)
    final = broker.wait_until_cancelled(
        order.order_id, service.settings.cancel_confirm_timeout_seconds)
    confirmed = final.state.is_terminal
    service.audit.record(
        A.ORDER_CANCELLED if confirmed else A.ERROR,
        command_id=command_id, broker=broker.name, order_id=order.order_id,
        symbol=order.symbol, reason="cancelled from the dashboard",
        message=None if confirmed else "cancel not confirmed; it may still be working",
        order=final.to_dict(),
    )
    if not confirmed:
        raise TradingError(
            f"{broker.name}: cancel of {order.symbol} order {order.order_id} "
            "was sent but not confirmed; it may still be working. Check the broker.")
    return {"status": "OK", "broker": broker.name, "order": final.to_dict()}


def change_order_price(
    service: TradingService, broker_name: str, order_id: str,
    limit_price: float, source: str = "api",
) -> dict[str, Any]:
    command_id = new_command_id()
    service.audit.record(
        A.COMMAND_RECEIVED, command_id=command_id, command="CHANGE_ORDER",
        source=source, broker=broker_name, order_id=order_id,
        limit_price=limit_price,
    )
    try:
        price = float(limit_price)
    except (TypeError, ValueError):
        raise OrderValidationError(f"limit {limit_price!r} is not a number")
    if not price > 0:
        raise OrderValidationError("limit must be positive")
    price = round_to_tick(price, service.settings.tick_size)

    broker, order = _open_order(service, broker_name, order_id)
    quote, _ = service.display_quote(broker, order.symbol)
    if quote is not None:
        check_price_deviation(order.symbol, price, quote, service.settings)

    note_replace_started(broker.name, order.order_id)
    try:
        new_order = broker.replace_order(order.order_id, price)
    except Exception:
        note_replace_failed(broker.name, order.order_id)
        raise
    if str(new_order.order_id) != str(order.order_id):
        note_replacement(broker.name, order.order_id, new_order)
    else:
        note_replace_failed(broker.name, order.order_id)   # same id: nothing to follow
    service.audit.record(
        A.ORDER_REPLACED, command_id=command_id, broker=broker.name,
        old_order_id=order.order_id, old_limit=order.limit_price,
        new_limit=price, order=new_order.to_dict(), reason="changed from the dashboard",
    )
    return {"status": "OK", "broker": broker.name, "order": new_order.to_dict()}
