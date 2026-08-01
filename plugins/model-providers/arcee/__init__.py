"""Arcee AI provider profile."""

from opencodon.providers import register_provider
from opencodon.providers.base import ProviderProfile

arcee = ProviderProfile(
    name="arcee",
    aliases=("arcee-ai", "arceeai"),
    env_vars=("ARCEEAI_API_KEY",),
    base_url="https://api.arcee.ai/api/v1",
)

register_provider(arcee)
