import asyncio
import json as _json
import logging
import random
import re
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError, field_validator

from classifier import ClassifiedInput, classify_input  # noqa: F401
from constants import DISCLAIMER_CLINICAL, SNIPPET_LEN
from embedder import OpenAIEmbedder
from policy import PROMPT_VERSION, SYSTEM_PROMPT  # noqa: F401  (used in future Claude integration)
from redflag_detector import (
    CATEGORY_LABELS,
    ESCALATION_STEPS,
    URGENT_PATIENT_SUMMARY,
    RedFlagResult,
    detect_red_flags,
)
from router import RouterDecision, route
from settings import Settings, configure_logging
from tools import guidelines_search
from tools.guidelines_search import GuidelinesSearchInput, GuidelinesSearchOutput
from validator import validate_and_repair
from workflows import run_doc_qa, run_patient_message, run_summary, run_triage

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


async def _db_patch_service_role(table: str, filter_params: dict, data: dict) -> None:
    """PATCH rows using the service role key, bypassing PostgREST RLS.

    Only for admin operations that the user JWT cannot perform (e.g. resetting
    doc status).  Reuses the shared _db_client connection pool — no new TCP
    connections opened per call.
    """
    resp = await _retrying_request(
        lambda: _db_client.patch(
            f"/{table}",
            params=filter_params,
            json=data,
            headers={
                # Override only Authorization; apikey + Content-Type come from
                # _db_client's default headers.
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Prefer": "return=minimal",
            },
        )
    )
    resp.raise_for_status()


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
    mode: Literal["triage", "summary", "patient_message", "doc_qa"]
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
    mock vs. real model, which model ID, prompt version, and which workflow
    pipeline was selected by the router.
    Not stored in the messages table — it lives in ChatResponse only.
    (workflow_name and router_reason ARE stored in message._meta for audit.)
    """

    is_mock: bool
    model: str  # "mock-v1" | "claude-opus-4-6" | etc.
    prompt_version: str  # matches policy.PROMPT_VERSION
    workflow: str = "triage"  # workflow that generated this response
    router_reason: str = "mode_triage"  # why the router chose this workflow
    # Tool metadata (Day 23) — IDs only, never snippet content
    tool_used: bool = False
    tool_name: str | None = None
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    # Red flag metadata (Day 24) — category labels only, no raw text
    redflag_severity: str = "none"
    redflag_categories: list[str] = Field(default_factory=list)


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

# ---------------------------------------------------------------------------
# Tool-use gate
#
# _GUIDELINE_QUERY_RE — detects when a triage query explicitly asks for
#   guideline-based reasoning. Matches Polish and English references.
#   Pattern is intentionally broad: false positives are safe (extra retrieval),
#   false negatives silently skip the tool (triage still returns a valid payload).
#
# _should_use_tool — deterministic, pure function, no I/O.
#   doc_qa:           always uses GuidelinesSearch.
#   triage:           uses it only when the user explicitly references guidelines.
#   summary / patient_message: never use the tool.
#
# _run_guidelines_search — wires live singletons into the injected-dep tool.
#   Returns empty output when _embedder is not configured.
#   Exceptions propagate to the caller, which logs and falls back to empty.
# ---------------------------------------------------------------------------

_GUIDELINE_QUERY_RE = re.compile(
    r"\b("
    r"na\s+podstawie\s+wytyczn\w*"
    r"|zgodnie\s+z\s+wytyczn\w*"
    r"|wytyczn\w+\s+\w+"  # "wytyczne PTK", "wytyczne kliniczne", etc.
    r"|based\s+on\s+(the\s+)?guidelines?"
    r"|according\s+to\s+(the\s+)?guidelines?"
    r")\b",
    re.IGNORECASE,
)


def _should_use_tool(workflow: str, input_text: str) -> bool:
    """Return True when GuidelinesSearch should run for this (workflow, input) pair.

    Deterministic — no I/O, no side effects. Safe to call in tests directly.
    """
    if workflow == "doc_qa":
        return True
    if workflow == "triage":
        return bool(_GUIDELINE_QUERY_RE.search(input_text))
    return False  # summary, patient_message


async def _run_guidelines_search(query: str, jwt: str, top_k: int = 3) -> GuidelinesSearchOutput:
    """Call the GuidelinesSearch tool with live singleton dependencies.

    Returns empty output when _embedder is not configured so the pipeline
    degrades gracefully (same pattern as _retrieve_context).
    """
    if _embedder is None:
        return GuidelinesSearchOutput(items=[])
    tool_input = GuidelinesSearchInput(query=query, top_k=top_k)
    return await guidelines_search.run(
        tool_input,
        embedder_embed=_embedder.embed,
        db_rpc=_db_rpc,
        jwt=jwt,
    )


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


def _dispatch_workflow(
    workflow: str, classified: ClassifiedInput, rag_context: str = ""
) -> dict[str, Any]:
    """Dispatch to the appropriate workflow function and return a raw payload dict.

    Week 3 seam: each branch calls a workflow function that will be replaced with
    a real LLM call (same signature, different body — see workflows.py docstring).

    Patchable in tests: patch "main._dispatch_workflow" to inject invalid dicts.
    Only called for the normal path (not unsafe, not vague — those short-circuit earlier).
    """
    if workflow == "summary":
        return run_summary(classified, rag_context)
    if workflow == "patient_message":
        return run_patient_message(classified, rag_context)
    if workflow == "doc_qa":
        return run_doc_qa(classified, rag_context)
    return run_triage(classified, rag_context)  # default: triage


def _build_refuse_payload() -> AssistantPayload:
    """Returned when the request is unsafe or out-of-scope (flag='refuse')."""
    return AssistantPayload(
        questions_to_ask=[],
        red_flags=[],
        possible_next_steps=[],
        patient_facing_summary="",
        sources=[],
        flag="refuse",
        disclaimer=DISCLAIMER_CLINICAL,
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
        disclaimer=DISCLAIMER_CLINICAL,
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
        disclaimer=DISCLAIMER_CLINICAL,
    )


def _enforce_redflag_policy(raw_dict: dict[str, Any], redflag: RedFlagResult) -> None:
    """Mutate *raw_dict* in-place to enforce stop-the-line escalation when severity=high.

    Called only for triage workflow after _dispatch_workflow returns.
    Must run before validate_and_repair so the enforced fields pass schema validation.

    Rules:
    - red_flags: replaced with human-readable category labels (no raw text).
    - possible_next_steps: escalation steps prepended; original steps follow.
    - patient_facing_summary: replaced with urgent wording.
    - sources: untouched (main.py injects them separately).
    - flag: kept as "uncertain" — triage always needs more info.
    """
    if not redflag.is_high:
        return

    raw_dict["red_flags"] = [CATEGORY_LABELS[c] for c in redflag.categories if c in CATEGORY_LABELS]
    raw_dict["possible_next_steps"] = ESCALATION_STEPS + raw_dict.get("possible_next_steps", [])
    raw_dict["patient_facing_summary"] = URGENT_PATIENT_SUMMARY


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
# Chat pipeline
#
# _execute_chat_pipeline contains all shared logic between /chat and
# /chat/stream: classify → safety → conversation ownership → idempotency →
# route → retrieve → dispatch → validate/repair → groundedness.
#
# Both handlers call this function, then differ only in how they persist
# messages and what response type they return.  This prevents any safety or
# routing change from being applied in one handler but forgotten in the other.
# ---------------------------------------------------------------------------


@dataclass
class _PipelineResult:
    """Return value of _execute_chat_pipeline.

    is_replay=True  → idempotency hit; replay_meta contains the stored _meta
                      dict.  Caller must NOT insert DB messages.
    is_replay=False → normal path; caller inserts user + assistant messages.
    """

    payload: AssistantPayload
    classified: ClassifiedInput
    decision: RouterDecision
    repair_applied: bool
    high_confidence: list[dict]
    is_replay: bool = False
    replay_meta: dict = field(default_factory=dict)
    # Tool metadata — stored in message._meta, surfaced in ResponseMetadata.
    # IDs only: never include query text or snippet content here.
    tool_used: bool = False
    tool_name: str | None = None
    tool_query_hash: str | None = None
    retrieved_chunk_ids: list[str] = field(default_factory=list)
    # Red flag metadata (Day 24) — labels only, no raw patient text.
    redflag_severity: str = "none"
    redflag_categories: list[str] = field(default_factory=list)


async def _execute_chat_pipeline(
    body: "ChatRequest",
    auth: "Auth",
    request_id: str,
    idempotency_key: str | None,
) -> _PipelineResult:
    """Shared pipeline for /chat and /chat/stream.

    Raises AppError on any failure (PII, mock disabled, conversation not found,
    validation failed).  On idempotency hit, returns result.is_replay=True with
    result.replay_meta populated; caller skips DB inserts.
    """
    user_id = auth.user_id

    # 0. Classify input — single pass over all checks.
    classified = classify_input(body.input_text)

    # 0a. PII — abort before any DB write.
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

    # 0b. Injection detected — log and continue (non-blocking).
    if classified.has_injection:
        logger.warning(
            "injection_detected",
            extra={
                "request_id": request_id,
                "injection_flags": classified.injection_flags,
            },
        )

    # 0c. Feature flag.
    if not settings.chat_mock_mode:
        raise AppError(
            "MOCK_DISABLED",
            "LLM integration not yet enabled. Set CHAT_MOCK_MODE=true to use mock responses.",
            status.HTTP_501_NOT_IMPLEMENTED,
            request_id,
        )

    # 1. Verify conversation ownership.
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

    # 1a. Idempotency check — before any writes.
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
            # Synthesise a RouterDecision so the finally log in /chat has valid values.
            _replay_decision = RouterDecision(
                workflow=meta.get("workflow_name", body.mode),
                reason=meta.get("router_reason", f"mode_{body.mode}"),
            )
            return _PipelineResult(
                payload=cached_payload,
                classified=classified,
                decision=_replay_decision,
                repair_applied=False,
                high_confidence=[],
                is_replay=True,
                replay_meta=meta,
            )

    # 2. Route request to the appropriate workflow.
    decision = route(body.mode, body.input_text)
    logger.info(
        "workflow_routed",
        extra={
            "request_id": request_id,
            "workflow": decision.workflow,
            "router_reason": decision.reason,
        },
    )

    # 3. Insert user message — include classification + routing metadata.
    # Persisted here (before dispatch/validation) so the user's intent is always
    # recorded even if validation fails later.  _meta stores flag labels and a
    # content-hash for audit; never stores raw flagged substrings or patient text.
    # Determine tool eligibility at routing time so it can be persisted in the
    # user message _meta.  Skipped for unsafe/vague inputs (short-circuit in step 5).
    tool_should_run = (
        _should_use_tool(decision.workflow, body.input_text)
        and not classified.is_unsafe_request
        and not classified.is_vague
    )

    user_meta: dict[str, Any] = {
        "input_hash": classified.input_hash,
        "injection_flags": classified.injection_flags,
        "is_vague": classified.is_vague,
        "is_unsafe_request": classified.is_unsafe_request,
        "prompt_version": PROMPT_VERSION,
        "workflow_name": decision.workflow,
        "router_reason": decision.reason,
        "tool_expected": tool_should_run,
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

    # 4. Tool-gated retrieval via GuidelinesSearch.
    #
    # doc_qa:      always runs the tool.
    # triage:      runs only when the user explicitly references guidelines.
    # summary/patient_message: no retrieval.
    #
    # Failures degrade gracefully to empty output — the pipeline continues
    # and the no-source fallback fires if tool_should_run but items=[].
    tool_output: GuidelinesSearchOutput | None = None
    high_confidence: list[dict] = []
    rag_context = ""

    if tool_should_run:
        try:
            tool_output = await _run_guidelines_search(body.input_text, auth.jwt)
            # Map tool items to the dict format expected by _format_rag_context.
            # content=item.snippet is sufficient for mock mode; Week 3 will use
            # full chunk text from the DB for LLM context assembly.
            high_confidence = [
                {
                    "chunk_id": item.chunk_id,
                    "doc_id": item.doc_id,
                    "title": item.title,
                    "section": item.section,
                    "content": item.snippet,
                    "score": item.score,
                }
                for item in tool_output.items
            ]
            rag_context = _format_rag_context(high_confidence)
            logger.info(
                "tool_executed",
                extra={
                    "request_id": request_id,
                    "tool_name": guidelines_search.TOOL_NAME,
                    # query_hash instead of raw query — privacy-safe audit reference
                    "tool_query_hash": guidelines_search.query_hash(body.input_text),
                    # chunk_ids only — no snippet content in logs
                    "retrieved_chunk_ids": [item.chunk_id for item in tool_output.items],
                    "item_count": len(tool_output.items),
                },
            )
        except Exception as exc:
            logger.warning(
                "tool_retrieval_skipped",
                extra={"request_id": request_id, "reason": type(exc).__name__},
            )
            tool_output = GuidelinesSearchOutput(items=[])

    # 5. Red flag detection — triage only, before dispatch.
    #    Runs on every triage request regardless of guardrail outcome so that
    #    severity metadata is always available for audit.  Enforcement only fires
    #    on the normal path (not unsafe, not vague).
    redflag = detect_red_flags(body.input_text) if decision.workflow == "triage" else RedFlagResult(severity="none")
    if redflag.is_high:
        logger.warning(
            "redflag_detected",
            extra={
                "request_id": request_id,
                "redflag_severity": redflag.severity,
                "redflag_categories": redflag.categories,
                # explanation contains only category names — safe to log
                "redflag_explanation": redflag.explanation,
            },
        )

    # 5a. Build raw payload dict.
    if classified.is_unsafe_request:
        logger.warning("unsafe_request_refused", extra={"request_id": request_id})
        raw_dict = _build_refuse_payload().model_dump()
    elif classified.is_vague:
        logger.info("vague_input_uncertain", extra={"request_id": request_id})
        raw_dict = _build_uncertain_payload().model_dump()
    else:
        raw_dict = _dispatch_workflow(decision.workflow, classified, rag_context)
        # Stop-the-line: enforce escalation fields for high-severity triage.
        _enforce_redflag_policy(raw_dict, redflag)

    # 4a. Inject sources — chunk_id + snippet, only from actual retrieval.
    if high_confidence and not classified.is_unsafe_request and not classified.is_vague:
        raw_dict["sources"] = [
            {
                "id": str(r["chunk_id"]),
                "title": r["title"],
                "section": r.get("section") or "",
                "text_snippet": r["content"][:SNIPPET_LEN],
            }
            for r in high_confidence
        ]

    # 5. Validate — repair once if needed — validate again.
    try:
        payload, repair_applied = validate_and_repair(raw_dict, AssistantPayload)
    except ValidationError as exc:
        logger.error(
            "payload_validation_failed",
            extra={
                "request_id": request_id,
                "validation_error_code": "schema_invalid_after_repair",
                "error_count": exc.error_count(),
            },
        )
        safe_detail = "Assistant response did not meet the required schema. Please retry."
        if settings.validation_debug:
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

    # 6. Groundedness enforcement.
    #
    # No-source fallback: fires when the tool WAS supposed to run (doc_qa always;
    # triage with explicit guideline reference) but returned zero items.
    # Distinct from unsafe/vague paths which short-circuit before retrieval.
    #
    # For workflows where the tool was not triggered (summary, patient_message,
    # triage without guideline language), high_confidence=[] is intentional and
    # the fallback must NOT fire — those payloads stand without citations.
    if tool_should_run and not high_confidence:
        logger.info(
            "no_source_fallback",
            extra={"request_id": request_id, "workflow": decision.workflow},
        )
        payload = _build_no_source_payload()
    else:
        payload = _check_groundedness(payload, high_confidence)

    # Resolve tool audit fields for _PipelineResult.
    _tool_actually_used = tool_should_run and tool_output is not None
    _retrieved_ids = [item.chunk_id for item in tool_output.items] if tool_output else []

    return _PipelineResult(
        payload=payload,
        classified=classified,
        decision=decision,
        repair_applied=repair_applied,
        high_confidence=high_confidence,
        tool_used=_tool_actually_used,
        tool_name=guidelines_search.TOOL_NAME if _tool_actually_used else None,
        tool_query_hash=guidelines_search.query_hash(body.input_text)
        if _tool_actually_used
        else None,
        retrieved_chunk_ids=_retrieved_ids,
        redflag_severity=redflag.severity,
        redflag_categories=redflag.categories,
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
    result: _PipelineResult | None = None  # set in try block; read safely in finally

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
        result = await _execute_chat_pipeline(body, auth, request_id, idempotency_key)

        # Idempotency replay — skip DB writes, reconstruct response from stored meta.
        if result.is_replay:
            meta = result.replay_meta
            return ChatResponse(
                request_id=request_id,
                assistant_payload=result.payload,
                response_metadata=ResponseMetadata(
                    is_mock=meta.get("is_mock", True),
                    model=meta.get("model", "mock-v1"),
                    prompt_version=meta.get("prompt_version", PROMPT_VERSION),
                    workflow=meta.get("workflow_name", body.mode),
                    router_reason=meta.get("router_reason", f"mode_{body.mode}"),
                ),
            )

        decision = result.decision
        payload = result.payload

        # Insert assistant message — validation must pass before storage.
        # Raw invalid output is never stored; only the repaired-and-validated payload.
        # (User message was already inserted inside _execute_chat_pipeline.)
        assistant_meta: dict[str, Any] = {
            "prompt_version": PROMPT_VERSION,
            "model": "mock-v1",
            "is_mock": True,
            "validation_status": "valid",
            "repair_applied": result.repair_applied,
            "workflow_name": decision.workflow,
            "router_reason": decision.reason,
            # Tool audit fields — IDs only, never query text or snippet content
            "tool_used": result.tool_used,
            "tool_name": result.tool_name,
            "tool_query_hash": result.tool_query_hash,
            "retrieved_chunk_ids": result.retrieved_chunk_ids,
            "redflag_severity": result.redflag_severity,
            "redflag_categories": result.redflag_categories,
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
                workflow=decision.workflow,
                router_reason=decision.reason,
                tool_used=result.tool_used,
                tool_name=result.tool_name,
                retrieved_chunk_ids=result.retrieved_chunk_ids,
                redflag_severity=result.redflag_severity,
                redflag_categories=result.redflag_categories,
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
                # router metadata — safe strings, no user content
                "workflow": result.decision.workflow if result else "unrouted",
                "router_reason": result.decision.reason if result else "none",
                # counts only — never raw content
                "injection_flag_count": len(result.classified.injection_flags) if result else 0,
                "retrieved_count": len(result.high_confidence) if result else 0,
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
    In Week 2: replace _mock_sse_stream with a real LLM streaming call and
    yield tokens as they arrive; store the message in a background task after
    the stream closes.
    """
    request_id = str(uuid.uuid4())
    user_id = auth.user_id

    result = await _execute_chat_pipeline(body, auth, request_id, idempotency_key)

    # Idempotency replay — stream the cached payload without any DB writes.
    if result.is_replay:
        return StreamingResponse(
            _mock_sse_stream(result.payload, request_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Request-Id": request_id},
        )

    decision = result.decision
    payload = result.payload

    # Persist the complete assistant message before streaming begins.
    # (User message was already inserted inside _execute_chat_pipeline.)
    # (In mock mode the payload is fully determined; Week 2 uses a background task.)
    assistant_meta: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "model": "mock-v1",
        "is_mock": True,
        "validation_status": "valid",
        "repair_applied": result.repair_applied,
        "workflow_name": decision.workflow,
        "router_reason": decision.reason,
        # Tool audit fields — IDs only, never query text or snippet content
        "tool_used": result.tool_used,
        "tool_name": result.tool_name,
        "tool_query_hash": result.tool_query_hash,
        "retrieved_chunk_ids": result.retrieved_chunk_ids,
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

    # Patch each stuck document back to pending using the service role key.
    # The user JWT cannot UPDATE docs to arbitrary statuses via the RLS policy;
    # _db_patch_service_role bypasses RLS using the service role key while
    # still reusing the shared _db_client connection pool.
    await _db_patch_service_role(
        "docs",
        {"status": "eq.processing"},
        {"status": "pending"},
    )

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
            text_snippet=r["content"][:SNIPPET_LEN],
        )
        for r in rows
    ]
    return RetrieveResponse(items=items, top_k=top_k)
