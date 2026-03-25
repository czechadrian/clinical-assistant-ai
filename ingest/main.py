import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from guardrails import detect_pii
from policy import PROMPT_VERSION, SYSTEM_PROMPT  # noqa: F401  (used in future Claude integration)
from settings import Settings, configure_logging

load_dotenv()

settings = Settings.from_env()
configure_logging(settings.app_env)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP clients
#
# Two clients, two responsibilities:
#
# _auth_client  — calls /auth/v1/user with the *user's* Bearer token to
#                 validate it. No default auth headers; token is injected
#                 per-request so each user's token is used in isolation.
#
# _db_client    — calls /rest/v1/* using the service role key as *apikey*
#                 (project identification at the API-gateway level only) and
#                 injects the *user's* JWT as Authorization per-request so
#                 PostgREST still evaluates RLS as that specific user.
#                 Never expose the service role key to the frontend.
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
    _auth_client = httpx.AsyncClient(base_url=settings.supabase_url)
    _db_client = httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={
            # apikey identifies the project at the API-gateway level.
            # Authorization is injected per-request with the user's JWT
            # so PostgREST evaluates RLS as that specific user.
            "apikey": settings.supabase_service_role_key,
            "Content-Type": "application/json",
        },
    )
    logger.info(
        "startup",
        extra={"app_env": settings.app_env, "git_commit": settings.git_commit},
    )
    yield
    await _auth_client.aclose()
    await _db_client.aclose()
    logger.info("shutdown")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=settings.cors_origin_regex,
    allow_methods=["GET", "POST"],  # only what this API actually exposes
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,  # Bearer tokens, not cookies
)

bearer_scheme = HTTPBearer()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@dataclass
class Auth:
    user_id: str
    jwt: str  # raw Bearer token — forwarded to PostgREST so RLS runs as this user


async def _validate_token(token: str) -> dict:
    """Call Supabase Auth to verify the token. Returns the user object."""
    response = await _auth_client.get(
        "/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": settings.supabase_service_role_key,
        },
    )
    if response.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return response.json()


async def get_auth(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),  # noqa: B008
) -> Auth:
    """FastAPI dependency — validates JWT and returns Auth(user_id, jwt)."""
    user = await _validate_token(credentials.credentials)
    return Auth(user_id=user["id"], jwt=credentials.credentials)


async def get_supabase_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),  # noqa: B008
) -> dict:
    """Legacy dependency kept for /whoami. Prefer get_auth for new endpoints."""
    return await _validate_token(credentials.credentials)


# ---------------------------------------------------------------------------
# Supabase REST helpers
#
# Both helpers accept the caller's JWT and inject it as Authorization.
# PostgREST evaluates RLS policies against auth.uid() from that JWT, so
# each user can only read/write their own rows — no extra filtering needed.
# ---------------------------------------------------------------------------


def _rls_headers(jwt: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jwt}"}


async def _db_insert(table: str, row: dict, jwt: str) -> dict:
    """Insert one row and return it. RLS applies via the caller's JWT."""
    response = await _db_client.post(
        f"/{table}",
        json=row,
        headers={**_rls_headers(jwt), "Prefer": "return=representation"},
    )
    if response.status_code not in (200, 201):
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"DB insert failed: {table}")
    return response.json()[0]


async def _db_select(path: str, params: dict, jwt: str) -> list[dict]:
    """Run a GET query against PostgREST. RLS applies via the caller's JWT."""
    response = await _db_client.get(path, params=params, headers=_rls_headers(jwt))
    if not response.is_success:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"DB query failed: {path}")
    return response.json()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class ConversationOut(BaseModel):
    id: str
    title: str | None
    created_at: str
    updated_at: str


class ChatRequest(BaseModel):
    mode: Literal["triage", "summary", "patient_message"]
    input_text: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str  # required — create via POST /conversations first


class Source(BaseModel):
    """A guideline or reference cited in the response."""

    id: str
    title: str
    section: str


class AssistantPayload(BaseModel):
    """Structured content returned for every assistant turn.

    questions_to_ask      — clarifying questions when the clinical picture is incomplete.
    red_flags             — symptoms/signs that require immediate escalation; empty if none.
    possible_next_steps   — suggested diagnostic or management steps in priority order.
    patient_facing_summary— plain-language explanation suitable for the patient; no diagnosis.
    sources               — guidelines cited; empty list when uncertain about existence.
    flag                  — "safe": answered normally,
                            "uncertain": partial answer / needs clarification,
                            "refuse": outside safe scope, not answered.
    disclaimer            — mandatory safety reminder (never omit).
    """

    questions_to_ask: list[str]
    red_flags: list[str]
    possible_next_steps: list[str]
    patient_facing_summary: str
    sources: list[Source]
    flag: Literal["safe", "uncertain", "refuse"]
    disclaimer: str


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: dict[str, Any]  # JSONB: {"text": "..."} for user, AssistantPayload for assistant


class ResponseMetadata(BaseModel):
    """Audit metadata returned alongside every chat response.

    Lets callers (and audit logs) know exactly what produced the answer:
    mock vs. real model, which model ID, and which prompt version was active.
    Not stored in the messages table — it lives in ChatResponse only.
    """

    is_mock: bool
    model: str  # "mock-v1" | "claude-opus-4-6" | etc.
    prompt_version: str  # matches policy.PROMPT_VERSION


class ChatResponse(BaseModel):
    request_id: str  # UUID v4 — use for logging and client-side deduplication
    assistant_payload: AssistantPayload
    response_metadata: ResponseMetadata


# ---------------------------------------------------------------------------
# Mock payload factory
#
# Returns a schema-valid AssistantPayload tailored to each mode.
# Replace with a real Claude call (using SYSTEM_PROMPT) in the next sprint.
# ---------------------------------------------------------------------------

_DISCLAIMER = (
    "Asystent AI nie zastepuje porady lekarskiej ani decyzji klinicznej. "
    "Zawsze konsultuj sie z wykwalifikowanym specjalista."
)


def _build_mock_payload(mode: str) -> AssistantPayload:
    if mode == "triage":
        return AssistantPayload(
            questions_to_ask=[
                "Jak dlugo trwaja objawy?",
                "Czy wystepuja objawy alarmowe (bol w klatce piersiowej, dusznosc, utrata przytomnosci)?",
                "Jakie leki przyjmuje pacjent na stale?",
            ],
            red_flags=[
                "Bol w klatce piersiowej promieniujacy do lewego ramienia lub szczeki — wykluczyc OZW.",
                "Nagla dusznosc spoczynkowa — pilna ocena ukladu oddechowego i krazenia.",
            ],
            possible_next_steps=[
                "Zebranie pelnego wywiadu lekarskiego i badanie fizykalne.",
                "EKG 12-odpr. jesli podejrzenie OZW.",
                "W przypadku objawow zagrazajacych zyciu — natychmiastowe skierowanie na SOR.",
            ],
            patient_facing_summary=(
                "Lekarz zbiera informacje o Twoich objawach, "
                "aby ocenic, jaka pomoc jest Ci potrzebna."
            ),
            sources=[],
            flag="uncertain",
            disclaimer=_DISCLAIMER,
        )

    if mode == "summary":
        return AssistantPayload(
            questions_to_ask=[],
            red_flags=[],
            possible_next_steps=[
                "Przekazac podsumowanie kliniczne lekarzowi prowadzacemu.",
                "Zaktualizowac dokumentacje medyczna pacjenta.",
            ],
            patient_facing_summary=(
                "Ponizej przedstawiono podsumowanie wizyty. "
                "W razie pytan prosimy o kontakt z lekarzem."
            ),
            sources=[
                Source(
                    id="mock-summary-001",
                    title="Standardy dokumentacji medycznej PTL",
                    section="Rozdzial 3 — Podsumowanie wizyty ambulatoryjnej",
                )
            ],
            flag="safe",
            disclaimer=_DISCLAIMER,
        )

    # mode == "patient_message"
    return AssistantPayload(
        questions_to_ask=[
            "Czy rozumiesz zalecenia lekarza i nie masz dodatkowych pytan?",
        ],
        red_flags=[],
        possible_next_steps=[
            "Przyjmuj leki zgodnie z zaleceniami — nie przerywaj terapii samodzielnie.",
            "Jesli objawy sie nasilaja lub pojawia sie nowe — skontaktuj sie z lekarzem.",
            "W nagłym przypadku zadzwon pod numer alarmowy 112.",
        ],
        patient_facing_summary=(
            "Twoj lekarz przygotowal dla Ciebie te informacje. "
            "Prosimy o zapoznanie sie z nimi i przestrzeganie zalecen."
        ),
        sources=[],
        flag="safe",
        disclaimer="Te informacje maja charakter pomocniczy i nie zastepuja indywidualnej porady lekarskiej.",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/version")
async def version():
    return {
        "git_commit": settings.git_commit,
        "app_env": settings.app_env,
    }


@app.get("/whoami")
async def whoami(user: dict = Depends(get_supabase_user)):  # noqa: B008
    return {"user_id": user["id"], "email": user.get("email")}


@app.post("/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest,
    auth: Auth = Depends(get_auth),  # noqa: B008
):
    row = await _db_insert(
        "conversations",
        {"user_id": auth.user_id, "title": body.title},
        auth.jwt,
    )
    return ConversationOut(**row)


@app.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(auth: Auth = Depends(get_auth)):  # noqa: B008
    rows = await _db_select(
        "/conversations",
        {
            "user_id": f"eq.{auth.user_id}",
            "order": "updated_at.desc",
            "select": "id,title,created_at,updated_at",
        },
        auth.jwt,
    )
    return [ConversationOut(**r) for r in rows]


@app.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def get_messages(
    conversation_id: str,
    auth: Auth = Depends(get_auth),  # noqa: B008
):
    # RLS will silently return [] if the conversation belongs to another user,
    # which we surface as 404 rather than leaking that the conversation exists.
    conv_rows = await _db_select(
        "/conversations",
        {"id": f"eq.{conversation_id}", "user_id": f"eq.{auth.user_id}", "select": "id"},
        auth.jwt,
    )
    if not conv_rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    rows = await _db_select(
        "/messages",
        {
            "conversation_id": f"eq.{conversation_id}",
            "order": "created_at.asc",
            "select": "id,conversation_id,role,content",
        },
        auth.jwt,
    )
    return [MessageOut(**r) for r in rows]


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, auth: Auth = Depends(get_auth)):  # noqa: B008
    request_id = str(uuid.uuid4())
    start = time.perf_counter()
    user_id = auth.user_id
    input_length = len(body.input_text)
    status_code = 200

    # Logged on arrival — never includes raw content.
    logger.info(
        "chat_request",
        extra={
            "request_id": request_id,
            "user_id": user_id,
            "conversation_id": body.conversation_id,
            "mode": body.mode,
            "input_length": input_length,
        },
    )

    try:
        # 0. PII guardrail — fail fast before any DB write -------------------
        pii_label = detect_pii(body.input_text)
        if pii_label:
            logger.warning(
                "pii_rejected",
                extra={"request_id": request_id, "pii_type": pii_label},
            )
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Potentially identifying patient data detected ({pii_label}). "
                "Please remove personal identifiers before submitting.",
            )

        # 0.5 Feature flag — fail fast if LLM integration is not enabled ----
        if not settings.chat_mock_mode:
            raise HTTPException(
                status.HTTP_501_NOT_IMPLEMENTED,
                "LLM integration not yet enabled. Set CHAT_MOCK_MODE=true to use mock responses.",
            )

        # 1. Verify the conversation exists and belongs to this user ---------
        # RLS enforces ownership; the explicit user_id filter is defence-in-depth.
        conv_rows = await _db_select(
            "/conversations",
            {"id": f"eq.{body.conversation_id}", "user_id": f"eq.{user_id}", "select": "id"},
            auth.jwt,
        )
        if not conv_rows:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Conversation not found or access denied"
            )

        # 2. Insert user message ---------------------------------------------
        await _db_insert(
            "messages",
            {
                "conversation_id": body.conversation_id,
                "role": "user",
                "content": {"text": body.input_text, "mode": body.mode},
                "user_id": user_id,
            },
            auth.jwt,
        )

        # 3. Build structured mock reply -------------------------------------
        # TODO: replace with real Claude API call using SYSTEM_PROMPT once the
        #       Anthropic SDK is wired in. The schema and mock shapes are final.
        payload = _build_mock_payload(body.mode)

        # 4. Insert assistant message ----------------------------------------
        await _db_insert(
            "messages",
            {
                "conversation_id": body.conversation_id,
                "role": "assistant",
                "content": payload.model_dump(),
                "user_id": user_id,
            },
            auth.jwt,
        )

        return ChatResponse(
            request_id=request_id,
            assistant_payload=payload,
            response_metadata=ResponseMetadata(
                is_mock=True,
                model="mock-v1",
                prompt_version=PROMPT_VERSION,
            ),
        )

    except HTTPException as exc:
        status_code = exc.status_code
        raise  # re-raise so FastAPI still returns the correct HTTP response

    except Exception:
        status_code = 500
        raise

    finally:
        # Fires on every exit path: success, HTTPException, or unhandled error.
        # Raw input and output content are never included.
        logger.info(
            "chat_complete",
            extra={
                "request_id": request_id,
                "user_id": user_id,
                "conversation_id": body.conversation_id,
                "mode": body.mode,
                "input_length": input_length,
                "status_code": status_code,
                "latency_ms": round((time.perf_counter() - start) * 1000),
                "is_mock": settings.chat_mock_mode,
                "prompt_version": PROMPT_VERSION,
            },
        )
