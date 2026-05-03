"""
finamt.agents
~~~~~~~~~~~~~~~~
Multi-agent extraction pipeline.

  rules       — rule-based extractor (no LLM, always runs first)
  agent1_text — text LLM extraction (qwen2.5:7b-instruct or llama3.1)
  agent2_vision — vision LLM extraction (qwen2.5vl:7b)
  agent3_validator — merges agent1 + agent2 outputs into one result
  pipeline    — orchestrates all steps, exposes run_pipeline()
  config      — per-agent model configuration
  prompts     — per-agent prompt templates
"""

from .agent import FinanceAgent
from .config import AgentModelConfig, AgentsConfig
from .pipeline import run_pipeline
from .prompts import RECEIPT_CATEGORIES

__all__ = ["FinanceAgent", "run_pipeline", "AgentsConfig", "AgentModelConfig", "RECEIPT_CATEGORIES"]
