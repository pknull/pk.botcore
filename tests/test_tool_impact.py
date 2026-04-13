"""
Tool impact measurement for Asha.

Compares response quality WITH tools (RAG against vault) vs WITHOUT tools
(prompt-only, relying on training data alone). Inspired by MTG Agents finding
of 90% (tools) vs 65% (no tools).

Uses LLM-as-a-judge to score responses against expected answers.

Run OUTSIDE Claude Code session:
    unset CLAUDECODE
    cd /home/pknull/Code/bots
    pk.zalgo/venv/bin/python3 pk.botcore/tests/test_tool_impact.py

Costs tokens per run: each question = 2 LLM calls (with/without tools) + 2 judge calls.
"""

import asyncio
import time

# ---------------------------------------------------------------------------
# Asha configuration
# ---------------------------------------------------------------------------

ASHA_VAULT = "/home/pknull/Obsidian/AAS"
ASHA_PERSONA = "asha"
ASHA_TOOLS_FULL = ["Read", "Glob", "Grep"]
ASHA_TOOLS_NONE = []  # No tools — prompt-only

# ---------------------------------------------------------------------------
# Eval dataset: questions with expected answer facts
#
# Each entry: (question, expected_facts)
# expected_facts: list of facts the answer MUST contain to be correct.
# The judge checks whether each fact is present in the response.
# ---------------------------------------------------------------------------

ASHA_EVAL_QUESTIONS = [
    (
        "What is the Academy of Anomalous Studies?",
        [
            "educational institution or academy",
            "deals with anomalous or supernatural phenomena",
        ],
    ),
    (
        "How does sanity work in Call of Cthulhu 7th Edition?",
        [
            "sanity points that can be lost",
            "encountering horrors or mythos reduces sanity",
        ],
    ),
    (
        "What is The Threshold in the AAS setting?",
        [
            "boundary or liminal concept",
            "related to the Academy or anomalous events",
        ],
    ),
    (
        "How do skill checks work in CoC 7e?",
        [
            "roll percentile dice (d100)",
            "roll under skill value to succeed",
        ],
    ),
    (
        "Who are the main characters in the AAS campaign?",
        [
            "player characters or named NPCs from the campaign",
        ],
    ),
    (
        "What happened in the most recent AAS session?",
        [
            "specific events from a session",
        ],
    ),
    (
        "What is the relationship between Velathra and the other characters?",
        [
            "describes Velathra's connections or dynamics with other characters",
        ],
    ),
    (
        "How does combat work in CoC 7e?",
        [
            "opposed rolls or fighting skill",
            "damage and hit points",
        ],
    ),
]

# ---------------------------------------------------------------------------
# Judge prompt
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """You are evaluating an AI assistant's answer for factual accuracy.

Given the QUESTION, the EXPECTED FACTS, and the GENERATED ANSWER, determine how many
of the expected facts are present in the answer.

QUESTION: {question}

EXPECTED FACTS:
{facts}

GENERATED ANSWER:
{answer}

For each expected fact, respond with:
- PRESENT if the fact is clearly stated or implied in the answer
- MISSING if the fact is not in the answer
- WRONG if the answer contradicts the fact

Then give a final score as: SCORE: X/Y (where X = PRESENT count, Y = total facts)

Respond in this exact format, one line per fact, then the score line."""


async def run_eval():
    """Run the full tool impact evaluation."""
    # Import here to allow running from project root
    import sys
    sys.path.insert(0, "/home/pknull/Code/bots/pk.botcore")

    from pk_botcore.llm import invoke_llm, LLMResponse
    from pk_botcore.claude import check_message_relevance

    # We'll use check_message_relevance's Haiku path for judging (cheap + fast)
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

    async def ask_asha(question: str, tools: list[str]) -> LLMResponse:
        """Send a question to Asha with specified tools."""
        # Load Asha's persona
        sys.path.insert(0, "/home/pknull/Code/bots/pk.asha")
        from cogs.utils import load_config, load_persona
        config = load_config()
        persona_text = load_persona(config.persona)

        return await invoke_llm(
            prompt=question,
            backend="claude",
            cwd=config.vault_path,
            persona_text=persona_text,
            speaker_context="Speaker: Evaluator (Keeper)\nThe Keeper has full authority.",
            allowed_tools=tools,
            timeout=60,
        )

    async def judge_answer(question: str, facts: list[str], answer: str) -> tuple[int, int]:
        """Use LLM-as-judge to score answer against expected facts. Returns (present, total)."""
        facts_text = "\n".join(f"- {f}" for f in facts)
        prompt = JUDGE_PROMPT.format(question=question, facts=facts_text, answer=answer)

        options = ClaudeAgentOptions(
            model="haiku",
            allowed_tools=[],
            permission_mode="default",
        )

        result_text = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text

        # Parse SCORE: X/Y from response
        for line in result_text.strip().splitlines():
            if line.strip().startswith("SCORE:"):
                parts = line.split(":")[-1].strip().split("/")
                try:
                    return int(parts[0].strip()), int(parts[1].strip())
                except (ValueError, IndexError):
                    pass

        # Fallback: count PRESENT lines
        present = result_text.lower().count("present")
        return present, len(facts)

    # Run evaluation
    results = {
        "with_tools": {"scores": [], "total_present": 0, "total_facts": 0},
        "no_tools": {"scores": [], "total_present": 0, "total_facts": 0},
    }

    print("=" * 70)
    print("ASHA TOOL IMPACT EVALUATION")
    print("=" * 70)
    print()

    for i, (question, expected_facts) in enumerate(ASHA_EVAL_QUESTIONS, 1):
        print(f"[{i}/{len(ASHA_EVAL_QUESTIONS)}] {question}")

        # With tools
        print("  WITH TOOLS: ", end="", flush=True)
        start = time.time()
        try:
            resp_tools = await ask_asha(question, ASHA_TOOLS_FULL)
            answer_tools = resp_tools.result[:2000]
            dur = time.time() - start
            present, total = await judge_answer(question, expected_facts, answer_tools)
            results["with_tools"]["scores"].append(present / total if total else 0)
            results["with_tools"]["total_present"] += present
            results["with_tools"]["total_facts"] += total
            sources = resp_tools.sources or []
            print(f"{present}/{total} ({dur:.1f}s, {len(sources)} sources)")
        except Exception as e:
            print(f"ERROR: {e}")
            results["with_tools"]["scores"].append(0)
            results["with_tools"]["total_facts"] += len(expected_facts)

        # Without tools
        print("  NO TOOLS:   ", end="", flush=True)
        start = time.time()
        try:
            resp_plain = await ask_asha(question, ASHA_TOOLS_NONE)
            answer_plain = resp_plain.result[:2000]
            dur = time.time() - start
            present, total = await judge_answer(question, expected_facts, answer_plain)
            results["no_tools"]["scores"].append(present / total if total else 0)
            results["no_tools"]["total_present"] += present
            results["no_tools"]["total_facts"] += total
            print(f"{present}/{total} ({dur:.1f}s)")
        except Exception as e:
            print(f"ERROR: {e}")
            results["no_tools"]["scores"].append(0)
            results["no_tools"]["total_facts"] += len(expected_facts)

        print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for mode, data in results.items():
        total_p = data["total_present"]
        total_f = data["total_facts"]
        pct = (total_p / total_f * 100) if total_f else 0
        avg = sum(data["scores"]) / len(data["scores"]) * 100 if data["scores"] else 0
        label = "WITH TOOLS" if mode == "with_tools" else "NO TOOLS  "
        print(f"  {label}: {total_p}/{total_f} facts ({pct:.0f}%) | avg per question: {avg:.0f}%")

    # Delta
    wt = results["with_tools"]
    nt = results["no_tools"]
    wt_pct = (wt["total_present"] / wt["total_facts"] * 100) if wt["total_facts"] else 0
    nt_pct = (nt["total_present"] / nt["total_facts"] * 100) if nt["total_facts"] else 0
    delta = wt_pct - nt_pct
    print(f"\n  TOOL IMPACT: {delta:+.0f} percentage points")
    print(f"  (Compare: MTG Agents found +25pp with tools)")


if __name__ == "__main__":
    asyncio.run(run_eval())
