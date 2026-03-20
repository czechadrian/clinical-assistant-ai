import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")

# ---------------------------------------------------------------------------
# HTTP clients
#
# Two clients, two responsibilities:
#
# _auth_client  — calls /auth/v1/user with the *user's* Bearer token to
#                 validate it. No default auth headers; token is injected
#                 per-request so each user's token is used in isolation.
#
# _db_client    — calls /rest/v1/* with the *service role* key. This key
#                 bypasses RLS, which is intentional: we've already verified
#                 the user upstream and will enforce ownership in the query
#                 itself (e.g. user_id=eq.<verified_id>). Never expose this
#                 key to the frontend.
#
# Both are module-level singletons created at startup and closed at shutdown.
# A single AsyncClient reuses its connection pool across all requests, which
# is far more efficient than opening a new TCP connection per request.
# ---------------------------------------------------------------------------

_auth_client: httpx.AsyncClient
_db_client: httpx.AsyncClient


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _auth_client, _db_client
    _auth_client = httpx.AsyncClient(base_url=SUPABASE_URL)
    _db_client = httpx.AsyncClient(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
    )
    yield
    await _auth_client.aclose()
    await _db_client.aclose()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)

bearer_scheme = HTTPBearer()


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


async def get_supabase_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """Validate the Bearer token against Supabase and return the user object.

    user["id"] from this response is the only safe source of user_id.
    Never read user_id from the request body.
    """
    response = await _auth_client.get(
        "/auth/v1/user",
        headers={
            "Authorization": f"Bearer {credentials.credentials}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    if response.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return response.json()


# ---------------------------------------------------------------------------
# Supabase REST helper
# ---------------------------------------------------------------------------


async def _db_insert(table: str, payload: dict) -> dict:
    """Insert one row and return it. Raises 500 on failure."""
    response = await _db_client.post(
        f"/{table}",
        json=payload,
        headers={"Prefer": "return=representation"},
    )
    if response.status_code not in (200, 201):
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"DB insert failed: {table}")
    return response.json()[0]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    conversation_id: str | None = None  # omit to start a new conversation
    message: str


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str


class ChatResponse(BaseModel):
    conversation_id: str
    user_message: MessageOut
    assistant_message: MessageOut


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/whoami")
async def whoami(user: dict = Depends(get_supabase_user)):
    return {"user_id": user["id"], "email": user.get("email")}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, user: dict = Depends(get_supabase_user)):
    user_id: str = user["id"]  # authoritative — comes from Supabase, not the client

    # 1. Resolve conversation ------------------------------------------------
    if body.conversation_id is None:
        conversation = await _db_insert("conversations", {"user_id": user_id})
        conversation_id: str = conversation["id"]
    else:
        # Verify the conversation exists AND belongs to this user.
        # Filtering on both id and user_id means a wrong user gets 403,
        # not a data leak.
        response = await _db_client.get(
            "/conversations",
            params={"id": f"eq.{body.conversation_id}", "user_id": f"eq.{user_id}", "select": "id"},
        )
        rows = response.json()
        if not rows:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversation not found or access denied")
        conversation_id = rows[0]["id"]

    # 2. Insert user message -------------------------------------------------
    user_message = await _db_insert(
        "messages",
        {"conversation_id": conversation_id, "role": "user", "content": body.message, "user_id": user_id},
    )

    # 3. Generate mock reply -------------------------------------------------
    reply_content = f"[mock] You said: {body.message}"

    # 4. Insert assistant message --------------------------------------------
    assistant_message = await _db_insert(
        "messages",
        {"conversation_id": conversation_id, "role": "assistant", "content": reply_content, "user_id": user_id},
    )

    return ChatResponse(
        conversation_id=conversation_id,
        user_message=MessageOut(**user_message),
        assistant_message=MessageOut(**assistant_message),
    )
