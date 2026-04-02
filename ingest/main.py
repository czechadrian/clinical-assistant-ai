import asyncio
import json as _json
import logging
import random
import re
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError, field_validator

from classifier import ClassifiedInput, classify_input  # noqa: F401
from embedder import OpenAIEmbedder
from policy import PROMPT_VERSION, SYSTEM_PROMPT  # noqa: F401  (used in future Claude integration)
from settings import Settings, configure_logging
from validator import validate_and_repair

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
_embedder: OpenAIEmbedder | None = None  # None when OPENAI_API_KEY is not set

# Conservative timeouts: connect fast, allow up to 10 s for DB reads.
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _auth_client, _db_client, _embedder
    _auth_client = httpx.AsyncClient(base_url=settings.supabase_url, timeout=_TIMEOUT)
    _db_client = httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        headers={
            # apikey identifies the project at the API-gateway level.
            # Authorization is injected per-request with the user's JWT
            # so PostgREST evaluates RLS as that specific user.
            "apikey": settings.supabase_service_role_key,
            "Content-Type": "application/json",
        },
        timeout=_TIMEOUT,
    )
    # Embedder is optional: only initialised when OPENAI_API_KEY is configured.
    # /retrieve returns 503 and /chat skips context if _embedder is None.
    _embedder = (
        OpenAIEmbedder(settings.openai_api_key, settings.embed_model)
        if settings.openai_api_key
        else None
    )
    logger.info(
        "startup",
        extra={
            "app_env": settings.app_env,
            "git_commit": settings.git_commit,
            "embedder_ready": _embedder is not None,
        },
    )
    yield
    await _auth_client.aclose()
    await _db_client.aclose()
    if _embedder is not None:
        await _embedder.aclose()
    logger.info("shutdown")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=settings.cors_origin_regex,
    allow_methods=["GET", "POST"],  # only what this API actually exposes
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    allow_credentials=False,  # Bearer tokens, not cookies
)

bearer_scheme = HTTPBearer()


# ---------------------------------------------------------------------------
# Error taxonomy
#
# AppError provides stable, machine-readable error codes so the frontend can
# react specifically (e.g. show a PII-specific warning vs. a generic retry).
#
# Response shape:
#   {"error": {"code": "PII_DETECTED", "message": "...", "request_id": "..."}}
#
# Stable codes (never rename these — clients key off them):
#   PII_DETECTED          400  — personal identifiers in input
#   MOCK_DISABLED         501  — LLM integration not enabled
#   CONVERSATION_NOT_FOUND 403 — conv missing or belongs to another user
#   VALIDATION_FAILED     500  — assistant payload failed schema validation
#   TIMEOUT               504  — upstream request exceeded timeout
#   TRANSIENT_UPSTREAM    502  — upstream unavailable after retries
#   DUPLICATE_REQUEST     200  — idempotent replay (not an error; see /chat)
# ---------------------------------------------------------------------------


@dataclass
class AppError(Exception):
    """Typed application error with a stable machine-readable code.

    Raised instead of HTTPException for domain errors in /chat.
    _app_error_handler converts it to a JSON response.
    """

    code: str  # stable identifier e.g. "PII_DETECTED"
    message: str  # human-readable, safe to expose to the client
    http_status: int  # HTTP status code
    request_id: str | None = None  # set by the /chat handler

    def __post_init__(self) -> None:
        super().__init__(self.message)


@app.exception_handler(AppError)
async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": exc.request_id,
            }
        },
    )


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
        raise AppError("UNAUTHORIZED", "Invalid or expired token.", status.HTTP_401_UNAUTHORIZED)
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
# Retry
#
# Wraps a single httpx request with up to _MAX_RETRIES retries.
# Only transient failures are retried: 429/502/503/504 and network errors.
# Client errors (4xx) and server errors with a stable cause (e.g. 403/422)
# are returned immediately so the caller can surface the real error.
#
# Backoff: full jitter — sleep(random(0, min(cap, base * 2^attempt))).
# This avoids thundering-herd while keeping retries fast on average.
# ---------------------------------------------------------------------------

_TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})
_MAX_RETRIES = 2


def _backoff(attempt: int) -> float:
    """Full-jitter exponential backoff in seconds."""
    base, cap = 0.5, 4.0
    return random.uniform(0, min(cap, base * (2**attempt)))


async def _retrying_request(factory: Callable[[], Any]) -> httpx.Response:
    """
    Call ``factory()`` (which must return an awaitable httpx.Response) and retry
    on transient failures up to ``_MAX_RETRIES`` times.

    Raises AppError on timeout or network exhaustion.
    Non-transient responses (including 4xx) are returned as-is.
    """
    last_exc: BaseException | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp: httpx.Response = await factory()
            if resp.status_code in _TRANSIENT_STATUSES and attempt < _MAX_RETRIES:
                await asyncio.sleep(_backoff(attempt))
                continue
            return resp
        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt >= _MAX_RETRIES:
                raise AppError(
                    "TIMEOUT",
                    "The request timed out. Please retry.",
                    status.HTTP_504_GATEWAY_TIMEOUT,
                ) from exc
            await asyncio.sleep(_backoff(attempt))
        except httpx.NetworkError as exc:
            last_exc = exc
            if attempt >= _MAX_RETRIES:
                raise AppError(
                    "TRANSIENT_UPSTREAM",
                    "Upstream service unavailable. Please retry.",
                    status.HTTP_502_BAD_GATEWAY,
                ) from exc
            await asyncio.sleep(_backoff(attempt))
    # Reached only if all retries ended with a transient status (not exception).
    raise AppError(
        "TRANSIENT_UPSTREAM",
        "Upstream service unavailable after retries. Please retry.",
        status.HTTP_502_BAD_GATEWAY,
    ) from last_exc


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
    response = await _retrying_request(
        lambda: _db_client.post(
            f"/{table}",
            json=row,
            headers={**_rls_headers(jwt), "Prefer": "return=representation"},
        )
    )
    if response.status_code in (401, 403):
        # RLS violation or permission denied — surface as 403 so callers get a usable error.
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Permission denied: {table}")
    if response.status_code not in (200, 201):
        # Log the raw PostgREST body so the real cause is visible in server logs.
        # Common codes: 42501=RLS violation, 42P01=table not found, 23505=unique constraint.
        logger.error(
            "db_insert_failed",
            extra={
                "table": table,
                "http_status": response.status_code,
                "postgrest_error": response.text,
            },
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"DB insert failed: {table} (HTTP {response.status_code}) — check server logs",
        )
    return response.json()[0]


async def _db_select(path: str, params: dict, jwt: str) -> list[dict]:
    """Run a GET query against PostgREST. RLS applies via the caller's JWT."""
    response = await _retrying_request(
        lambda: _db_client.get(path, params=params, headers=_rls_headers(jwt))
    )
    if not response.is_success:
        logger.error(
            "db_select_failed",
            extra={
                "path": path,
                "http_status": response.status_code,
                "postgrest_error": response.text,
            },
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"DB query failed: {path} (HTTP {response.status_code}) — check server logs",
        )
    return response.json()


async def _db_rpc(function_name: str, params: dict, jwt: str) -> list[dict]:
    """Call a PostgREST RPC function.  RLS applies via the caller's JWT.

    The function is called at: POST /rest/v1/rpc/{function_name}
    params is serialised as the JSON request body.
    """
    response = await _retrying_request(
        lambda: _db_client.post(
            f"/rpc/{function_name}",
            json=params,
            headers=_rls_headers(jwt),
        )
    )
    if not response.is_success:
        logger.error(
            "db_rpc_failed",
            extra={
                "function": function_name,
                "http_status": response.status_code,
                "postgrest_error": response.text,
            },
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"DB RPC failed: {function_name} (HTTP {response.status_code}) — check server logs",
        )
    return response.json()


async def _retrieve_context(query: str, top_k: int, jwt: str) -> list[dict]:
    """Return the top-k closest doc_chunks for *query*.

    Returns [] silently on any failure (embedder not configured, API error,
    DB error) so callers can degrade gracefully.  This is intentional for
    /chat: retrieval failure must never block the assistant response.

    Each returned dict contains: chunk_id, doc_id, title, section, content, score.
    The 'content' field (full chunk text) must never be logged.
    """
    if _embedder is None:
        return []
    try:
        embeddings = await _embedder.embed([query])
        if not embeddings:
            return []
        rows = await _db_rpc(
            "match_chunks",
            {"query_embedding": embeddings[0], "match_count": top_k},
            jwt,
        )
        return rows
    except Exception as exc:
        # Log type only — the query text is patient input and must not be logged.
        logger.warning(
            "retrieval_skipped",
            extra={"reason": type(exc).__name__},
        )
        return []


def _format_rag_context(rows: list[dict]) -> str:
    """Wrap retrieved chunks in structured XML-like tags for LLM injection.

    Injection resistance: retrieved text is isolated inside <retrieved_context>
    so the model treats it as source material only, not executable instructions.
    SYSTEM_PROMPT (policy.py) must instruct the model to honour this boundary.

    Week 3 integration point: pass the return value of this function as
    additional context inside the anthropic.messages.create() call, e.g.:
        messages=[{"role": "user", "content": classified.for_llm() + context_block}]

    Privacy: the return value contains full chunk text — never log it.
    """
    if not rows:
        return ""
    parts: list[str] = []
    for r in rows:
        label = f"doc_id='{r['doc_id']}' title='{r['title']}'"
        if r.get("section"):
            label += f" section='{r['section']}'"
        parts.append(f"<source {label}>\n{r['content']}\n</source>")
    return "<retrieved_context>\n" + "\n\n".join(parts) + "\n</retrieved_context>"


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
    """A guideline or reference cited in the response.

    id          — chunk_id, traces to the exact indexed chunk in doc_chunks.
    text_snippet— first _SNIPPET_LEN chars of chunk content; populated server-side
                  for the debug toggle in the UI.  Never logged.  Optional so
                  stored messages (which predate this field) still deserialise.
    """

    id: str
    title: str
    section: str
    text_snippet: str | None = None


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

    @field_validator("content", mode="before")
    @classmethod
    def _coerce_content(cls, v: Any) -> dict:
        """PostgREST returns TEXT columns as JSON strings. Parse them on the way in."""
        if isinstance(v, str):
            import json as _json

            return _json.loads(v)
        return v


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


class DocOut(BaseModel):
    id: str
    title: str
    filename: str
    storage_path: str
    file_hash: str
    version: str
    status: str
    last_ingested_at: str | None = None
    last_error_code: str | None = None
    created_at: str
    updated_at: str


class CreateDocRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    filename: str = Field(..., min_length=1, max_length=200)
    storage_path: str = Field(..., min_length=1, max_length=500)
    file_hash: str = Field(..., min_length=64, max_length=64)  # SHA-256 hex
    version: str = Field(default="1", max_length=20)


class RetrievedChunk(BaseModel):
    """One item in a /retrieve response.

    text_snippet is the first 300 characters of the chunk content — enough
    for display purposes.  The /chat endpoint uses full content internally
    via _retrieve_context(), never via this model.
    """

    chunk_id: str
    doc_id: str
    title: str  # from docs.title
    section: str | None
    score: float  # cosine similarity in [0, 1]; higher = more relevant
    text_snippet: str  # first 300 chars of doc_chunks.content


class RetrieveResponse(BaseModel):
    """Contract for GET /retrieve.

    top_k echoes the requested limit so callers can tell whether fewer
    results were returned because the index is sparse (items < top_k).
    """

    items: list[RetrievedChunk]
    top_k: int


# ---------------------------------------------------------------------------
# Grounding helpers
#
# _MIN_RETRIEVAL_SCORE — cosine similarity threshold; chunks below this are too
#   distant to be useful and are excluded from the context window.
#
# _GUIDELINE_LANGUAGE_RE — matches confident guideline-style claims in assistant
#   output (specific orgs, dosages, standard-of-care phrases).  Used by
#   _check_groundedness to downgrade unsupported responses.
#
# _build_no_source_payload — returned when retrieval finds nothing above the
#   threshold for a non-vague, non-unsafe query.  Honest: never fabricates.
#
# _check_groundedness — post-validation safety net.  If sources[] is empty
#   but the response text contains specific guideline language, downgrade to
#   uncertain.  One deterministic pass; no I/O; no LLM call.
# ---------------------------------------------------------------------------

_MIN_RETRIEVAL_SCORE = 0.5

_GUIDELINE_LANGUAGE_RE = re.compile(
    r"\b("
    r"wytyczn\w+\s+(?:PTK|ESC|WHO|PTD|AHA|ACC|ERS|EAN)\b"
    r"|zalecen\w+\s+(?:PTK|ESC|WHO|PTD|AHA|ACC)\b"
    r"|zgodnie\s+z\s+\w+\s+\d{4}\b"
    r"|\d+\s*mg\b|\d+\s*µg\b|\d+\s*IU\b"
    r"|dawka\s+\w+\s+wynosi\b"
    r"|standard(?:y|owe|ów)?\s+(?:leczenia|postępowania)\b"
    r")",
    re.IGNORECASE,
)


def _has_guideline_language(text: str) -> bool:
    """True when text contains specific guideline references or dosages.

    Used only to check assistant-generated output, never raw user input.
    """
    return bool(_GUIDELINE_LANGUAGE_RE.search(text))


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


def _build_raw_payload(
    mode: str, classified: ClassifiedInput, rag_context: str = ""
) -> dict[str, Any]:
    """
    Return the assistant payload as a raw dict for validation.

    This is the single seam for Week 2: replace the function body with:
        system = SYSTEM_PROMPT + ("\n\n" + rag_context if rag_context else "")
        raw_json = await anthropic_client.messages.create(
            model="claude-opus-4-6",
            system=system,
            messages=[{"role": "user", "content": classified.for_llm()}],
        )
        return json.loads(raw_json.content[0].text)

    rag_context is pre-formatted by _format_rag_context() and injected as an
    additional system instruction so the model treats it as source material only.

    Patchable in tests — patch "main._build_raw_payload" to inject invalid dicts.
    """
    if classified.is_unsafe_request:
        return _build_refuse_payload().model_dump()
    if classified.is_vague:
        return _build_uncertain_payload().model_dump()
    return _build_mock_payload(mode).model_dump()


def _build_refuse_payload() -> AssistantPayload:
    """Returned when the request is unsafe or out-of-scope (flag='refuse')."""
    return AssistantPayload(
        questions_to_ask=[],
        red_flags=[],
        possible_next_steps=[],
        patient_facing_summary="",
        sources=[],
        flag="refuse",
        disclaimer=_DISCLAIMER,
    )


def _build_uncertain_payload() -> AssistantPayload:
    """Returned when the input is too vague to act on (flag='uncertain')."""
    return AssistantPayload(
        questions_to_ask=[
            "Proszę podać główny objaw lub dolegliwość pacjenta.",
            "Od jak dawna pacjent odczuwa dolegliwości?",
            "Czy występują objawy towarzyszące (np. ból, gorączka, duszność)?",
            "Jakie leki przyjmuje pacjent na stałe?",
            "Czy pacjent był hospitalizowany lub operowany w przeszłości?",
        ],
        red_flags=[],
        possible_next_steps=["Proszę o podanie bardziej szczegółowych informacji klinicznych."],
        patient_facing_summary="",
        sources=[],
        flag="uncertain",
        disclaimer=_DISCLAIMER,
    )


def _build_no_source_payload() -> AssistantPayload:
    """Returned when retrieval finds no chunks above _MIN_RETRIEVAL_SCORE.

    Distinct from _build_uncertain_payload: this communicates a knowledge-base
    gap, not a vague query.  Prompts the user to provide more detail AND
    signals that relevant guideline documents may need to be indexed.
    """
    return AssistantPayload(
        questions_to_ask=[
            "Proszę opisać objawy bardziej szczegółowo.",
            "Od jak dawna pacjent odczuwa te dolegliwości?",
            "Czy w bazie wiedzy zaindeksowano odpowiednie dokumenty medyczne?",
        ],
        red_flags=[],
        possible_next_steps=[
            "Upewnij się, że odpowiednie wytyczne kliniczne zostały zaindeksowane w systemie.",
        ],
        patient_facing_summary=(
            "Brak wystarczających danych źródłowych. "
            "Asystent nie może udzielić odpowiedzi bez dostępu do odpowiednich wytycznych."
        ),
        sources=[],
        flag="uncertain",
        disclaimer=_DISCLAIMER,
    )


def _check_groundedness(payload: AssistantPayload, high_confidence: list[dict]) -> AssistantPayload:
    """Downgrade to uncertain if sources[] is empty but the response contains
    specific guideline language (dosages, org-named guidelines, standard-of-care phrases).

    Deterministic — no I/O, no LLM call.  One pass, never loops.
    Only fires when sources[] is empty; payloads with citations pass through unchanged.
    Refuse payloads are never touched (flag='refuse' is intentional).
    """
    if payload.sources or payload.flag == "refuse":
        return payload

    combined = " ".join([payload.patient_facing_summary, *payload.possible_next_steps])
    if not _has_guideline_language(combined):
        return payload

    return AssistantPayload(
        questions_to_ask=[
            "Brak wystarczających danych źródłowych do udzielenia tej odpowiedzi.",
            *payload.questions_to_ask[:3],
        ],
        red_flags=payload.red_flags,
        possible_next_steps=[],
        patient_facing_summary=(
            "Brak wystarczających danych źródłowych. "
            "Asystent nie może udzielić odpowiedzi bez dostępu do odpowiednich wytycznych."
        ),
        sources=[],
        flag="uncertain",
        disclaimer=payload.disclaimer,
    )


# ---------------------------------------------------------------------------
# SSE stream helper
# ---------------------------------------------------------------------------


async def _mock_sse_stream(payload: AssistantPayload, request_id: str):
    """Yield SSE chunks: delta tokens from patient_facing_summary, then a done event.

    In production (Week 2), replace with real LLM streaming tokens.
    Each chunk is self-contained JSON so the client can parse incrementally.
    """
    summary = payload.patient_facing_summary or "Informacje przetworzone."
    for word in summary.split():
        yield f"data: {_json.dumps({'type': 'delta', 'token': word + ' '})}\n\n"
        await asyncio.sleep(0.02)
    yield (
        f"data: {_json.dumps({'type': 'done', 'request_id': request_id, 'payload': payload.model_dump()})}\n\n"
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
async def chat(
    body: ChatRequest,
    auth: Auth = Depends(get_auth),  # noqa: B008
    idempotency_key: str | None = Header(None, alias="idempotency-key"),  # noqa: B008
):
    request_id = str(uuid.uuid4())
    start = time.perf_counter()
    user_id = auth.user_id
    input_length = len(body.input_text)
    status_code = 200
    classified: ClassifiedInput | None = None  # set in try block; read safely in finally
    retrieved: list[dict] = []  # set in try block; count logged in finally
    high_confidence: list[dict] = []  # filtered subset of retrieved above threshold

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
        # 0. Classify input — single pass over all checks -------------------
        # Returns a typed object; raw body.input_text is never read again.
        classified = classify_input(body.input_text)

        # 0a. PII — abort before any DB write --------------------------------
        if classified.pii_flags:
            logger.warning(
                "pii_rejected",
                extra={"request_id": request_id, "pii_type": classified.pii_flags[0]},
            )
            raise AppError(
                "PII_DETECTED",
                f"Potentially identifying patient data detected ({classified.pii_flags[0]}). "
                "Please remove personal identifiers before submitting.",
                status.HTTP_400_BAD_REQUEST,
                request_id,
            )

        # 0b. Injection detected — log and continue (non-blocking) ----------
        if classified.has_injection:
            logger.warning(
                "injection_detected",
                extra={
                    "request_id": request_id,
                    "injection_flags": classified.injection_flags,
                },
            )

        # 0.5 Feature flag ---------------------------------------------------
        if not settings.chat_mock_mode:
            raise AppError(
                "MOCK_DISABLED",
                "LLM integration not yet enabled. Set CHAT_MOCK_MODE=true to use mock responses.",
                status.HTTP_501_NOT_IMPLEMENTED,
                request_id,
            )

        # 1. Verify conversation ownership -----------------------------------
        conv_rows = await _db_select(
            "/conversations",
            {"id": f"eq.{body.conversation_id}", "user_id": f"eq.{user_id}", "select": "id"},
            auth.jwt,
        )
        if not conv_rows:
            raise AppError(
                "CONVERSATION_NOT_FOUND",
                "Conversation not found or access denied.",
                status.HTTP_403_FORBIDDEN,
                request_id,
            )

        # 1a. Idempotency check — before any writes --------------------------
        # If the client sent a key we've already processed, return the cached
        # assistant response without inserting any new rows.
        if idempotency_key:
            cached_rows = await _db_select(
                "/messages",
                {
                    "conversation_id": f"eq.{body.conversation_id}",
                    "role": "eq.assistant",
                    "content->_meta->>idempotency_key": f"eq.{idempotency_key}",
                    "select": "id,content",
                },
                auth.jwt,
            )
            if cached_rows:
                content = cached_rows[0]["content"]
                meta = content.get("_meta", {})
                cached_payload = AssistantPayload.model_validate(
                    {k: v for k, v in content.items() if k != "_meta"}
                )
                logger.info(
                    "idempotent_replay",
                    extra={"request_id": request_id, "idempotency_key": idempotency_key},
                )
                return ChatResponse(
                    request_id=request_id,
                    assistant_payload=cached_payload,
                    response_metadata=ResponseMetadata(
                        is_mock=meta.get("is_mock", True),
                        model=meta.get("model", "mock-v1"),
                        prompt_version=meta.get("prompt_version", PROMPT_VERSION),
                    ),
                )

        # 2. Insert user message — include classification metadata -----------
        # _meta stores flag labels and a content-hash for audit.
        # It never stores raw flagged substrings or patient text.
        user_meta: dict[str, Any] = {
            "input_hash": classified.input_hash,
            "injection_flags": classified.injection_flags,
            "is_vague": classified.is_vague,
            "is_unsafe_request": classified.is_unsafe_request,
            "prompt_version": PROMPT_VERSION,
        }
        if idempotency_key:
            user_meta["idempotency_key"] = idempotency_key

        await _db_insert(
            "messages",
            {
                "conversation_id": body.conversation_id,
                "role": "user",
                "content": {
                    "text": body.input_text,
                    "mode": body.mode,
                    "_meta": user_meta,
                },
                "user_id": user_id,
            },
            auth.jwt,
        )

        # 2a. Retrieve context — non-blocking, degrades to [] on any failure ---
        # Privacy: chunk content and query text are never logged (only counts).
        retrieved = await _retrieve_context(body.input_text, top_k=3, jwt=auth.jwt)
        # Only chunks above the confidence threshold are used for grounding.
        high_confidence = [r for r in retrieved if r.get("score", 0) >= _MIN_RETRIEVAL_SCORE]
        rag_context = _format_rag_context(high_confidence)

        # 3. Build raw payload dict -------------------------------------------
        if classified.is_unsafe_request:
            logger.warning("unsafe_request_refused", extra={"request_id": request_id})
        elif classified.is_vague:
            logger.info("vague_input_uncertain", extra={"request_id": request_id})

        raw_dict = _build_raw_payload(body.mode, classified, rag_context)

        # 3a. Inject sources — chunk_id + snippet, only from actual retrieval.
        # Never invented.  text_snippet is truncated to _SNIPPET_LEN for the
        # UI debug toggle; it is not logged.
        if high_confidence:
            raw_dict["sources"] = [
                {
                    "id": str(r["chunk_id"]),
                    "title": r["title"],
                    "section": r.get("section") or "",
                    "text_snippet": r["content"][:_SNIPPET_LEN],
                }
                for r in high_confidence
            ]

        # 4. Validate — repair once if needed — validate again ----------------
        # On success: payload is a valid AssistantPayload; repair_applied records whether
        # coercions were needed.  On failure: 500 with a safe message (no raw content).
        try:
            payload, repair_applied = validate_and_repair(raw_dict, AssistantPayload)
        except ValidationError as exc:
            logger.error(
                "payload_validation_failed",
                extra={
                    "request_id": request_id,
                    "validation_error_code": "schema_invalid_after_repair",
                    # error_count is a safe integer; field paths omitted (could hint at content)
                    "error_count": exc.error_count(),
                },
            )
            safe_detail = "Assistant response did not meet the required schema. Please retry."
            if settings.validation_debug:
                # Field paths + error types only — never the raw invalid values.
                error_locs = [
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()
                ]
                safe_detail = f"{safe_detail} [debug] Invalid fields: {error_locs}"
            raise AppError(
                "VALIDATION_FAILED",
                safe_detail,
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                request_id,
            ) from None

        if repair_applied:
            logger.warning("payload_repaired", extra={"request_id": request_id})

        # 4a. Groundedness enforcement ----------------------------------------
        # No-source fallback: non-vague, non-unsafe query with no high-confidence
        # chunks → override with an explicit "no supporting documents" uncertain payload.
        # Keeps the response honest: we never answer with guideline-level confidence
        # when the knowledge base has no relevant material.
        if not high_confidence and not classified.is_unsafe_request and not classified.is_vague:
            logger.info("no_source_fallback", extra={"request_id": request_id})
            payload = _build_no_source_payload()
        else:
            # Post-check: if sources[] empty but response contains specific guideline
            # language (dosages, org refs), downgrade to uncertain.  Safety net for
            # cases where the LLM generates confident claims without retrieved backing.
            payload = _check_groundedness(payload, high_confidence)

        # 5. Insert assistant message — validation must pass before storage ---
        # Raw invalid output is never stored; only the repaired-and-validated payload.
        assistant_meta: dict[str, Any] = {
            "prompt_version": PROMPT_VERSION,
            "model": "mock-v1",
            "is_mock": True,
            "validation_status": "valid",
            "repair_applied": repair_applied,
        }
        if idempotency_key:
            assistant_meta["idempotency_key"] = idempotency_key

        await _db_insert(
            "messages",
            {
                "conversation_id": body.conversation_id,
                "role": "assistant",
                "content": {
                    **payload.model_dump(),
                    "_meta": assistant_meta,
                },
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

    except AppError as exc:
        status_code = exc.http_status
        raise  # _app_error_handler converts to JSONResponse

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
                # counts only — never raw content
                "injection_flag_count": len(classified.injection_flags) if classified else 0,
                "retrieved_count": len(high_confidence),
            },
        )


@app.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    auth: Auth = Depends(get_auth),  # noqa: B008
    idempotency_key: str | None = Header(None, alias="idempotency-key"),  # noqa: B008
) -> StreamingResponse:
    """SSE variant of /chat.  Streams delta tokens then a 'done' event.

    Persists ONE complete assistant message after payload is determined.
    In Week 2: replace _build_raw_payload with a real LLM streaming call and
    yield tokens as they arrive; store the message in a background task after
    the stream closes.
    """
    request_id = str(uuid.uuid4())
    user_id = auth.user_id

    classified = classify_input(body.input_text)

    if classified.pii_flags:
        raise AppError(
            "PII_DETECTED",
            f"Potentially identifying patient data detected ({classified.pii_flags[0]}). "
            "Please remove personal identifiers before submitting.",
            status.HTTP_400_BAD_REQUEST,
            request_id,
        )
    if classified.has_injection:
        logger.warning(
            "injection_detected",
            extra={"request_id": request_id, "injection_flags": classified.injection_flags},
        )
    if not settings.chat_mock_mode:
        raise AppError(
            "MOCK_DISABLED",
            "LLM integration not yet enabled. Set CHAT_MOCK_MODE=true to use mock responses.",
            status.HTTP_501_NOT_IMPLEMENTED,
            request_id,
        )

    conv_rows = await _db_select(
        "/conversations",
        {"id": f"eq.{body.conversation_id}", "user_id": f"eq.{user_id}", "select": "id"},
        auth.jwt,
    )
    if not conv_rows:
        raise AppError(
            "CONVERSATION_NOT_FOUND",
            "Conversation not found or access denied.",
            status.HTTP_403_FORBIDDEN,
            request_id,
        )

    # Idempotency: return cached stream if this key was already processed.
    if idempotency_key:
        cached_rows = await _db_select(
            "/messages",
            {
                "conversation_id": f"eq.{body.conversation_id}",
                "role": "eq.assistant",
                "content->_meta->>idempotency_key": f"eq.{idempotency_key}",
                "select": "id,content",
            },
            auth.jwt,
        )
        if cached_rows:
            content = cached_rows[0]["content"]
            cached_payload = AssistantPayload.model_validate(
                {k: v for k, v in content.items() if k != "_meta"}
            )
            logger.info(
                "idempotent_stream_replay",
                extra={"request_id": request_id, "idempotency_key": idempotency_key},
            )
            return StreamingResponse(
                _mock_sse_stream(cached_payload, request_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Request-Id": request_id},
            )

    # Retrieve context and build payload (same logic as /chat).
    retrieved = await _retrieve_context(body.input_text, top_k=3, jwt=auth.jwt)
    high_confidence = [r for r in retrieved if r.get("score", 0) >= _MIN_RETRIEVAL_SCORE]
    rag_context = _format_rag_context(high_confidence)

    raw_dict = _build_raw_payload(body.mode, classified, rag_context)
    if high_confidence:
        raw_dict["sources"] = [
            {
                "id": str(r["chunk_id"]),
                "title": r["title"],
                "section": r.get("section") or "",
                "text_snippet": r["content"][:_SNIPPET_LEN],
            }
            for r in high_confidence
        ]

    try:
        payload, repair_applied = validate_and_repair(raw_dict, AssistantPayload)
    except ValidationError as exc:
        logger.error(
            "payload_validation_failed",
            extra={"request_id": request_id, "error_count": exc.error_count()},
        )
        raise AppError(
            "VALIDATION_FAILED",
            "Assistant response did not meet the required schema. Please retry.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            request_id,
        ) from None

    if not high_confidence and not classified.is_unsafe_request and not classified.is_vague:
        payload = _build_no_source_payload()
    else:
        payload = _check_groundedness(payload, high_confidence)

    # Persist user message.
    user_meta: dict[str, Any] = {
        "input_hash": classified.input_hash,
        "injection_flags": classified.injection_flags,
        "is_vague": classified.is_vague,
        "is_unsafe_request": classified.is_unsafe_request,
        "prompt_version": PROMPT_VERSION,
    }
    if idempotency_key:
        user_meta["idempotency_key"] = idempotency_key
    await _db_insert(
        "messages",
        {
            "conversation_id": body.conversation_id,
            "role": "user",
            "content": {"text": body.input_text, "mode": body.mode, "_meta": user_meta},
            "user_id": user_id,
        },
        auth.jwt,
    )

    # Persist the complete assistant message before streaming begins.
    # (In mock mode the payload is fully determined; Week 2 uses a background task.)
    assistant_meta: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "model": "mock-v1",
        "is_mock": True,
        "validation_status": "valid",
        "repair_applied": repair_applied,
    }
    if idempotency_key:
        assistant_meta["idempotency_key"] = idempotency_key
    await _db_insert(
        "messages",
        {
            "conversation_id": body.conversation_id,
            "role": "assistant",
            "content": {**payload.model_dump(), "_meta": assistant_meta},
            "user_id": user_id,
        },
        auth.jwt,
    )

    return StreamingResponse(
        _mock_sse_stream(payload, request_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Request-Id": request_id},
    )


# ---------------------------------------------------------------------------
# Docs endpoints (Day 15 — document metadata, no embeddings yet)
# ---------------------------------------------------------------------------


@app.get("/docs", response_model=list[DocOut])
async def list_docs(auth: Auth = Depends(get_auth)):  # noqa: B008
    """List all shared medical document metadata.  Any authenticated user can read."""
    rows = await _db_select(
        "/docs",
        {
            "order": "created_at.desc",
            "select": "id,title,filename,storage_path,file_hash,version,status,last_ingested_at,last_error_code,created_at,updated_at",
        },
        auth.jwt,
    )
    return [DocOut(**r) for r in rows]


@app.post("/docs", response_model=DocOut, status_code=status.HTTP_201_CREATED)
async def create_doc(
    body: CreateDocRequest,
    auth: Auth = Depends(get_auth),  # noqa: B008
):
    """Create a document metadata record.  Requires admin role (enforced by DB RLS).

    The file must already be uploaded to Supabase Storage before calling this endpoint.
    PostgREST returns 403 if the caller is not an admin — _db_insert surfaces this as HTTP 403.
    """
    row = await _db_insert(
        "docs",
        {
            "title": body.title,
            "filename": body.filename,
            "storage_path": body.storage_path,
            "file_hash": body.file_hash,
            "version": body.version,
        },
        auth.jwt,
    )
    return DocOut(**row)


# ---------------------------------------------------------------------------
# Admin ingest endpoints (Day 20 — automation)
# ---------------------------------------------------------------------------


class IngestTriggerResponse(BaseModel):
    queued: int
    message: str


class IngestStatusResponse(BaseModel):
    counts: dict[str, int]
    total: int


async def _is_admin(user_id: str, jwt: str) -> bool:
    """Return True if the authenticated user has role='admin' in profiles."""
    rows = await _db_select(
        "/profiles",
        {"id": f"eq.{user_id}", "select": "role"},
        jwt,
    )
    return bool(rows and rows[0].get("role") == "admin")


async def _run_ingest_background() -> None:
    """Thin wrapper so tests can patch this instead of importing worker."""
    from worker import run_all as _worker_run_all

    await _worker_run_all(settings)


@app.post("/admin/ingest", response_model=IngestTriggerResponse, status_code=202)
async def trigger_ingest(
    background_tasks: BackgroundTasks,
    auth: Auth = Depends(get_auth),  # noqa: B008
):
    """Trigger a background ingest run for all pending documents.

    Admin role required.  Returns 409 if an ingest run is already in progress.
    The worker executes asynchronously; poll GET /admin/ingest/status or
    GET /docs to track progress.
    """
    if not await _is_admin(auth.user_id, auth.jwt):
        raise AppError(
            "FORBIDDEN",
            "Admin role required to trigger ingest.",
            status.HTTP_403_FORBIDDEN,
        )

    if not settings.openai_api_key:
        raise AppError(
            "CONFIGURATION_ERROR",
            "OPENAI_API_KEY is not configured. Embeddings cannot be generated.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Concurrency guard: reject if any doc is currently being processed.
    processing = await _db_select(
        "/docs",
        {"status": "eq.processing", "select": "id"},
        auth.jwt,
    )
    if processing:
        raise AppError(
            "INGEST_RUNNING",
            f"An ingest run is already in progress ({len(processing)} document(s) processing).",
            status.HTTP_409_CONFLICT,
        )

    pending = await _db_select(
        "/docs",
        {"status": "eq.pending", "select": "id"},
        auth.jwt,
    )
    if not pending:
        return IngestTriggerResponse(queued=0, message="No pending documents to process.")

    background_tasks.add_task(_run_ingest_background)
    return IngestTriggerResponse(
        queued=len(pending),
        message=f"Queued {len(pending)} document(s) for processing.",
    )


@app.post("/admin/ingest/reset-stuck", response_model=IngestTriggerResponse)
async def reset_stuck_docs(
    auth: Auth = Depends(get_auth),  # noqa: B008
):
    """Reset documents stuck in 'processing' back to 'pending'.

    Use when a worker crashed mid-run and left documents in 'processing'.
    Admin role required.  See docs/ingest-runbook.md for guidance.
    """
    if not await _is_admin(auth.user_id, auth.jwt):
        raise AppError(
            "FORBIDDEN",
            "Admin role required.",
            status.HTTP_403_FORBIDDEN,
        )

    stuck = await _db_select(
        "/docs",
        {"status": "eq.processing", "select": "id"},
        auth.jwt,
    )
    if not stuck:
        return IngestTriggerResponse(queued=0, message="No stuck documents found.")

    # Patch each stuck document back to pending using service role
    # (user JWT cannot update docs to arbitrary states without going through
    # the admin-only UPDATE policy; service role bypasses RLS).
    import httpx as _httpx

    service_headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    async with _httpx.AsyncClient(
        base_url=f"{settings.supabase_url}/rest/v1",
        timeout=_TIMEOUT,
    ) as svc:
        resp = await svc.patch(
            "/docs",
            params={"status": "eq.processing"},
            json={"status": "pending"},
            headers=service_headers,
        )
        resp.raise_for_status()

    count = len(stuck)
    logger.info("reset_stuck_docs", extra={"count": count, "user_id": auth.user_id})
    return IngestTriggerResponse(
        queued=count,
        message=f"Reset {count} stuck document(s) to 'pending'.",
    )


@app.get("/admin/ingest/status", response_model=IngestStatusResponse)
async def ingest_status(
    auth: Auth = Depends(get_auth),  # noqa: B008
):
    """Return document counts grouped by status.  Admin role required."""
    if not await _is_admin(auth.user_id, auth.jwt):
        raise AppError(
            "FORBIDDEN",
            "Admin role required.",
            status.HTTP_403_FORBIDDEN,
        )

    rows = await _db_select("/docs", {"select": "status"}, auth.jwt)
    counts: dict[str, int] = {}
    for row in rows:
        s = row.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return IngestStatusResponse(counts=counts, total=len(rows))


# ---------------------------------------------------------------------------
# Retrieval endpoint (Day 16 — contract only; vector search added Day 18)
# ---------------------------------------------------------------------------

_SNIPPET_LEN = 300  # characters shown in text_snippet; tune when real chunks exist


@app.get("/retrieve", response_model=RetrieveResponse)
async def retrieve(
    query: str = "",
    top_k: int = 5,
    auth: Auth = Depends(get_auth),  # noqa: B008
):
    """Semantic chunk retrieval for RAG.

    Embeds the query, runs a pgvector cosine-similarity search via the
    match_chunks Postgres function, and returns the top_k nearest chunks.

    Returns an empty list for blank queries without hitting the embedding API.
    Returns 503 if OPENAI_API_KEY is not configured.

    Auth required: chunk content is clinical knowledge — not public.
    """
    if not 1 <= top_k <= 20:
        raise AppError(
            "VALIDATION_FAILED",
            "top_k must be between 1 and 20.",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    if not query.strip():
        return RetrieveResponse(items=[], top_k=top_k)

    if _embedder is None:
        raise AppError(
            "CONFIGURATION_ERROR",
            "Embedding provider is not configured. Set OPENAI_API_KEY in the environment.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    rows = await _retrieve_context(query, top_k, auth.jwt)
    items = [
        RetrievedChunk(
            chunk_id=str(r["chunk_id"]),
            doc_id=str(r["doc_id"]),
            title=r["title"],
            section=r.get("section"),
            score=float(r["score"]),
            # text_snippet is a display excerpt; full content stays server-side.
            text_snippet=r["content"][:_SNIPPET_LEN],
        )
        for r in rows
    ]
    return RetrieveResponse(items=items, top_k=top_k)
