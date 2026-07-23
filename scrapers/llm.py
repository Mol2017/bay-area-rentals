"""Claude API client for the enrichment tier that regex can't answer.

Stdlib only, like the rest of the project -- this talks to the Messages API
over ``urllib`` rather than pulling in the ``anthropic`` package, so the daily
job still installs nothing. The tradeoff is that we hand-roll retries and
lose typed responses; both are cheap here because exactly two request shapes
are used.

Two paths, chosen by volume:

* ``enrich_one`` / ``enrich_many`` -- plain Messages API. Used for the daily
  delta (30-60 changed listings). Immediate, ~$0.60/day.
* ``submit_batch`` / ``poll_batch`` / ``batch_results`` -- Batches API, 50%
  cheaper, completes within the hour. Used for the one-time 833-listing
  backfill, where nothing is waiting on the result.

Batching the daily delta would save about 29 cents and force the cron into an
hour-long poll loop, so it deliberately isn't wired up that way.

Model split follows caseload difficulty, not volume:

* ``MODEL_BULK`` (Haiku 4.5) writes summaries and reads amenities off pages
  that already state them plainly -- high volume, low difficulty.
* ``MODEL_HARD`` (Opus 4.8) gets only listings where ``enrich.AMBIGUOUS``
  fired -- the phrasings where a wrong-but-plausible reading is the failure
  mode ("Double Occupancy: $900/month" as an upsell price on a private room).
  That set is small and adversarially selected, which is exactly where the
  more capable model earns its cost.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"

MODEL_BULK = "claude-haiku-4-5"
MODEL_MID = "claude-sonnet-5"
MODEL_HARD = "claude-opus-4-8"

# Listing text is already truncated to 20k chars by enrich.fetch_detail_text.
#
# 700 was too low: 29 of 568 responses hit the cap and were returned as
# truncated JSON, which is unrecoverable -- a cut-off structured response is
# not partially usable, it fails to parse entirely. Summary plus five
# evidence quotes plus the cross-check block runs long on verbose listings.
# The cap only bounds a runaway; it is not a cost control, since output
# tokens are a fraction of the ~2.2k input tokens per page.
MAX_TOKENS = 2000


class LLMUnavailable(RuntimeError):
    """No API key, or the API could not be reached.

    The runner catches this and keeps whatever the regex tier produced. A
    missing key degrades enrichment; it must never fail the scrape.
    """


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise LLMUnavailable(
            "ANTHROPIC_API_KEY is not set -- run with --no-llm for the "
            "regex-only path, or export a key to enable the LLM tier."
        )
    return key


# ── Output contract ───────────────────────────────────────────────────────
#
# Enum members mirror scrapers/schema.py exactly. "unknown" is a member
# rather than a null because a nullable enum makes the model reach for the
# closest value under pressure; naming the escape hatch keeps abstentions
# honest. It is mapped back to None before the value reaches a Unit.

_ENUM = lambda *v: {"type": "string", "enum": [*v, "unknown"]}  # noqa: E731
_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}

# Fields the parser also produces, read back independently so the two can be
# compared. This is not for overwriting the parser -- it is a second opinion
# from a different source (the detail page, not the index), and a
# disagreement is recorded rather than resolved. Nearly every bug found while
# building this was a confident wrong value that nothing contradicted; a
# second reader is what turns those into something visible.
CROSSCHECK_SCHEMA = {
    "type": "object",
    "properties": {
        # True only when the page is about the single unit at the address
        # given. Building pages that cover many units (Tripalink lists 271
        # units across 11 property pages) cannot confirm any one unit's rent
        # or bedroom count, and comparing against them would manufacture
        # conflicts that are really just the wrong question.
        "describes_one_unit": {"type": "boolean"},
        "bedrooms": _NUM,
        "bathrooms": _NUM,
        "rent": _NUM,
        "square_feet": _NUM,
        "available_date": _STR,
        "available_now": {"type": "boolean"},
        "city": _STR,
        "listing_kind": _ENUM("residential", "room", "parking", "storage",
                              "commercial", "application"),
    },
    "required": ["describes_one_unit", "bedrooms", "bathrooms", "rent",
                 "square_feet", "available_date", "available_now", "city",
                 "listing_kind"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "room_type": _ENUM("entire_place", "private_room", "shared_room"),
        "pets": _ENUM("allowed", "cats_only", "dogs_only", "none"),
        "furnished": _ENUM("furnished", "partial", "unfurnished"),
        "parking": _ENUM("garage", "off_street", "street", "none"),
        "laundry": _ENUM("in_unit", "shared", "hookups", "none"),
        "summary": {"type": "string"},
        "observed": CROSSCHECK_SCHEMA,
        "evidence": {
            "type": "object",
            "properties": {
                f: {"type": "string"}
                for f in ("room_type", "pets", "furnished", "parking", "laundry")
            },
            "additionalProperties": False,
        },
    },
    "required": ["room_type", "pets", "furnished", "parking", "laundry",
                 "summary", "evidence", "observed"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You read one rental listing's detail page and extract structured facts.

Report only what the page states about THIS unit. Use "unknown" whenever the \
page is silent, describes a different unit, or describes the neighbourhood \
rather than the listing. "unknown" is always better than a plausible guess -- \
a wrong value is committed to a public dataset and is not reviewed again.

room_type is what the renter actually gets:
- entire_place: the whole apartment or house
- private_room: your own bedroom in a shared unit (shared kitchen/bath is \
still private_room)
- shared_room: you share the bedroom itself, e.g. a bed in a double

An occupancy cap is NOT an answer on its own. "2 tenants max per room" says \
how many people the room holds, not whether the second one is the renter's \
partner or a stranger the operator matched them with. Answer "unknown" unless \
the page says which. A cap of exactly 1 does resolve it: private_room.

Three phrasings that are routinely misread, all from this dataset:
- "Double Occupancy: $900/month" listed beside a base rent is an OPTIONAL \
UPGRADE PRICE for a second person in a private room. It is private_room.
- A listing titled "PRIVATE ROOM" whose body says "rate is per room for \
double occupancy" is private_room -- the rate is quoted per room, and two \
people MAY share it. Title and body do not actually conflict.
- "Semi-private" describes a partitioned or shared sleeping area: shared_room.
- "Rent by the bed" / "a bed in a double" / "shared bed space": shared_room.

parking/laundry/pets/furnished describe the unit or its building, not the \
area. "Street parking is plentiful in this neighbourhood" is unknown, not \
street. An amenity offered to residents of the building (on-site laundry) \
does count. When the page has an explicit labelled field ("Laundry: In-unit \
Washer & Dryer", "Parking: NONE"), that is the listing stating a fact about \
itself -- prefer it over any phrase elsewhere on the page, including a \
neighbourhood description that mentions a garage.

- laundry: in_unit (machine inside the unit) / shared (on-site, communal, \
coin-op, laundry room) / hookups (connections only, no machine supplied) / \
none (explicitly stated absent)
- parking: garage (garage or carport) / off_street (assigned space, driveway, \
private lot) / street (street or permit parking only) / none
- furnished: furnished / partial (some rooms or "semi-furnished") / \
unfurnished
- pets: allowed (cats and dogs) / cats_only / dogs_only / none

"none" and "unknown" are different answers. "none" means the page states the \
amenity is absent; "unknown" means it is silent. Never use one for the other.

evidence: for every field you did NOT answer "unknown", quote the shortest \
span of the page that supports it, verbatim. Omit keys for unknown fields.

observed: read the headline facts back off the page independently, so they can be compared against what was parsed from the listing index. This is a cross-check, so do not copy anything from the "already determined" values above -- report only what THIS page states, and use null where it is silent. Do not calculate or infer: if the page gives a rent range, report the low end it advertises; if it never states square footage, null.

Set describes_one_unit to false when the page covers a whole building or several floor plans rather than the single unit at the address given. On those pages report only what is true building-wide and leave unit-level numbers null -- a per-unit rent read off a multi-unit page is not evidence about the unit being checked.

listing_kind is what the page is advertising: residential (a whole home), room (a bedroom within one), parking, storage, commercial, or application (a form or placeholder, not a rentable unit).

If the advertised rent buys a single room or bed rather than the whole unit, the summary MUST say so explicitly -- "rent is per room" or "priced per bed". This is the single most important thing a summary can carry: an $810 "3 bedroom" that is really one bedroom in a shared flat is misleading without it, and nothing else in the record states it.

summary: 1-2 plain sentences a renter would find useful -- what the place is, \
what stands out. No marketing language, no invented facts, no price repetition.

The summary describes the property, never this extraction task. Do not write \
about what the listing does or does not mention, and do not refer to "the \
listing" or "the page" at all. If the page says little, write a shorter \
summary of what it does say; a single clause is fine. Never return an empty \
summary -- at minimum state the unit type and location."""


def _user_prompt(item: dict) -> str:
    known = {k: v for k, v in (item.get("known") or {}).items() if v}
    lines = [
        f"Address: {item.get('address')}",
        f"Title: {item.get('title')}",
    ]
    if known:
        lines.append(
            "Already determined from structured data (do not contradict, and "
            f"do not re-derive): {json.dumps(known)}"
        )
    lines += ["", "--- listing page text ---", item.get("text") or ""]
    return "\n".join(lines)


def build_request(item: dict, *, model: str) -> dict:
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "output_config": {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
        "messages": [{"role": "user", "content": _user_prompt(item)}],
    }


# ── HTTP ──────────────────────────────────────────────────────────────────


def _post(path: str, payload: dict, *, betas: str | None = None, retries: int = 3) -> dict:
    body = json.dumps(payload).encode()
    headers = {
        "x-api-key": _api_key(),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    if betas:
        headers["anthropic-beta"] = betas
    return _request("POST", path, headers, body, retries)


def _get(path: str, *, retries: int = 3) -> dict:
    headers = {"x-api-key": _api_key(), "anthropic-version": API_VERSION}
    return _request("GET", path, headers, None, retries)


def _request(method: str, path: str, headers: dict, body, retries: int) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{API_BASE}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            # 4xx other than 429 will fail identically on retry.
            if e.code not in (429, 500, 502, 503, 529):
                raise LLMUnavailable(f"{method} {path} -> {e.code}: {detail}") from e
            last = LLMUnavailable(f"{method} {path} -> {e.code}: {detail}")
        except Exception as e:  # noqa: BLE001  network failures are varied
            last = e
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise LLMUnavailable(f"{method} {path} failed after {retries} attempts: {last}")


def _parse_response(message: dict) -> dict | None:
    """Pull the structured object out of a Messages API response."""
    if message.get("stop_reason") == "refusal":
        return None
    for block in message.get("content") or []:
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError:
                return None
    return None


def normalize(parsed: dict | None) -> dict:
    """Map the wire format onto Unit field values.

    ``"unknown"`` becomes None -- the schema's way of saying the source
    didn't state it, which is distinct from e.g. ``parking="none"``.
    """
    if not parsed:
        return {}
    out = {}
    for field in ("room_type", "pets", "furnished", "parking", "laundry"):
        value = parsed.get(field)
        out[field] = None if value in (None, "unknown", "") else value
    summary = (parsed.get("summary") or "").strip()
    out["summary"] = summary or None
    out["evidence"] = parsed.get("evidence") or {}
    # Passed through untouched for the caller to compare against the parser.
    # Deliberately not merged into the unit: this is a second opinion, not a
    # correction, and resolving it here would hide the disagreement.
    out["observed"] = parsed.get("observed") or {}
    return out


# ── Immediate path (daily delta) ──────────────────────────────────────────


def enrich_one(item: dict, *, model: str = MODEL_BULK) -> dict:
    return normalize(_parse_response(_post("/messages", build_request(item, model=model))))


def enrich_many(items: list[dict], *, model: str = MODEL_BULK, on_error=None) -> dict:
    """Enrich sequentially, keyed by each item's ``id``.

    One listing's failure must not sink the batch -- the caller keeps
    whatever the regex tier produced for it.
    """
    out: dict[str, dict] = {}
    for item in items:
        try:
            out[item["id"]] = enrich_one(item, model=model)
        except LLMUnavailable as e:
            if on_error:
                on_error(item, e)
    return out


# ── Batch path (one-time backfill) ────────────────────────────────────────


def submit_batch(items: list[dict], *, model: str = MODEL_BULK) -> str:
    """Queue up to 100k requests at 50% cost. Returns the batch id."""
    payload = {
        "requests": [
            {"custom_id": item["id"], "params": build_request(item, model=model)}
            for item in items
        ]
    }
    return _post("/messages/batches", payload)["id"]


def batch_status(batch_id: str) -> dict:
    """One-shot status check, for callers that poll across invocations."""
    return _get(f"/messages/batches/{batch_id}")


def poll_batch(batch_id: str, *, interval: int = 60, timeout: int = 86_400) -> dict:
    """Block until the batch ends. Batches usually finish well inside an hour."""
    waited = 0
    while True:
        batch = _get(f"/messages/batches/{batch_id}")
        if batch.get("processing_status") == "ended":
            return batch
        if waited >= timeout:
            raise LLMUnavailable(f"batch {batch_id} still running after {timeout}s")
        time.sleep(interval)
        waited += interval


def batch_results(batch_id: str) -> dict:
    """Fetch results, keyed by ``custom_id``.

    Results come back in arbitrary order, so they are keyed rather than
    zipped against the input list.
    """
    headers = {"x-api-key": _api_key(), "anthropic-version": API_VERSION}
    req = urllib.request.Request(
        f"{API_BASE}/messages/batches/{batch_id}/results", headers=headers
    )
    out: dict[str, dict] = {}
    with urllib.request.urlopen(req, timeout=300) as resp:
        for line in resp:                      # .jsonl, one result per line
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            result = row.get("result") or {}
            if result.get("type") != "succeeded":
                continue
            out[row["custom_id"]] = normalize(_parse_response(result["message"]))
    return out


def estimate_cost(n_items: int, *, model: str = MODEL_BULK, batch: bool = False,
                  avg_input_tokens: int = 5_000) -> float:
    """Rough USD estimate, for the runner to print before spending money."""
    rates = {                       # $ per million tokens (input, output)
        MODEL_BULK: (1.0, 5.0),     # Haiku 4.5
        MODEL_MID: (2.0, 10.0),     # Sonnet 5 introductory pricing
        MODEL_HARD: (5.0, 25.0),
    }
    rate_in, rate_out = rates.get(model, rates[MODEL_BULK])
    cost = (n_items * avg_input_tokens / 1e6) * rate_in + (n_items * 200 / 1e6) * rate_out
    return round(cost * (0.5 if batch else 1.0), 2)
