from __future__ import annotations

from copy import deepcopy
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response


mcp = FastMCP(
    "Internet Offer Flow Server",
    instructions=(
        "Use this flow in order: "
        "1) ask user for rodne_cislo_suffix (last digits), "
        "2) call authenticate_user(rodne_cislo_suffix=...), "
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
BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML_PATH = BASE_DIR / "index.html"
DB_PATH = Path(os.getenv("MOCK_DB_PATH", "data/mock_external_service.db"))

DEFAULT_MOCK_USERS: list[dict[str, Any]] = [
    {
        "customer_id": "u-1001",
        "name": "Jan Novak",
        "rodne_cislo_suffix": "1234",
        "phone_number": AGENT_KNOWN_PHONE_NUMBER,
        "email": "jan.novak@example.com",
        "current_plan_mbps": 100,
    },
    {
        "customer_id": "u-1002",
        "name": "Petra Svobodova",
        "rodne_cislo_suffix": "5678",
        "phone_number": AGENT_KNOWN_PHONE_NUMBER,
        "email": "petra.svobodova@example.com",
        "current_plan_mbps": 100,
    },
]

# Fallback cross-call state for clients that do not reliably keep MCP sessions.
CONVERSATION_STATE: dict[str, dict[str, Any]] = {}


def _normalize_phone(phone_number: str) -> str:
    return "".join(ch for ch in phone_number if ch.isdigit())


def _normalize_suffix(rodne_cislo_suffix: str) -> str:
    return "".join(ch for ch in rodne_cislo_suffix if ch.isdigit())


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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "customer_id": row["customer_id"],
        "name": row["name"],
        "rodne_cislo_suffix": row["rodne_cislo_suffix"],
        "phone_number": row["phone_number"],
        "email": row["email"],
        "current_plan_mbps": row["current_plan_mbps"],
        "created_at": row["created_at"],
    }


def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mock_users (
                customer_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rodne_cislo_suffix TEXT NOT NULL UNIQUE,
                phone_number TEXT NOT NULL,
                email TEXT NOT NULL,
                current_plan_mbps INTEGER NOT NULL CHECK (current_plan_mbps > 0),
                created_at TEXT NOT NULL
            )
            """
        )
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
        count = conn.execute("SELECT COUNT(*) FROM mock_users").fetchone()[0]
        if count == 0:
            now = _utc_now_iso()
            conn.executemany(
                """
                INSERT INTO mock_users (
                    customer_id, name, rodne_cislo_suffix, phone_number, email,
                    current_plan_mbps, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        user["customer_id"],
                        user["name"],
                        user["rodne_cislo_suffix"],
                        user["phone_number"],
                        user["email"],
                        user["current_plan_mbps"],
                        now,
                    )
                    for user in DEFAULT_MOCK_USERS
                ],
            )


def _list_mock_users() -> list[dict[str, Any]]:
    _ensure_db()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT customer_id, name, rodne_cislo_suffix, phone_number, email,
                   current_plan_mbps, created_at
            FROM mock_users
            ORDER BY created_at DESC, name ASC
            """
        ).fetchall()
    return [_row_to_user(row) for row in rows]


def _get_user_by_suffix(rodne_cislo_suffix: str) -> dict[str, Any] | None:
    _ensure_db()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT customer_id, name, rodne_cislo_suffix, phone_number, email,
                   current_plan_mbps, created_at
            FROM mock_users
            WHERE rodne_cislo_suffix = ?
            """,
            (rodne_cislo_suffix,),
        ).fetchone()
    return _row_to_user(row) if row else None


def _get_user_by_customer_id(customer_id: str) -> dict[str, Any] | None:
    _ensure_db()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT customer_id, name, rodne_cislo_suffix, phone_number, email,
                   current_plan_mbps, created_at
            FROM mock_users
            WHERE customer_id = ?
            """,
            (customer_id,),
        ).fetchone()
    return _row_to_user(row) if row else None


def _create_mock_user(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    suffix = _normalize_suffix(str(payload.get("rodne_cislo_suffix", "")))
    phone_number = _normalize_phone(str(payload.get("phone_number", AGENT_KNOWN_PHONE_NUMBER)))
    email = str(payload.get("email", "")).strip().lower()
    current_plan_raw = payload.get("current_plan_mbps", 100)
    customer_id = str(payload.get("customer_id", "")).strip() or f"u-{uuid.uuid4().hex[:8]}"

    if not name:
        raise ValueError("name is required")
    if len(suffix) < 4 or len(suffix) > 10:
        raise ValueError("rodne_cislo_suffix must contain 4-10 digits")
    if not phone_number:
        raise ValueError("phone_number is required")
    if not email:
        raise ValueError("email is required")

    try:
        current_plan_mbps = int(current_plan_raw)
    except (TypeError, ValueError):
        raise ValueError("current_plan_mbps must be a number") from None
    if current_plan_mbps <= 0:
        raise ValueError("current_plan_mbps must be > 0")

    _ensure_db()
    now = _utc_now_iso()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute(
            """
            INSERT INTO mock_users (
                customer_id, name, rodne_cislo_suffix, phone_number, email,
                current_plan_mbps, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                name,
                suffix,
                phone_number,
                email,
                current_plan_mbps,
                now,
            ),
        )
    return {
        "customer_id": customer_id,
        "name": name,
        "rodne_cislo_suffix": suffix,
        "phone_number": phone_number,
        "email": email,
        "current_plan_mbps": current_plan_mbps,
        "created_at": now,
    }


def _delete_mock_user(customer_id: str) -> bool:
    _ensure_db()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.execute("DELETE FROM mock_users WHERE customer_id = ?", (customer_id,))
    return cursor.rowcount > 0


def _list_saved_requests(limit: int = 300) -> list[dict[str, Any]]:
    _ensure_db()
    safe_limit = max(1, min(limit, 5000))
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT request_id, created_at, customer_id, customer_name, current_plan_mbps,
                   offered_plan_mbps, status, external_reference
            FROM external_upgrade_requests
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _db_stats() -> dict[str, Any]:
    _ensure_db()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        users_count = conn.execute("SELECT COUNT(*) FROM mock_users").fetchone()[0]
        requests_count = conn.execute(
            "SELECT COUNT(*) FROM external_upgrade_requests"
        ).fetchone()[0]
        latest_request = conn.execute(
            """
            SELECT request_id, created_at, customer_name, offered_plan_mbps, status
            FROM external_upgrade_requests
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "users_count": users_count,
        "requests_count": requests_count,
        "latest_request": dict(latest_request) if latest_request else None,
        "db_path": str(DB_PATH),
    }


def _write_request_to_db(
    customer: dict[str, Any],
    offer: dict[str, Any],
) -> dict[str, Any]:
    _ensure_db()
    request_id = str(uuid.uuid4())
    external_reference = f"EXT-{uuid.uuid4().hex[:10].upper()}"
    created_at = _utc_now_iso()

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
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


def _require_customer(auth_state: dict[str, Any]) -> dict[str, Any]:
    customer_id = str(auth_state.get("customer_id", ""))
    if not customer_id:
        raise ValueError("Invalid auth state. Restart authentication.")
    customer = _get_user_by_customer_id(customer_id)
    if not customer:
        raise ValueError(
            "Authenticated customer no longer exists. Run authenticate_user again."
        )
    return customer


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
            "Unauthorized. First call authenticate_user(rodne_cislo_suffix=...) and "
            "keep returned conversation_id for next tool calls."
        )
    return auth_state, normalized_conversation_id


@mcp.tool
async def authenticate_user(
    rodne_cislo_suffix: Annotated[
        str,
        (
            "Last digits of rodne cislo. This is the only authorization input. "
            "Use suffix only, not full rodne cislo."
        ),
    ],
    ctx: Context,
    conversation_id: Annotated[
        str | None,
        (
            "Optional stable flow identifier. If missing, server creates one and "
            "returns it. Pass the returned conversation_id to subsequent tools."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Authenticate the user before any protected tool call using rodne_cislo_suffix only.
    """
    suffix = _normalize_suffix(rodne_cislo_suffix)
    if len(suffix) < 4 or len(suffix) > 10:
        return {
            "authenticated": False,
            "reason": "rodne_cislo_suffix must contain 4-10 digits.",
        }

    customer = _get_user_by_suffix(suffix)
    if not customer:
        return {
            "authenticated": False,
            "reason": "Unknown rodne_cislo_suffix.",
        }

    resolved_conversation_id = (
        _normalize_conversation_id(conversation_id) or _new_conversation_id()
    )

    await ctx.set_state(
        "auth",
        {
            "authenticated": True,
            "customer_id": customer["customer_id"],
            "name": customer["name"],
            "rodne_cislo_suffix": customer["rodne_cislo_suffix"],
            "phone_number": customer["phone_number"],
        },
    )
    await ctx.set_state("flow", _authenticated_flow_state())
    await _save_conversation_snapshot(ctx, resolved_conversation_id)

    return {
        "authenticated": True,
        "customer_id": customer["customer_id"],
        "name": customer["name"],
        "rodne_cislo_suffix": customer["rodne_cislo_suffix"],
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
    """
    auth, resolved_conversation_id = await _require_auth(ctx, conversation_id)
    customer = _require_customer(auth)

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
    """
    auth, resolved_conversation_id = await _require_auth(ctx, conversation_id)
    customer = _require_customer(auth)
    flow = await ctx.get_state("flow") or _authenticated_flow_state()
    if not flow.get("user_info_downloaded"):
        raise ValueError("Flow error: call download_user_info before prepare_new_offer.")

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
    """
    auth, resolved_conversation_id = await _require_auth(ctx, conversation_id)
    customer = _require_customer(auth)
    flow = await ctx.get_state("flow") or _authenticated_flow_state()
    if not flow.get("offer_prepared"):
        raise ValueError("Flow error: call prepare_new_offer before submission.")

    if not accept_offer:
        return {
            "status": "cancelled",
            "conversation_id": resolved_conversation_id,
            "message": "Offer was not accepted. Nothing sent to external service.",
        }

    offer = await ctx.get_state("prepared_offer")
    if not offer:
        raise ValueError("No prepared offer found in session state.")

    try:
        external_result = (
            _write_request_to_db(customer, offer)
            if persist_to_db
            else _mock_external_call()
        )
    except sqlite3.Error as exc:
        raise RuntimeError(f"DB write failed: {exc}") from exc

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
    """
    for key in ("auth", "flow", "prepared_offer", "last_submission"):
        await ctx.delete_state(key)

    resolved_conversation_id = _normalize_conversation_id(conversation_id)
    if resolved_conversation_id:
        CONVERSATION_STATE.pop(resolved_conversation_id, None)

    return {
        "status": "ok",
        "message": "Session cleared.",
        "conversation_id": resolved_conversation_id,
    }


@mcp.custom_route("/", methods=["GET"])
async def admin_index(_: Request) -> Response:
    if not INDEX_HTML_PATH.exists():
        return PlainTextResponse("index.html not found", status_code=500)
    return FileResponse(INDEX_HTML_PATH)


@mcp.custom_route("/index.html", methods=["GET"])
async def admin_index_file(_: Request) -> Response:
    if not INDEX_HTML_PATH.exists():
        return PlainTextResponse("index.html not found", status_code=500)
    return FileResponse(INDEX_HTML_PATH)


@mcp.custom_route("/admin/api/overview", methods=["GET"])
async def admin_overview(request: Request) -> Response:
    errors: dict[str, str] = {}
    limit_raw = request.query_params.get("limit", "300")
    try:
        limit = int(limit_raw)
        if limit <= 0:
            raise ValueError("limit must be > 0")
    except Exception:
        limit = 300
        errors["limit"] = f"Invalid limit '{limit_raw}', using default 300."

    payload: dict[str, Any] = {
        "status": "ok",
        "partial": False,
        "stats": None,
        "users": [],
        "requests": [],
        "errors": errors,
    }

    try:
        payload["stats"] = _db_stats()
    except Exception as exc:
        errors["stats"] = str(exc)

    try:
        payload["users"] = _list_mock_users()
    except Exception as exc:
        errors["users"] = str(exc)

    try:
        payload["requests"] = _list_saved_requests(limit=limit)
    except Exception as exc:
        errors["requests"] = str(exc)

    if errors:
        payload["status"] = "partial"
        payload["partial"] = True

    return JSONResponse(payload)


@mcp.custom_route("/admin/api/users", methods=["GET"])
async def admin_list_users(_: Request) -> Response:
    try:
        return JSONResponse({"users": _list_mock_users()})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/admin/api/users", methods=["POST"])
async def admin_create_user(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        user = _create_mock_user(payload)
        return JSONResponse({"user": user}, status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": "User with this customer_id or rodne_cislo_suffix already exists."},
            status_code=409,
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/admin/api/users/{customer_id}", methods=["DELETE"])
async def admin_delete_user(request: Request) -> Response:
    customer_id = request.path_params.get("customer_id", "").strip()
    if not customer_id:
        return JSONResponse({"error": "customer_id is required"}, status_code=400)
    try:
        deleted = _delete_mock_user(customer_id)
        if not deleted:
            return JSONResponse({"error": "User not found"}, status_code=404)
        return JSONResponse({"status": "deleted", "customer_id": customer_id})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/admin/api/requests", methods=["GET"])
async def admin_list_requests(request: Request) -> Response:
    try:
        limit = int(request.query_params.get("limit", "300"))
        return JSONResponse({"requests": _list_saved_requests(limit=limit)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    try:
        stats = _db_stats()
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)
    return JSONResponse({"status": "ok", "db_path": stats["db_path"]})


if __name__ == "__main__":
    _ensure_db()
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))
    path = os.getenv("MCP_PATH", "/mcp")
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    json_response = _env_bool("MCP_JSON_RESPONSE", default=False)
    stateless_http = _env_bool("MCP_STATELESS_HTTP", default=False)
    mcp.run(
        transport=transport,
        host=host,
        port=port,
        path=path,
        json_response=json_response,
        stateless_http=stateless_http,
    )
