"""Importing this module registers every job handler with hackeval.jobs."""

from . import plagiarism, quality, rules, sandbox, snapshot, uicheck  # noqa: F401
from .livetest import runner  # noqa: F401
