"""Rank sticker replies using precomputed catalog annotations."""

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys
import unicodedata


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / "sticker-index.jsonl"
OCCASIONS = ("generic", "morning", "bedtime", "spring_festival", "meal", "other")
ACTION_SCOPES = ("generic", "purchase", "attend", "scout", "other")
RESPONSE_MODES = ("generic", "explicit_ack", "implicit_ack", "empathize", "cheer_up", "reassure")
INTENTS = (
    "acknowledge", "accept_task", "agree", "thank", "congratulate", "comfort",
    "apologize", "understand", "surprise", "confusion", "greet", "farewell",
    "encourage", "joke", "complain", "reject", "urgency", "thinking", "neutral",
)
TARGETS = ("self", "user", "third_party", "situation", "unclear")
SEND_MODES = {
    "after_text": ("after_text", "standalone", "needs_text"),
    "standalone": ("standalone",),
    "reaction": ("reaction",),
}

# Minimum formality, maximum intimacy, maximum teasing.
AUDIENCES = {
    "customer": (3, 0, 0),
    "company": (2, 0, 0),
    "team": (1, 1, 1),
    "team_private": (0, 1, 1),
    "friend": (0, 3, 3),
}
COMMITMENTS = {
    "none": {"none"},
    "received": {"none", "received"},
    "accept": {"none", "received", "accept"},
    "done": {"none", "received", "done"},
    "reject": {"none", "received", "reject"},
}


def load_catalog(path):
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: {exc.msg}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{number}: expected a JSON object")
            yield item


def labels(analysis, field):
    value = analysis.get(field)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry.strip()]


def level(analysis, field):
    value = analysis.get(field)
    return value if type(value) is int and 0 <= value <= 4 else None


def normalize(text):
    return unicodedata.normalize("NFKC", text).strip().casefold()


def related(left, right):
    left, right = normalize(left), normalize(right)
    return bool(left and right and (left in right or right in left))


def rank_item(item, args):
    analysis = item.get("analysis")
    if item.get("analysis_status") not in ("machine_labeled", "visually_reviewed"):
        return None
    if not isinstance(analysis, Mapping):
        return None
    if analysis.get("confidence") == "low":
        return None
    if not all(isinstance(item.get(field), str) and item[field] for field in ("id", "path")):
        return None

    primary = analysis.get("main_intent") == args.intent
    if not primary and args.intent not in labels(analysis, "secondary_intents"):
        return None
    risk = level(analysis, "risk_severity")
    if risk is None or not isinstance(analysis.get("risk_tags"), list):
        return None
    if risk > args.max_risk or (args.max_risk == 0 and labels(analysis, "risk_tags")):
        return None
    commitment = analysis.get("commitment")
    if not isinstance(commitment, str) or commitment not in COMMITMENTS[args.task_state]:
        return None
    if analysis.get("send_mode") not in SEND_MODES[args.placement]:
        return None
    target = analysis.get("target")
    if args.target and target not in args.target:
        return None

    min_formal, max_intimacy, max_teasing = AUDIENCES[args.audience]
    limits = {
        "formality": (min_formal, 4),
        "intimacy": (0, max_intimacy),
        "teasing": (0, max_teasing),
        "abstraction": args.abstraction,
        "meme_barrier": (0, args.max_meme_barrier),
        "context_dependence": (0, args.max_context_dependence),
    }
    for field, (low, high) in limits.items():
        value = level(analysis, field)
        if value is None or not low <= value <= high:
            return None
    if any(related(context, avoid) for context in args.context for avoid in labels(analysis, "avoid_contexts")):
        return None

    score = 10 if primary else 5
    reasons = ["main_intent" if primary else "secondary_intent"]
    if item["analysis_status"] == "visually_reviewed":
        score += 1
        reasons.append("reviewed")
    if any(related(context, good) for context in args.context for good in labels(analysis, "suitable_contexts")):
        score += 3
        reasons.append("suitable_context")
    if any(related(style, kind) for style in args.humor for kind in labels(analysis, "humor_types")):
        score += 2
        reasons.append("preferred_humor")
    for field, preferred in (("warmth", args.warmth), ("expressiveness", args.expressiveness)):
        value = level(analysis, field)
        if preferred is not None and value is not None:
            score += max(0, 2 - abs(value - preferred))
    if any(related(query, args.text) for query in labels(analysis, "keywords") if len(normalize(query)) > 1):
        score += 1
        reasons.append("message_keyword")

    score -= risk
    return {"id": item["id"], "path": item["path"], "series": item.get("series"),
            "score": score, "match_reasons": reasons, "analysis": analysis}


def rank_with_profile(item, args):
    ranked = rank_item(item, args)
    if ranked is None:
        return None
    profile = item.get("selection_profile")
    if not isinstance(profile, Mapping):
        profile = {}
    elif any(key in profile and profile[key] not in values for key, values in (
            ("occasion", OCCASIONS), ("action_scope", ACTION_SCOPES), ("response_mode", RESPONSE_MODES))):
        return None

    requested_occasion = getattr(args, "occasion", None) or "generic"
    occasion = profile.get("occasion", "generic")
    if occasion not in ("generic", requested_occasion):
        return None
    if getattr(args, "require_occasion", False) and occasion != requested_occasion:
        return None
    if occasion != "generic" and occasion == requested_occasion:
        ranked["score"] += 6
        ranked["match_reasons"].append("occasion")

    requested_scope = getattr(args, "action_scope", None)
    scope = profile.get("action_scope", "generic")
    scoped_commitment = ranked["analysis"]["commitment"] in ("accept", "done", "reject")
    if requested_scope and (scoped_commitment or getattr(args, "require_action_scope", False)):
        if scope not in ("generic", requested_scope):
            return None
        if getattr(args, "require_action_scope", False) and (not scoped_commitment or scope != requested_scope):
            return None
        if scope == requested_scope:
            ranked["score"] += 4
            ranked["match_reasons"].append("action_scope")

    requested_mode = getattr(args, "response_mode", None)
    if requested_mode and requested_mode != "generic":
        if profile.get("response_mode") == requested_mode:
            ranked["score"] += 4
            ranked["match_reasons"].append("response_mode")
        elif getattr(args, "require_response_mode", False):
            return None

    expressiveness = level(ranked["analysis"], "expressiveness")
    cap = getattr(args, "max_expressiveness", None)
    if cap is not None and (expressiveness is None or expressiveness > cap):
        return None
    return ranked


def select(items, args):
    if args.serious:
        return []
    matches = (rank_with_profile(item, args) for item in items)
    return sorted((match for match in matches if match is not None),
                  key=lambda match: (-match["score"], match["path"]))[:args.limit]


def level_arg(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer from 0 to 4") from exc
    if not 0 <= number <= 4:
        raise argparse.ArgumentTypeError("expected an integer from 0 to 4")
    return number


def range_arg(value):
    parts = value.split(":")
    if len(parts) == 2:
        low, high = (level_arg(part) for part in parts)
        if low <= high:
            return low, high
    raise argparse.ArgumentTypeError("expected LOW:HIGH with 0 <= LOW <= HIGH <= 4")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--intent", choices=INTENTS, required=True, help="Intended action of the reply")
    parser.add_argument("--audience", choices=AUDIENCES, default="team")
    parser.add_argument("--task-state", choices=COMMITMENTS, default="none", help="Actual task status")
    parser.add_argument("--context", action="append", default=[], help="Situation tags or short phrases")
    parser.add_argument("--text", default="", help="User message for optional keyword matching")
    parser.add_argument("--placement", choices=SEND_MODES, default="after_text")
    parser.add_argument("--serious", action="store_true", help="Suppress stickers for this reply")
    parser.add_argument("--abstraction", type=range_arg, default=(0, 4), metavar="LOW:HIGH")
    parser.add_argument("--max-meme-barrier", type=level_arg, default=1)
    parser.add_argument("--max-context-dependence", type=level_arg, default=2)
    parser.add_argument("--max-risk", type=level_arg, default=0)
    parser.add_argument("--target", action="append", choices=TARGETS, help="Allowed expression targets")
    parser.add_argument("--humor", action="append", default=[], help="Preferred humor, repeat to add styles")
    parser.add_argument("--warmth", type=level_arg, help="Preferred warmth")
    parser.add_argument("--expressiveness", type=level_arg, help="Preferred expressiveness")
    parser.add_argument("--max-expressiveness", type=level_arg, help="Hard persona ceiling")
    parser.add_argument("--occasion", choices=OCCASIONS, help="Actual occasion; defaults to generic")
    parser.add_argument("--require-occasion", action="store_true", help="Require an exact occasion label")
    parser.add_argument("--action-scope", choices=("purchase", "attend", "scout"), help="Actual action being committed")
    parser.add_argument("--require-action-scope", action="store_true", help="Require an exact concrete action")
    parser.add_argument("--response-mode", choices=RESPONSE_MODES[1:], help="Specific communicative function")
    parser.add_argument("--require-response-mode", action="store_true", help="Return no sticker if the function is unavailable")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    for flag, value in (("occasion", args.occasion), ("action_scope", args.action_scope), ("response_mode", args.response_mode)):
        if getattr(args, f"require_{flag}") and value is None:
            option = flag.replace("_", "-")
            parser.error(f"--require-{option} needs --{option}")
    try:
        results = select(load_catalog(args.catalog), args)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    # JSON remains readable when the Windows process output is piped.
    sys.stdout.buffer.write((json.dumps(results, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
