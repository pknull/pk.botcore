# pk.botcore

Shared infrastructure for PK Discord bots (pk.zalgo and pk.asha).

## Installation

```bash
# From the bot directory (editable install)
pip install -e ../pk.botcore

# Or directly
pip install -e /home/pknull/Code/pk.botcore
```

## Modules

| Module | Purpose |
|--------|---------|
| `sessions` | User session management (persistence across messages) |
| `channels` | Channel listen mode configuration |
| `claude` | Claude Agent SDK invocation with streaming |
| `chunking` | Discord message chunking (2000 char limit) |
| `http` | Shared aiohttp session utilities |
| `embeds` | Discord embed helpers |
| `cmd_executor` | Dynamic command registry for [CMD:*] directives |

## Usage

```python
from pk_botcore import (
    # Sessions
    UserSession, load_sessions, save_sessions,
    # Channels
    ChannelConfig, MODE_MENTION, MODE_LISTEN, MODE_IGNORE,
    load_channel_config, save_channel_config,
    # Claude
    ClaudeResponse, invoke_claude, check_message_relevance,
    # Utilities
    chunk_message, make_embed,
    get_http_session, close_http_session, fetch_json,
    # Commands
    CommandRegistry, claude_command, CmdResult,
)
```

## Command Registry

Register cog methods for Claude to invoke via `[CMD:name:args]` directives:

```python
from pk_botcore import claude_command, CmdResult

class MyCog(commands.Cog):
    @claude_command("greet")
    async def cmd_greet(self, ctx, name: str) -> CmdResult:
        return CmdResult(success=True, message=f"Hello, {name}!")
```

Then in your bot startup:

```python
from pk_botcore import CommandRegistry

registry = CommandRegistry(bot)
# Call after all cogs loaded
registry.discover_commands()
```

## Requirements

- Python 3.11+
- discord.py 2.0+
- aiohttp 3.8+
- claude-agent-sdk
