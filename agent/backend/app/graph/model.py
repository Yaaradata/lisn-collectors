"""Chat model factory.

IMPORTANT — DATA GOVERNANCE, not a technical preference:
  Which provider is used is a data-governance decision. Vertex keeps prompts
  and results inside our GCP project (clariversev1). An external Anthropic API
  call sends them to a third party. Collector data includes Flipkart incident
  ids, order ids, and agent names. Confirm with the customer before anything
  but local testing uses a non-Vertex provider.

  Default MODEL_PROVIDER=vertex is the safer choice — a default, not a decision.
"""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import Settings

logger = logging.getLogger(__name__)


def build_chat_model(settings: Settings) -> BaseChatModel:
    provider = settings.model_provider
    if provider == "vertex":
        from langchain_google_vertexai import ChatVertexAI

        model = ChatVertexAI(
            model_name=settings.vertex_model,
            project=settings.gcp_project,
            location=settings.vertex_location,
            temperature=0,
            max_retries=1,
        )
        logger.info(
            "chat model provider=vertex project=%s location=%s model=%s",
            settings.gcp_project,
            settings.vertex_location,
            settings.vertex_model,
        )
        return model

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if not settings.anthropic_api_key:
            raise RuntimeError(
                "MODEL_PROVIDER=anthropic requires ANTHROPIC_API_KEY. "
                "Confirm with the customer before sending Flipkart incident/"
                "order/agent identifiers to a third-party API."
            )
        model = ChatAnthropic(
            model=settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            temperature=0,
        )
        logger.warning(
            "chat model provider=anthropic model=%s — prompts leave GCP; "
            "confirm customer approval before production use",
            settings.anthropic_model,
        )
        return model

    raise ValueError(f"unsupported MODEL_PROVIDER={provider!r}")
