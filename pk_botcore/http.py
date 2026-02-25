"""HTTP utilities for Discord bots."""

import asyncio
import io
import logging
from typing import Any

import aiohttp

logger = logging.getLogger('pk_botcore.http')

# Global aiohttp session for reuse across all requests
_http_session: aiohttp.ClientSession | None = None


async def get_http_session() -> aiohttp.ClientSession:
    """Get or create a shared aiohttp ClientSession."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def close_http_session() -> None:
    """Close the shared aiohttp ClientSession."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
        _http_session = None


async def fetch_json(
    url: str,
    *,
    timeout: int = 10,
    headers: dict[str, str] | None = None
) -> Any | None:
    """Fetch JSON data from a URL asynchronously.

    Args:
        url: The URL to fetch
        timeout: Request timeout in seconds
        headers: Optional request headers

    Returns:
        Parsed JSON data, or None on error
    """
    try:
        session = await get_http_session()
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            resp.raise_for_status()
            return await resp.json()
    except aiohttp.ClientError as e:
        logger.error("HTTP error fetching JSON from %s: %s", url, e)
        return None
    except asyncio.TimeoutError:
        logger.error("Timeout fetching JSON from %s", url)
        return None
    except Exception as e:
        logger.error("Unexpected error fetching JSON from %s: %s", url, e)
        return None


async def get_image_data(
    url: str,
    *,
    timeout: int = 10
) -> dict[str, Any] | None:
    """Retrieve image data from a URL asynchronously.

    Args:
        url: The image URL to fetch
        timeout: Request timeout in seconds

    Returns:
        Dict with 'content' (BytesIO) and 'filename' keys, or None on error
    """
    try:
        session = await get_http_session()
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            resp.raise_for_status()
            data = await resp.read()
        content = io.BytesIO(data)
        filename = url.rsplit("/", 1)[-1]
        return {"content": content, "filename": filename}
    except aiohttp.ClientError as e:
        logger.error("HTTP error fetching image from %s: %s", url, e)
        return None
    except asyncio.TimeoutError:
        logger.error("Timeout fetching image from %s", url)
        return None
    except Exception as e:
        logger.error("Unexpected error fetching image from %s: %s", url, e)
        return None
