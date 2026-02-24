# MCP Internet Offer Flow Server

Simple FastMCP server over Streamable HTTP transport with:
- authentication by `name` + `rodne_cislo_suffix` + phone number
- mocked user info download
- new internet offer flow (`100 Mbps -> 250 Mbps`)
- external service submission (mock response or SQLite persistence)

## Run locally

```bash
pip install "fastmcp>=3.0.2"
python mcp_server.py
```

Server endpoint: `http://localhost:8000/mcp`

## Tool flow

1. Ask user for `name` and `rodne_cislo_suffix` (last digits only).
2. Call `authenticate_user(name, rodne_cislo_suffix, phone_number="731527923")`
3. Save returned `conversation_id`
4. Call `download_user_info(conversation_id="<value from authenticate_user>")`
5. Call `prepare_new_offer(conversation_id="<same value>")`
6. Ask user if they accept the offer.
7. Call `submit_offer_to_external_service(accept_offer=true|false, persist_to_db=true|false, conversation_id="<same value>")`
8. Optional: `get_flow_status(conversation_id=...)` and `logout(conversation_id=...)`

Agent-known mock phone number for authentication:
- `731527923`

Mock users:
- `Jan Novak` + suffix `1234`
- `Petra Svobodova` + suffix `5678`

If `persist_to_db=true`, records are stored in `data/mock_external_service.db`.

## Docker

```bash
docker build -t mcp-offer-server .
docker run --rm -p 8000:8000 mcp-offer-server
```

Optional env vars:
- `MCP_HOST` (default `0.0.0.0`)
- `MCP_PORT` (default `8000`)
- `MCP_PATH` (default `/mcp`)
- `MOCK_DB_PATH` (default `data/mock_external_service.db`)

## Session compatibility

Some MCP clients recreate MCP session between tool calls. In that case, keep and pass
the `conversation_id` returned by `authenticate_user` to all following tools.
This server then restores flow state from fallback storage and continues the flow.
