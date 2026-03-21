import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from guardrails import detect_pii
from policy import SYSTEM_PROMPT  # noqa: F401  (used in future Claude integration)
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
    _auth_client = httpx.AsyncClient(base_url=settings.supabase_url)
    _db_client = httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
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
# Auth dependency
# ---------------------------------------------------------------------------


async def get_supabase_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),  # noqa: B008
) -> dict:
    """Validate the Bearer token against Supabase and return the user object.

    user["id"] from this response is the only safe source of user_id.
    Never read user_id from the request body.
    """
    response = await _auth_client.get(
        "/auth/v1/user",
        headers={
            "Authorization": f"Bearer {credentials.credentials}",
            "apikey": settings.supabase_service_role_key,
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
    mode: Literal["triage", "summary", "patient_message"]
    input_text: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str | None = None  # omit to start a new conversation


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
    content: str  # JSON-encoded AssistantPayload for assistant messages


class ChatResponse(BaseModel):
    request_id: str  # UUID v4 — use for logging and client-side deduplication
    conversation_id: str
    user_message: MessageOut
    assistant_message: MessageOut
    assistant_payload: AssistantPayload  # parsed payload — avoids client-side JSON.parse


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


def _build_mock_payload(mode: str, input_text: str) -> AssistantPayload:
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


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, user: dict = Depends(get_supabase_user)):  # noqa: B008
    request_id = str(uuid.uuid4())
    start = time.perf_counter()
    user_id: str = user["id"]  # authoritative — comes from Supabase, not the client
    input_length = len(body.input_text)
    status_code = 200

    # Logged on arrival — never includes raw content.
    logger.info(
        "chat_request",
        extra={
            "request_id": request_id,
            "user_id": user_id,
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

        # 1. Resolve conversation --------------------------------------------
        if body.conversation_id is None:
            conversation = await _db_insert("conversations", {"user_id": user_id})
            conversation_id: str = conversation["id"]
        else:
            response = await _db_client.get(
                "/conversations",
                params={
                    "id": f"eq.{body.conversation_id}",
                    "user_id": f"eq.{user_id}",
                    "select": "id",
                },
            )
            rows = response.json()
            if not rows:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "Conversation not found or access denied"
                )
            conversation_id = rows[0]["id"]

        # 2. Insert user message ---------------------------------------------
        user_message = await _db_insert(
            "messages",
            {
                "conversation_id": conversation_id,
                "role": "user",
                "content": body.input_text,
                "user_id": user_id,
            },
        )

        # 3. Build structured mock reply -------------------------------------
        # TODO: replace with real Claude API call using SYSTEM_PROMPT once the
        #       Anthropic SDK is wired in. The schema and mock shapes are final.
        payload = _build_mock_payload(body.mode, body.input_text)

        # 4. Insert assistant message ----------------------------------------
        assistant_message = await _db_insert(
            "messages",
            {
                "conversation_id": conversation_id,
                "role": "assistant",
                "content": json.dumps(payload.model_dump(), ensure_ascii=False),
                "user_id": user_id,
            },
        )

        return ChatResponse(
            request_id=request_id,
            conversation_id=conversation_id,
            user_message=MessageOut(**user_message),
            assistant_message=MessageOut(**assistant_message),
            assistant_payload=payload,
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
                "mode": body.mode,
                "input_length": input_length,
                "status_code": status_code,
                "latency_ms": round((time.perf_counter() - start) * 1000),
            },
        )
