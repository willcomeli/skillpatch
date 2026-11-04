"""Benchmark adapters for external backends and native environment runners."""

from .base import ExecutionAdapter
from .registry import get_execution_adapter, get_native_adapter, register_execution_adapter

__all__ = ["ExecutionAdapter", "get_execution_adapter", "get_native_adapter", "register_execution_adapter"]
