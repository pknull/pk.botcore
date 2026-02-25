"""pk.botcore - Shared infrastructure for PK Discord bots."""

from setuptools import setup, find_packages

setup(
    name="pk-botcore",
    version="0.1.0",
    description="Shared infrastructure for pk.zalgo and pk.asha Discord bots",
    author="Louis Grenzebach",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "discord.py>=2.0.0",
        "aiohttp>=3.8.0",
        "claude-agent-sdk>=0.1.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
        ],
    },
)
