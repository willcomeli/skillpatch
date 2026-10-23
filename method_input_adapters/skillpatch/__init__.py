"""SkillPatch offline diagnosis inputs, independent of model calls and execution."""

from .adapter.base import DiagnosisAdapter
from .loader import build_diagnosis_context
from .models import ContextItem, DiagnosisContext
from .registry import get_diagnosis_adapter, register_diagnosis_adapter

__all__ = ["DiagnosisAdapter", "DiagnosisContext", "ContextItem",
           "build_diagnosis_context",
           "get_diagnosis_adapter", "register_diagnosis_adapter"]
