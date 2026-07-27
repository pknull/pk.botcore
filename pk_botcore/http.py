"""HTTP utilities for Discord bots."""

import asyncio
import io
import ipaddress
import logging
import os
import socket
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp

logger = logging.getLogger('pk_botcore.http')

# Global aiohttp session for reuse across all requests
_http_session: aiohttp.ClientSession | None = None
DEFAULT_MAX_IMAGE_BYTES = 7 * 1024 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


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
    timeout: int = 10,
    max_bytes: int | None = None,
    max_redirects: int = 3,
) -> dict[str, Any] | None:
    """Retrieve image data from a URL asynchronously.

    Args:
        url: The image URL to fetch
        timeout: Request timeout in seconds

    Returns:
        Dict with 'content' (BytesIO) and 'filename' keys, or None on error
    """
    if max_bytes is None:
        try:
            max_bytes = int(
                os.getenv("PK_BOTCORE_MAX_IMAGE_BYTES", str(DEFAULT_MAX_IMAGE_BYTES))
            )
        except ValueError:
            max_bytes = DEFAULT_MAX_IMAGE_BYTES
    max_bytes = max(1, max_bytes)

    try:
        current_url = url
        session = None

        for redirect_count in range(max_redirects + 1):
            approved_addresses = await asyncio.wait_for(
                _validate_public_url(current_url),
                timeout=timeout,
            )
            if session is None:
                session = await get_http_session()
            pinned_url, host_header, server_hostname = _build_pinned_request(
                current_url,
                approved_addresses[0],
            )
            async with session.get(
                pinned_url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
                headers={"Host": host_header, "Connection": "close"},
                server_hostname=server_hostname,
            ) as resp:
                _validate_peer_address(resp)

                if resp.status in REDIRECT_STATUSES:
                    if redirect_count >= max_redirects:
                        raise ValueError("too many redirects")
                    location = resp.headers.get("Location")
                    if not location:
                        raise ValueError("redirect response has no Location header")
                    current_url = urljoin(current_url, location)
                    continue

                resp.raise_for_status()
                media_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if not media_type.startswith("image/"):
                    raise ValueError(f"unexpected media type: {media_type or 'missing'}")

                content_length = resp.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise ValueError("image exceeds maximum size")
                    except ValueError as exc:
                        if str(exc) == "image exceeds maximum size":
                            raise
                        raise ValueError("invalid Content-Length header") from exc

                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("image exceeds maximum size")

                path_name = unquote(urlsplit(current_url).path.rsplit("/", 1)[-1])
                filename = os.path.basename(path_name.replace("\\", "/"))
                filename = filename.replace("\x00", "")[:255] or "image"
                return {"content": io.BytesIO(bytes(data)), "filename": filename}

        raise ValueError("too many redirects")
    except aiohttp.ClientError as e:
        logger.error("HTTP error fetching image from %s: %s", url, e)
        return None
    except asyncio.TimeoutError:
        logger.error("Timeout fetching image from %s", url)
        return None
    except Exception as e:
        logger.error("Unexpected error fetching image from %s: %s", url, e)
        return None


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def _validate_public_url(url: str) -> list[str]:
    """Return public resolved addresses for a validated HTTP(S) URL."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http and https image URLs are allowed")
    if not parsed.hostname:
        raise ValueError("image URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in image URLs are not allowed")

    hostname = parsed.hostname
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError("image URL points to a non-public address")
        return [str(literal)]

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    if not addresses:
        raise ValueError("image hostname did not resolve")
    if any(not _is_public_ip(sockaddr[0]) for *_, sockaddr in addresses):
        raise ValueError("image hostname resolves to a non-public address")
    return list(dict.fromkeys(sockaddr[0] for *_, sockaddr in addresses))


def _build_pinned_request(url: str, address: str) -> tuple[str, str, str | None]:
    """Replace the URL host with a validated IP whilst preserving Host and SNI."""
    parsed = urlsplit(url)
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    ip_value = ipaddress.ip_address(address)
    pinned_host = f"[{ip_value}]" if ip_value.version == 6 else str(ip_value)
    pinned_netloc = pinned_host if port == default_port else f"{pinned_host}:{port}"

    original_host = parsed.hostname or ""
    host_header = original_host if port == default_port else f"{original_host}:{port}"
    pinned_url = urlunsplit(
        (parsed.scheme, pinned_netloc, parsed.path, parsed.query, parsed.fragment)
    )
    server_hostname = original_host if parsed.scheme == "https" else None
    return pinned_url, host_header, server_hostname


def _validate_peer_address(response: aiohttp.ClientResponse) -> None:
    """Check the connected peer when aiohttp exposes it, limiting DNS rebinding."""
    connection = getattr(response, "connection", None)
    transport = getattr(connection, "transport", None)
    if transport is None:
        return
    peer = transport.get_extra_info("peername")
    if peer and not _is_public_ip(peer[0]):
        raise ValueError("image connection reached a non-public address")
