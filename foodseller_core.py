"""
Food Seller - calculations with no database or Flask, so they can be tested alone.

The rule that stops double deduction: for one stock item on one day, what leaves
stock is max(manual preparation usage, what the day's sales needed by recipe).
The two are two views of the same usage, so "5 KG used this morning" and "50 vada
pav sold needing 5 KG" deduct 5 KG, in whichever order they were entered.
"""

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")
QTY_PLACES = Decimal("0.0001")
MONEY_PLACES = Decimal("0.01")


class CoreError(ValueError):
    pass


def dec(value, label="value"):
    if isinstance(value, bool):
        raise CoreError(f"{label} must be a number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise CoreError(f"{label} must be a number")
    if not number.is_finite():
        raise CoreError(f"{label} must be a number")
    return number


def qty4(value):
    return dec(value).quantize(QTY_PLACES, rounding=ROUND_HALF_UP)


def money2(value):
    return dec(value).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def convert_qty(quantity, from_unit, to_unit):
    """Units are dicts with id, name, dimension, to_base. Raises CoreError when incompatible."""
    if from_unit["id"] == to_unit["id"]:
        return qty4(quantity)
    if from_unit["dimension"] == "custom" or from_unit["dimension"] != to_unit["dimension"]:
        raise CoreError(f"{from_unit['name']} cannot be converted to {to_unit['name']}")
    return qty4(dec(quantity) * dec(from_unit["to_base"]) / dec(to_unit["to_base"]))


def average_cost(stock_qty, stock_value, fallback_rate):
    """Moving average cost per stock unit. Falls back to the product's purchase price
    when stock is empty or negative, where value / quantity would mean nothing."""
    stock_qty, stock_value = dec(stock_qty), dec(stock_value)
    if stock_qty > 0 and stock_value > 0:
        return stock_value / stock_qty
    return dec(fallback_rate)


def usage_target(manual_qty, recipe_qty):
    return max(dec(manual_qty), dec(recipe_qty))


def split_usage_cost(cost, manual_qty, recipe_qty):
    """Splits one day's usage cost of one item into (cost of what was sold by recipe,
    cost of manual usage beyond any recipe)."""
    cost, recipe_qty = dec(cost), dec(recipe_qty)
    effective = usage_target(manual_qty, recipe_qty)
    if effective <= 0:
        return ZERO, ZERO
    recipe_cost = cost * min(recipe_qty / effective, Decimal(1))
    return recipe_cost, cost - recipe_cost


def allocate_discount(line_totals, discount):
    """Shares a bill discount across lines by value; shares always add up exactly."""
    discount = money2(discount)
    totals = [dec(t) for t in line_totals]
    subtotal = sum(totals, ZERO)
    if subtotal <= 0 or discount <= 0:
        return [ZERO for _ in totals]
    shares, used = [], ZERO
    for i, total in enumerate(totals):
        share = discount - used if i == len(totals) - 1 else money2(discount * total / subtotal)
        share = min(share, total) if i < len(totals) - 1 else share
        shares.append(share)
        used += share
    return shares


def stock_state(stock_qty, min_alert):
    stock_qty, min_alert = dec(stock_qty), dec(min_alert)
    if stock_qty <= 0:
        return "OUT"
    if min_alert > 0 and stock_qty <= min_alert:
        return "LOW"
    return "OK"


def compute_pnl(parts):
    """parts: revenue, recipe_cost, unmatched_cost, wastage, spoilage, personal,
    adjustment_net (gain +, loss -), expenses. Personal use is the owner's drawing:
    shown, but not a business cost. Purchases are absent on purpose; they sit in
    stock until used."""
    revenue = dec(parts.get("revenue", 0))
    cogs = dec(parts.get("recipe_cost", 0)) + dec(parts.get("unmatched_cost", 0))
    gross = revenue - cogs
    operating = (dec(parts.get("expenses", 0)) + dec(parts.get("wastage", 0)) + dec(parts.get("spoilage", 0))
                 - dec(parts.get("adjustment_net", 0)))
    return {"cogs": cogs, "gross_profit": gross, "operating_costs": operating, "net_profit": gross - operating}


def purchase_suggestion(stock_qty, min_alert, avg_daily_usage, cover_days):
    """Quantity to buy so stock lasts cover_days and still stays at the alert level.
    Rounded up to one decimal so a suggestion is a quantity people actually buy."""
    need = dec(avg_daily_usage) * dec(cover_days) + dec(min_alert) - dec(stock_qty)
    if need <= 0:
        return ZERO
    return Decimal(math.ceil(need * 10)) / 10


def units_for_target(target_profit, fixed_costs, unit_profit):
    """Units to sell to earn target_profit after fixed_costs, or None if each unit earns nothing."""
    unit_profit = dec(unit_profit)
    if unit_profit <= 0:
        return None
    return max(math.ceil((dec(target_profit) + dec(fixed_costs)) / unit_profit), 0)
