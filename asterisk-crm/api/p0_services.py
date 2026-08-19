"""Pure and transactional business services for the season-terrain P0.

This module deliberately contains no FastAPI routes.  Keeping the rules here
makes the later API and dashboard consumers use one semantic-stage mapping and
prevents economics logic from leaking into the static UI.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping
from uuid import UUID


MONEY = Decimal("0.01")
PERCENT = Decimal("0.00000001")

# Historical stage values are never rewritten. Every reader uses this mapping
# before applying funnel or income-recognition policy.
FUNNEL_STAGE_MAP: dict[str, str] = {
    "new": "new_lead",
    "qualified": "qualified",
    "proposal": "proposal_sent",
    "negotiation": "decision_pending",
    "won": "closed_won",
    "lost": "closed_lost",
    "new_lead": "new_lead",
    "contacted": "contacted",
    "measure_scheduled": "measure_scheduled",
    "measure_completed": "measure_completed",
    "proposal_sent": "proposal_sent",
    "decision_pending": "decision_pending",
    "contract_signed": "contract_signed",
    "prepayment_received": "prepayment_received",
    "production": "production",
    "installation_scheduled": "installation_scheduled",
    "installed": "installed",
    "closed_won": "closed_won",
    "closed_lost": "closed_lost",
    "disqualified": "disqualified",
}


def semantic_funnel_stage(stage: str) -> str:
    """Return the canonical P0 stage for a legacy or P0 database value."""

    try:
        return FUNNEL_STAGE_MAP[stage]
    except KeyError as exc:
        raise ValueError(f"Unsupported deal stage: {stage}") from exc


def _decimal(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class EconomicsSettings:
    version: int
    tax_percent: Decimal
    reserve_percent: Decimal
    rent_percent: Decimal
    manager_percent: Decimal
    marketing_percent: Decimal
    measure_percent: Decimal
    owner_ae_share_percent: Decimal
    partner_installation_share_percent: Decimal = Decimal("0")
    golden_ae_percent: Decimal = Decimal("10")
    take_ae_percent: Decimal = Decimal("8")
    max_raise_price_delta_percent: Decimal = Decimal("15")
    income_recognition_stage: str = "installed"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EconomicsSettings":
        return cls(**{field: _decimal(row[field]) if field.endswith("_percent") else row[field]
                      for field in cls.__dataclass_fields__ if field in row})


@dataclass(frozen=True)
class EconomicsInputs:
    quoted_price: Decimal
    materials_cost: Decimal = Decimal("0")
    production_cost: Decimal = Decimal("0")
    seamstress_cost: Decimal = Decimal("0")
    installation_direct_cost: Decimal = Decimal("0")
    fuel_cost: Decimal = Decimal("0")
    other_direct_cost: Decimal = Decimal("0")
    installation_mode: str = "unknown"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "EconomicsInputs":
        values = {field: row[field] for field in cls.__dataclass_fields__ if field in row}
        for field in values:
            if field != "installation_mode":
                values[field] = _decimal(values[field])
        return cls(**values)


@dataclass(frozen=True)
class EconomicsResult:
    director_profit: Decimal | None
    ae_amount: Decimal | None
    ae_percent: Decimal | None
    price_floor_ae_8: Decimal | None
    price_floor_ae_10: Decimal | None
    price_floor_ae_12: Decimal | None
    owner_income_solo: Decimal | None
    owner_income_with_partner: Decimal | None
    projected_owner_income: Decimal | None
    economics_status: str
    calculation_error: str | None


def calculate_economics(inputs: EconomicsInputs, settings: EconomicsSettings) -> EconomicsResult:
    """Calculate a reproducible economics snapshot from explicit inputs.

    Percentages are stored as human-readable percent values (``6`` means 6%).
    The function intentionally does not depend on the current date or database
    state, so an old revision can always be recomputed from its saved snapshot.
    """

    if inputs.installation_mode not in {"solo", "with_partner", "external_team", "unknown"}:
        raise ValueError("Unsupported installation mode")
    if inputs.quoted_price < 0 or any(value < 0 for value in asdict(inputs).values() if isinstance(value, Decimal)):
        raise ValueError("Economics inputs cannot be negative")

    price = inputs.quoted_price
    direct_cost = sum((
        inputs.materials_cost, inputs.production_cost, inputs.seamstress_cost,
        inputs.installation_direct_cost, inputs.fuel_cost, inputs.other_direct_cost,
    ), Decimal("0"))
    rate = sum((
        settings.tax_percent, settings.reserve_percent, settings.rent_percent,
        settings.manager_percent, settings.marketing_percent, settings.measure_percent,
    ), Decimal("0")) / Decimal("100")
    owner_share = settings.owner_ae_share_percent / Decimal("100")

    if price == 0 or owner_share <= 0:
        return EconomicsResult(None, None, None, None, None, None, None, None, None,
                               "rebuild_or_reject", "Quoted price and owner AE share must be positive")

    director_profit = price - direct_cost - price * rate
    ae_amount = director_profit * owner_share
    ae_percent = ae_amount / price

    def floor(target_percent: Decimal) -> Decimal | None:
        denominator = Decimal("1") - rate - (target_percent / Decimal("100")) / owner_share
        return None if denominator <= 0 else direct_cost / denominator

    floor_8, floor_10, floor_12 = floor(Decimal("8")), floor(Decimal("10")), floor(Decimal("12"))
    if floor_8 is None:
        return EconomicsResult(_money(director_profit), _money(ae_amount), ae_percent.quantize(PERCENT),
                               None, None, None, None, None, None, "rebuild_or_reject",
                               "Current expense structure cannot reach target AE")

    manager_and_measure_income = price * (settings.manager_percent + settings.measure_percent) / Decimal("100")
    owner_income_solo = ae_amount + manager_and_measure_income + inputs.installation_direct_cost
    owner_income_with_partner = (
        ae_amount + manager_and_measure_income
        + inputs.installation_direct_cost * (Decimal("1") - settings.partner_installation_share_percent / Decimal("100"))
    )
    projected = {
        "solo": owner_income_solo,
        "with_partner": owner_income_with_partner,
        "external_team": ae_amount + manager_and_measure_income,
        "unknown": None,
    }[inputs.installation_mode]
    required_increase = floor_8 / price - Decimal("1")
    if ae_percent >= settings.golden_ae_percent / Decimal("100"):
        status = "golden"
    elif ae_percent >= settings.take_ae_percent / Decimal("100"):
        status = "take"
    elif required_increase <= settings.max_raise_price_delta_percent / Decimal("100"):
        status = "raise_price"
    else:
        status = "rebuild_or_reject"

    return EconomicsResult(
        _money(director_profit), _money(ae_amount), ae_percent.quantize(PERCENT),
        _money(floor_8), _money(floor_10) if floor_10 is not None else None,
        _money(floor_12) if floor_12 is not None else None,
        _money(owner_income_solo), _money(owner_income_with_partner),
        _money(projected) if projected is not None else None, status, None,
    )


@dataclass(frozen=True)
class CashflowTotals:
    net_confirmed_customer_cash: Decimal
    realized_cost_outflows: Decimal
    open_reserved_obligations: Decimal
    other_reserved_cash: Decimal
    safe_cash: Decimal


def calculate_safe_cash(movements: Iterable[Mapping[str, Any]], obligations: Iterable[Mapping[str, Any]]) -> CashflowTotals:
    """Calculate only from posted movements and open obligations.

    Settlement changes an obligation from ``open`` to ``settled`` but posts a
    separate realized-cost movement, so the same cost is never lost or counted
    twice in safe cash.
    """

    incoming = refunds = realized = reserves = releases = Decimal("0")
    for movement in movements:
        amount = _decimal(movement["amount"])
        kind = movement["kind"]
        if kind == "customer_incoming":
            incoming += amount
        elif kind == "customer_refund":
            refunds += amount
        elif kind == "realized_cost_outflow":
            realized += amount
        elif kind == "realized_cost_outflow_reversal":
            realized -= amount
        elif kind == "other_reserved_cash":
            reserves += amount
        elif kind == "other_reserved_cash_release":
            releases += amount
        else:
            raise ValueError(f"Unsupported cash movement kind: {kind}")
    open_obligations = sum((_decimal(item["amount"]) for item in obligations if item["status"] == "open"), Decimal("0"))
    net = incoming - refunds
    other = reserves - releases
    return CashflowTotals(_money(net), _money(realized), _money(open_obligations), _money(other),
                          _money(net - realized - open_obligations - other))


def write_economics_revision(cursor: Any, deal_id: UUID, inputs: EconomicsInputs,
                             settings: EconomicsSettings, actor_id: UUID | None = None) -> tuple[UUID, EconomicsResult]:
    """Append an economics revision and move the deal pointer in the caller's transaction."""

    cursor.execute("SELECT id FROM deals WHERE id=%s FOR UPDATE", (deal_id,))
    if not cursor.fetchone():
        raise ValueError("Deal not found")
    cursor.execute("SELECT coalesce(max(revision), 0) + 1 FROM deal_economics_revisions WHERE deal_id=%s", (deal_id,))
    revision = cursor.fetchone()[0]
    result = calculate_economics(inputs, settings)
    cursor.execute(
        """INSERT INTO deal_economics_revisions(
               deal_id,revision,settings_version,installation_mode,quoted_price,
               materials_cost,production_cost,seamstress_cost,installation_direct_cost,fuel_cost,other_direct_cost,
               tax_percent,reserve_percent,rent_percent,manager_percent,marketing_percent,measure_percent,
               owner_ae_share_percent,partner_installation_share_percent,director_profit,ae_amount,ae_percent,
               price_floor_ae_8,price_floor_ae_10,price_floor_ae_12,owner_income_solo,owner_income_with_partner,
               projected_owner_income,economics_status,calculation_error,created_by
           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (deal_id, revision, settings.version, inputs.installation_mode, inputs.quoted_price,
         inputs.materials_cost, inputs.production_cost, inputs.seamstress_cost, inputs.installation_direct_cost,
         inputs.fuel_cost, inputs.other_direct_cost, settings.tax_percent, settings.reserve_percent,
         settings.rent_percent, settings.manager_percent, settings.marketing_percent, settings.measure_percent,
         settings.owner_ae_share_percent, settings.partner_installation_share_percent, result.director_profit,
         result.ae_amount, result.ae_percent, result.price_floor_ae_8, result.price_floor_ae_10,
         result.price_floor_ae_12, result.owner_income_solo, result.owner_income_with_partner,
         result.projected_owner_income, result.economics_status, result.calculation_error, actor_id),
    )
    revision_id = cursor.fetchone()[0]
    cursor.execute("UPDATE deals SET current_economics_revision_id=%s,updated_at=now() WHERE id=%s", (revision_id, deal_id))
    return revision_id, result


def recognize_income_for_stage(cursor: Any, deal_id: UUID, target_stage: str,
                               actor_id: UUID | None = None) -> bool:
    """Create a single immutable earned-income record when the configured stage is reached."""

    semantic_target = semantic_funnel_stage(target_stage)
    cursor.execute("SELECT id,current_economics_revision_id FROM deals WHERE id=%s FOR UPDATE", (deal_id,))
    deal = cursor.fetchone()
    if not deal:
        raise ValueError("Deal not found")
    cursor.execute(
        "SELECT version,income_recognition_stage FROM economics_settings_versions ORDER BY version DESC LIMIT 1"
    )
    settings = cursor.fetchone()
    if not settings:
        return False
    recognition_stage = settings["income_recognition_stage"] if isinstance(settings, Mapping) else settings[1]
    if semantic_target != recognition_stage:
        return False
    current_revision_id = deal["current_economics_revision_id"] if isinstance(deal, Mapping) else deal[1]
    if not current_revision_id:
        return False
    cursor.execute(
        """SELECT projected_owner_income FROM deal_economics_revisions WHERE id=%s""", (current_revision_id,)
    )
    revision = cursor.fetchone()
    projected = revision["projected_owner_income"] if isinstance(revision, Mapping) else revision[0]
    if projected is None:
        return False
    version = settings["version"] if isinstance(settings, Mapping) else settings[0]
    cursor.execute(
        """INSERT INTO deal_income_recognitions(
               deal_id,economics_revision_id,settings_version,recognition_stage,recognized_owner_income,created_by
           ) VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(deal_id) DO NOTHING RETURNING id""",
        (deal_id, current_revision_id, version, semantic_target, projected, actor_id),
    )
    return cursor.fetchone() is not None


def settle_cost_obligation(cursor: Any, obligation_id: UUID, *, occurred_at: Any, confirmed_at: Any,
                           actor_id: UUID | None = None, note: str | None = None) -> UUID:
    """Post a realized outflow and mark exactly one open obligation as settled."""

    cursor.execute("SELECT id,deal_id,amount,status FROM deal_cost_obligations WHERE id=%s FOR UPDATE", (obligation_id,))
    obligation = cursor.fetchone()
    if not obligation:
        raise ValueError("Cost obligation not found")
    status = obligation["status"] if isinstance(obligation, Mapping) else obligation[3]
    if status != "open":
        raise ValueError("Only an open cost obligation can be settled")
    deal_id = obligation["deal_id"] if isinstance(obligation, Mapping) else obligation[1]
    amount = obligation["amount"] if isinstance(obligation, Mapping) else obligation[2]
    cursor.execute(
        """INSERT INTO deal_cash_movements(deal_id,obligation_id,kind,amount,occurred_at,confirmed_at,note,created_by)
           VALUES(%s,%s,'realized_cost_outflow',%s,%s,%s,%s,%s) RETURNING id""",
        (deal_id, obligation_id, amount, occurred_at, confirmed_at, note, actor_id),
    )
    movement_id = cursor.fetchone()[0]
    cursor.execute(
        """UPDATE deal_cost_obligations
           SET status='settled',settled_at=%s,settled_movement_id=%s,updated_at=now() WHERE id=%s""",
        (confirmed_at, movement_id, obligation_id),
    )
    return movement_id
