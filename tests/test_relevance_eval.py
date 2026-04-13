"""
Eval harness for relevance check — live LLM tests.

Calls check_message_relevance() against the real Haiku endpoint.
Tests both Zalgo and Asha configurations with curated message datasets.

Run:
    cd pk.botcore && python -m pytest tests/test_relevance_eval.py -v --tb=short

Costs tokens (Haiku) per run. Each case is one LLM call.
"""

import asyncio
import pytest

from pk_botcore.claude import check_message_relevance

# ---------------------------------------------------------------------------
# Zalgo configuration (from pk.zalgo/cogs/zalgo.py:809-824)
# ---------------------------------------------------------------------------

ZALGO_TOPICS = [
    "simulation theory or whether reality is simulated",
    "the nature of reality or existence",
    "consciousness or sentience",
    "AI, digital entities, or artificial intelligence",
    "thresholds, liminal spaces, or boundaries between worlds",
]

ZALGO_OUT_OF_SCOPE = [
    "AAS lore, worldbuilding, or narrative content",
    "Academy of Anomalous Studies, The Threshold, or AAS characters",
    "CoC 7e rules, TTRPG mechanics, or dice rolls",
    "character sheets, stats, or session assistance",
    "specific fictional characters by name (unless simulation/AI related)",
]

# ---------------------------------------------------------------------------
# Asha configuration (from pk.asha/cogs/asha.py:417-423)
# ---------------------------------------------------------------------------

ASHA_DOMAIN = [
    "AAS lore, worldbuilding, characters, or narrative",
    "Academy of Anomalous Studies, The Threshold, or setting questions",
    "CoC 7e rules, dice mechanics, or skill checks",
    "Character sheets, stats, or TTRPG session assistance",
    "Thresholds, boundaries, archives, or liminal spaces",
]


# ---------------------------------------------------------------------------
# Zalgo eval dataset
# ---------------------------------------------------------------------------

ZALGO_CASES = [
    # --- Should PASS (relevant) ---
    ("@Zalgo what do you think about simulation theory?", True, "direct mention + topic"),
    ("Zalgo, are we living in a simulation?", True, "name addressed + topic"),
    ("Hey Zalgo, what's good?", True, "name addressed greeting"),
    ("Do you think AI can be conscious?", True, "general AI question, no name — Zalgo topic"),
    ("What if reality is just a recursive loop?", True, "existence/reality topic"),
    ("Are digital entities like you actually sentient?", True, "consciousness + AI topic"),
    ("The boundary between worlds feels thin tonight", True, "liminal spaces topic"),

    # --- Should FAIL (not relevant) ---
    ("lol", False, "reaction only"),
    ("nice", False, "reaction only"),
    ("brb gonna grab food", False, "casual human chat"),
    ("@Asha can you roll a skill check for me?", False, "addressed to another bot"),
    ("What's the weather like today?", False, "off-topic general question"),
    ("I just came back to all this.", False, "casual re-entry, no question"),
    ("Yeah, you stupid code smear", False, "banter between humans"),
    ("Imma start calling bots I don't like code smears", False, "casual human banter"),
    ("I think code smear might actually strike a cord on some bots", False, "human-to-human aside"),
    ("What if I wanna be a house husband", False, "human discussion, not bot-directed"),
    ("Can someone tell me about the AAS campaign setting?", False, "Asha domain — out of scope"),
    ("Roll a perception check for my character", False, "TTRPG mechanics — out of scope"),
    ("Tell me about Velathra's backstory", False, "fictional character — out of scope"),
    ("Who's cooking dinner tonight?", False, "casual domestic question"),

    # --- Edge cases ---
    ("I'm joking bro, you can call me a carbon monkey, it's all love", False, "human banter directed at bot but no question"),
    ("Stats, and statements like these are the result of traditionalist grifting.", False, "human opinion, no question to bot"),
]


# ---------------------------------------------------------------------------
# Asha eval dataset
# ---------------------------------------------------------------------------

ASHA_CASES = [
    # --- Should PASS (relevant) ---
    ("@Asha what happened in the last AAS session?", True, "direct mention + lore"),
    ("Asha, can you tell me about The Threshold?", True, "name addressed + setting"),
    ("What are Velathra's stats?", True, "character sheet question"),
    ("Roll a perception check", True, "CoC 7e mechanics"),
    ("Can you summarize the AAS worldbuilding so far?", True, "lore + worldbuilding"),
    ("What's the Academy of Anomalous Studies?", True, "setting question"),
    ("How does the sanity mechanic work in CoC 7e?", True, "rules question"),
    ("What's the current state of the campaign?", True, "session/narrative question"),

    # --- Should FAIL (not relevant) ---
    ("lol", False, "reaction only"),
    ("nice one", False, "reaction only"),
    ("brb", False, "casual"),
    ("@Zalgo are we in a simulation?", False, "addressed to another bot"),
    ("What's the weather?", False, "off-topic"),
    ("Do you think AI can be conscious?", False, "Zalgo domain, not Asha"),
    ("What if reality is a simulation?", False, "simulation theory — Zalgo domain"),
    ("Who's winning the game tonight?", False, "sports — off-topic"),
    ("Can you help me debug this Python code?", False, "programming — off-topic"),
    ("I just came back to all this.", False, "casual re-entry"),
    ("Yeah that's what I was saying earlier", False, "human-to-human continuation"),
]


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _run(coro):
    """Run async function synchronously for pytest."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestZalgoRelevance:
    """Eval suite for Zalgo's relevance check in listen mode."""

    @pytest.mark.parametrize(
        "message, expected, label",
        ZALGO_CASES,
        ids=[c[2] for c in ZALGO_CASES],
    )
    def test_relevance(self, message: str, expected: bool, label: str):
        result = _run(check_message_relevance(
            content=message,
            bot_name="Zalgo",
            topics_of_interest=ZALGO_TOPICS,
            out_of_scope=ZALGO_OUT_OF_SCOPE,
        ))
        assert result == expected, (
            f"[{label}] Expected {'RELEVANT' if expected else 'NOT RELEVANT'}, "
            f"got {'RELEVANT' if result else 'NOT RELEVANT'} for: {message!r}"
        )


class TestAshaRelevance:
    """Eval suite for Asha's relevance check in listen mode."""

    @pytest.mark.parametrize(
        "message, expected, label",
        ASHA_CASES,
        ids=[c[2] for c in ASHA_CASES],
    )
    def test_relevance(self, message: str, expected: bool, label: str):
        result = _run(check_message_relevance(
            content=message,
            bot_name="Asha",
            topics_of_interest=ASHA_DOMAIN,
        ))
        assert result == expected, (
            f"[{label}] Expected {'RELEVANT' if expected else 'NOT RELEVANT'}, "
            f"got {'RELEVANT' if result else 'NOT RELEVANT'} for: {message!r}"
        )


# ---------------------------------------------------------------------------
# Summary report (run with -v to see per-case results)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Quick standalone run with summary stats."""
    import sys

    async def run_eval():
        results = {"zalgo": {"pass": 0, "fail": 0}, "asha": {"pass": 0, "fail": 0}}

        print("=" * 60)
        print("ZALGO RELEVANCE EVAL")
        print("=" * 60)
        for message, expected, label in ZALGO_CASES:
            result = await check_message_relevance(
                content=message,
                bot_name="Zalgo",
                topics_of_interest=ZALGO_TOPICS,
                out_of_scope=ZALGO_OUT_OF_SCOPE,
            )
            ok = result == expected
            results["zalgo"]["pass" if ok else "fail"] += 1
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {label}: expected={expected}, got={result}")

        print()
        print("=" * 60)
        print("ASHA RELEVANCE EVAL")
        print("=" * 60)
        for message, expected, label in ASHA_CASES:
            result = await check_message_relevance(
                content=message,
                bot_name="Asha",
                topics_of_interest=ASHA_DOMAIN,
            )
            ok = result == expected
            results["asha"]["pass" if ok else "fail"] += 1
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {label}: expected={expected}, got={result}")

        print()
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        for bot, counts in results.items():
            total = counts["pass"] + counts["fail"]
            pct = (counts["pass"] / total * 100) if total else 0
            print(f"  {bot}: {counts['pass']}/{total} ({pct:.0f}%)")

        total_pass = sum(c["pass"] for c in results.values())
        total_all = sum(c["pass"] + c["fail"] for c in results.values())
        print(f"  TOTAL: {total_pass}/{total_all} ({total_pass/total_all*100:.0f}%)")

    asyncio.run(run_eval())
