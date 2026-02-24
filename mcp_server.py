from __future__ import annotations

from copy import deepcopy
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Context, FastMCP


mcp = FastMCP(
    "Internet Offer Flow Server",
    instructions=(
        "Use this flow in order: "
        "1) ask user for full name and rodne_cislo_suffix (last digits), "
        "2) call authenticate_user with those values plus phone_number=731527923, "
        "3) remember returned conversation_id and pass it to all next tools, "
        "4) call download_user_info(conversation_id=...), "
        "5) call prepare_new_offer(conversation_id=...), "
        "6) ask user if they accept the offer, then call "
        "submit_offer_to_external_service(accept_offer=..., persist_to_db=..., "
        "conversation_id=...). "
        "Do not call protected tools before successful authentication."
    ),
)


AGENT_KNOWN_PHONE_NUMBER = "731527923"


MOCK_USERS: dict[str, dict[str, Any]] = {
    "jan novak": {
        "customer_id": "u-1001",
        "name": "Jan Novak",
        "rodne_cislo_suffix": "1234",
        "phone_number": AGENT_KNOWN_PHONE_NUMBER,
        "email": "jan.novak@example.com",
        "current_plan_mbps": 100,
    },
    "petra svobodova": {
        "customer_id": "u-1002",
        "name": "Petra Svobodova",
        "rodne_cislo_suffix": "5678",
        "phone_number": AGENT_KNOWN_PHONE_NUMBER,
        "email": "petra.svobodova@example.com",
        "current_plan_mbps": 100,
    },
}

# Fallback cross-call state for clients that do not reliably keep MCP sessions.
CONVERSATION_STATE: dict[str, dict[str, Any]] = {}

DB_PATH = Path(os.getenv("MOCK_DB_PATH", "data/mock_external_service.db"))


def _normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _normalize_phone(phone_number: str) -> str:
    return "".join(ch for ch in phone_number if ch.isdigit())


def _normalize_conversation_id(conversation_id: str | None) -> str | None:
    if conversation_id is None:
        return None
    normalized = conversation_id.strip()
    if not normalized:
        return None
    return normalized


def _new_conversation_id() -> str:
    return f"conv-{uuid.uuid4().hex[:12]}"


def _default_flow_state() -> dict[str, bool]:
    return {
        "authenticated": False,
        "user_info_downloaded": False,
        "offer_prepared": False,
        "submitted": False,
    }


def _authenticated_flow_state() -> dict[str, bool]:
    flow = _default_flow_state()
    flow["authenticated"] = True
    return flow


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _save_conversation_snapshot(ctx: Context, conversation_id: str | None) -> None:
    if not conversation_id:
        return
    snapshot: dict[str, Any] = {}
    for key in ("auth", "flow", "prepared_offer", "last_submission"):
        value = await ctx.get_state(key)
        if value is not None:
            snapshot[key] = deepcopy(value)
    CONVERSATION_STATE[conversation_id] = snapshot


async def _restore_conversation_snapshot(ctx: Context, conversation_id: str | None) -> bool:
    if not conversation_id:
        return False
    snapshot = CONVERSATION_STATE.get(conversation_id)
    if not snapshot:
        return False
    for key, value in snapshot.items():
        await ctx.set_state(key, deepcopy(value))
    return True


async def _require_auth(
    ctx: Context, conversation_id: str | None = None
) -> tuple[dict[str, Any], str | None]:
    normalized_conversation_id = _normalize_conversation_id(conversation_id)
    auth_state = await ctx.get_state("auth")
    if (not auth_state or not auth_state.get("authenticated")) and normalized_conversation_id:
        await _restore_conversation_snapshot(ctx, normalized_conversation_id)
        auth_state = await ctx.get_state("auth")

    if not auth_state or not auth_state.get("authenticated"):
        raise ValueError(
            "Unauthorized. First call authenticate_user(name, rodne_cislo_suffix, "
            "phone_number) and keep returned conversation_id for next tool calls."
        )
    return auth_state, normalized_conversation_id


def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_upgrade_requests (
                request_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                current_plan_mbps INTEGER NOT NULL,
                offered_plan_mbps INTEGER NOT NULL,
                status TEXT NOT NULL,
                external_reference TEXT NOT NULL
            )
            """
        )


def _write_request_to_db(
    customer: dict[str, Any],
    offer: dict[str, Any],
) -> dict[str, Any]:
    _ensure_db()
    request_id = str(uuid.uuid4())
    external_reference = f"EXT-{uuid.uuid4().hex[:10].upper()}"
    created_at = _utc_now_iso()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO external_upgrade_requests (
                request_id, created_at, customer_id, customer_name,
                current_plan_mbps, offered_plan_mbps, status, external_reference
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                created_at,
                customer["customer_id"],
                customer["name"],
                customer["current_plan_mbps"],
                offer["offered_plan_mbps"],
                "accepted",
                external_reference,
            ),
        )

    return {
        "request_id": request_id,
        "external_reference": external_reference,
        "saved_to_db": True,
        "db_path": str(DB_PATH),
        "created_at": created_at,
        "status": "accepted",
    }


def _mock_external_call() -> dict[str, Any]:
    return {
        "saved_to_db": False,
        "status": "accepted",
        "external_reference": f"MOCK-{uuid.uuid4().hex[:8].upper()}",
        "created_at": _utc_now_iso(),
    }


@mcp.tool
async def authenticate_user(
    name: Annotated[
        str,
        (
            "Full user name (for example: 'Jan Novak'). Ask user for this value if "
            "unknown."
        ),
    ],
    rodne_cislo_suffix: Annotated[
        str,
        (
            "Last digits of rodne cislo. Ask user for this value. Use only suffix, "
            "not full rodne cislo."
        ),
    ],
    ctx: Context,
    phone_number: Annotated[
        str,
        (
            "User phone number used for verification. The agent should use known "
            "mock value 731527923 unless system context says otherwise."
        ),
    ] = AGENT_KNOWN_PHONE_NUMBER,
    conversation_id: Annotated[
        str | None,
        (
            "Optional stable flow identifier. If missing, server creates one and "
            "returns it. Pass the returned conversation_id to subsequent tools."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Authenticate the user before any protected tool call.

    When to call:
    - Always first in the flow.

    What to collect from user:
    - `name`
    - `rodne_cislo_suffix`
    - Phone is already known to the agent as `731527923` (mock), so user input for
      phone is not required.

    How to call:
    - `authenticate_user(name=<from user>, rodne_cislo_suffix=<from user>, phone_number="731527923")`

    What you get:
    - `authenticated=true` on success and a `customer_id`
    - `conversation_id` that should be passed to the next tools
    - On failure, clear reason and no access to protected tools.
    """
    user = MOCK_USERS.get(_normalize_name(name))
    if not user:
        return {"authenticated": False, "reason": "Unknown user name."}

    if user["rodne_cislo_suffix"] != rodne_cislo_suffix.strip():
        return {"authenticated": False, "reason": "Invalid rodne_cislo_suffix."}

    if user["phone_number"] != _normalize_phone(phone_number):
        return {"authenticated": False, "reason": "Invalid phone_number."}

    resolved_conversation_id = _normalize_conversation_id(conversation_id) or _new_conversation_id()

    await ctx.set_state(
        "auth",
        {
            "authenticated": True,
            "customer_id": user["customer_id"],
            "name": user["name"],
            "phone_number": user["phone_number"],
        },
    )
    await ctx.set_state("flow", _authenticated_flow_state())
    await _save_conversation_snapshot(ctx, resolved_conversation_id)

    return {
        "authenticated": True,
        "customer_id": user["customer_id"],
        "name": user["name"],
        "phone_number": user["phone_number"],
        "conversation_id": resolved_conversation_id,
        "next_step": "Call download_user_info(conversation_id=<returned_value>).",
    }


@mcp.tool
async def download_user_info(
    ctx: Context,
    conversation_id: Annotated[
        str | None,
        (
            "conversation_id returned by authenticate_user. Required for clients "
            "that do not keep MCP session state between calls."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Download mocked customer profile after authentication.

    When to call:
    - After successful `authenticate_user`.

    What to collect from user:
    - Nothing new.

    What you get:
    - Customer profile, current plan speed, and contact info.
    - This unlocks `prepare_new_offer`.
    """
    auth, resolved_conversation_id = await _require_auth(ctx, conversation_id)
    customer = MOCK_USERS[_normalize_name(auth["name"])]

    flow = await ctx.get_state("flow") or _authenticated_flow_state()
    flow["user_info_downloaded"] = True
    await ctx.set_state("flow", flow)
    await _save_conversation_snapshot(ctx, resolved_conversation_id)

    return {
        "customer_id": customer["customer_id"],
        "name": customer["name"],
        "phone_number": customer["phone_number"],
        "email": customer["email"],
        "current_plan_mbps": customer["current_plan_mbps"],
        "conversation_id": resolved_conversation_id,
        "message": "User info downloaded. Next call prepare_new_offer.",
    }


@mcp.tool
async def prepare_new_offer(
    ctx: Context,
    conversation_id: Annotated[
        str | None,
        (
            "conversation_id returned by authenticate_user. Required for clients "
            "that do not keep MCP session state between calls."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Prepare a fixed upgrade offer from 100 Mbps to 250 Mbps.

    When to call:
    - After `download_user_info`.

    What to collect from user:
    - Nothing before calling.
    - After receiving the offer, ask user whether they accept it.

    What you get:
    - Offer object with speed upgrade details.
    - Then call `submit_offer_to_external_service` with user's acceptance.
    """
    auth, resolved_conversation_id = await _require_auth(ctx, conversation_id)
    flow = await ctx.get_state("flow") or _authenticated_flow_state()
    if not flow.get("user_info_downloaded"):
        raise ValueError("Flow error: call download_user_info before prepare_new_offer.")

    customer = MOCK_USERS[_normalize_name(auth["name"])]
    offer = {
        "offer_id": f"offer-{uuid.uuid4().hex[:8]}",
        "customer_id": customer["customer_id"],
        "current_plan_mbps": customer["current_plan_mbps"],
        "offered_plan_mbps": 250,
        "price_delta_czk": 0,
        "description": "Upgrade internet speed from 100 Mbps to 250 Mbps.",
        "valid_until": "2026-12-31",
    }
    await ctx.set_state("prepared_offer", offer)

    flow["offer_prepared"] = True
    await ctx.set_state("flow", flow)
    await _save_conversation_snapshot(ctx, resolved_conversation_id)

    return {
        "offer": offer,
        "conversation_id": resolved_conversation_id,
        "next_step": (
            "Ask user for acceptance, then call "
            "submit_offer_to_external_service(accept_offer=<true_or_false>, "
            "conversation_id=<same_value>)."
        ),
    }


@mcp.tool
async def submit_offer_to_external_service(
    ctx: Context,
    accept_offer: bool = True,
    persist_to_db: bool = True,
    conversation_id: Annotated[
        str | None,
        (
            "conversation_id returned by authenticate_user. Required for clients "
            "that do not keep MCP session state between calls."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Final step: submit accepted offer to external service.

    When to call:
    - After `prepare_new_offer` and after user confirms acceptance.

    What to collect from user:
    - `accept_offer` confirmation (yes/no).

    How to call:
    - `accept_offer=True` if user agrees.
    - `persist_to_db=True` to save in SQLite, otherwise use mock external response.

    What you get:
    - Final submission status and external reference ID.
    """
    auth, resolved_conversation_id = await _require_auth(ctx, conversation_id)
    flow = await ctx.get_state("flow") or _authenticated_flow_state()
    if not flow.get("offer_prepared"):
        raise ValueError("Flow error: call prepare_new_offer before submission.")

    if not accept_offer:
        return {
            "status": "cancelled",
            "conversation_id": resolved_conversation_id,
            "message": "Offer was not accepted. Nothing sent to external service.",
        }

    customer = MOCK_USERS[_normalize_name(auth["name"])]
    offer = await ctx.get_state("prepared_offer")
    if not offer:
        raise ValueError("No prepared offer found in session state.")

    external_result = (
        _write_request_to_db(customer, offer) if persist_to_db else _mock_external_call()
    )

    flow["submitted"] = True
    await ctx.set_state("flow", flow)
    await ctx.set_state("last_submission", external_result)
    await _save_conversation_snapshot(ctx, resolved_conversation_id)

    return {
        "status": "submitted",
        "customer_id": customer["customer_id"],
        "offer_id": offer["offer_id"],
        "conversation_id": resolved_conversation_id,
        "external_result": external_result,
    }


@mcp.tool
async def get_flow_status(
    ctx: Context,
    conversation_id: Annotated[
        str | None,
        (
            "Optional conversation_id returned by authenticate_user. Use this to "
            "read flow state when MCP session was recreated."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Return current auth and process status for the current session.

    When to call:
    - Any time you need to recover flow state or debug what step is next.

    What to collect from user:
    - Nothing.
    """
    resolved_conversation_id = _normalize_conversation_id(conversation_id)
    flow = await ctx.get_state("flow")
    auth = await ctx.get_state("auth")
    if (
        (not auth or not auth.get("authenticated"))
        and resolved_conversation_id
        and await _restore_conversation_snapshot(ctx, resolved_conversation_id)
    ):
        flow = await ctx.get_state("flow")
        auth = await ctx.get_state("auth")

    return {
        "authenticated": bool(auth and auth.get("authenticated")),
        "conversation_id": resolved_conversation_id,
        "flow": flow or _default_flow_state(),
    }


@mcp.tool
async def logout(
    ctx: Context,
    conversation_id: Annotated[
        str | None,
        (
            "Optional conversation_id to clear fallback flow state created by "
            "authenticate_user."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Reset session state (auth + flow + prepared offer).

    When to call:
    - End of conversation or when user wants to restart verification.

    What to collect from user:
    - Nothing.
    """
    for key in ("auth", "flow", "prepared_offer", "last_submission"):
        await ctx.delete_state(key)

    resolved_conversation_id = _normalize_conversation_id(conversation_id)
    if resolved_conversation_id:
        CONVERSATION_STATE.pop(resolved_conversation_id, None)

    return {"status": "ok", "message": "Session cleared.", "conversation_id": resolved_conversation_id}


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))
    path = os.getenv("MCP_PATH", "/mcp")
    mcp.run(transport="streamable-http", host=host, port=port, path=path)
