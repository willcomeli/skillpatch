"""Benchmark adapters for task preparation and feedback reading."""

from .base import ExecutionAdapter
from .registry import get_execution_adapter, register_execution_adapter

__all__ = ["ExecutionAdapter", "get_execution_adapter", "register_execution_adapter"]
