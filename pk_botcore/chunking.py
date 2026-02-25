"""Message chunking for Discord's character limit."""


def chunk_message(text: str, max_length: int = 2000) -> list[str]:
    """
    Split a message into chunks that fit Discord's limit.

    Structure-aware: avoids breaking inside code blocks, lists, or YAML.
    Prefers breaking at paragraph boundaries (blank lines).

    Args:
        text: The message text to split
        max_length: Maximum length per chunk (default 2000 for Discord)

    Returns:
        List of message chunks
    """
    if len(text) <= max_length:
        return [text]

    blocks = _parse_blocks(text)
    chunks = []
    current_chunk = ""

    for block in blocks:
        block_text = block + "\n"

        if len(current_chunk) + len(block_text) <= max_length:
            current_chunk += block_text
        else:
            if current_chunk.strip():
                chunks.append(current_chunk.rstrip())
                current_chunk = ""

            if len(block_text) > max_length:
                forced = _force_split(block, max_length)
                chunks.extend(forced[:-1])
                current_chunk = forced[-1] + "\n" if forced else ""
            else:
                current_chunk = block_text

    if current_chunk.strip():
        chunks.append(current_chunk.rstrip())

    return chunks


def _parse_blocks(text: str) -> list[str]:
    """
    Parse text into logical blocks that should stay together.

    Blocks: code fences, contiguous list items, paragraphs.
    """
    lines = text.split("\n")
    blocks = []
    current_block = []
    in_code_fence = False
    in_list = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code_fence:
                current_block.append(line)
                blocks.append("\n".join(current_block))
                current_block = []
                in_code_fence = False
            else:
                if current_block:
                    blocks.append("\n".join(current_block))
                current_block = [line]
                in_code_fence = True
            continue

        if in_code_fence:
            current_block.append(line)
            continue

        is_list_item = bool(
            stripped.startswith(("- ", "* ", "+ ")) or
            (len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".) ")
        )
        is_list_continuation = in_list and line.startswith(("  ", "\t"))

        if is_list_item or is_list_continuation:
            if not in_list and current_block:
                blocks.append("\n".join(current_block))
                current_block = []
            in_list = True
            current_block.append(line)
            continue

        if not stripped:
            if in_list:
                if current_block:
                    blocks.append("\n".join(current_block))
                    current_block = []
                in_list = False
            elif current_block:
                blocks.append("\n".join(current_block))
                current_block = []
            blocks.append("")
            continue

        if in_list:
            if current_block:
                blocks.append("\n".join(current_block))
                current_block = []
            in_list = False

        current_block.append(line)

    if current_block:
        blocks.append("\n".join(current_block))

    return blocks


def _force_split(text: str, max_length: int) -> list[str]:
    """Force-split oversized block, preferring line boundaries."""
    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 <= max_length:
            current += line + "\n"
        else:
            if current:
                chunks.append(current.rstrip())
            if len(line) > max_length:
                while line:
                    chunks.append(line[:max_length])
                    line = line[max_length:]
                current = ""
            else:
                current = line + "\n"

    if current:
        chunks.append(current.rstrip())

    return chunks
