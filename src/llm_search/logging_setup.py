"""Shared colorized logging configuration.

Provides a single setup function that configures colorized console output
for all loggers in the llm_search namespace. Call once at application startup;
all child loggers (providers, server, etc.) inherit the configuration.
"""

import logging
import sys

LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}


def setup_colorized_logging(verbose=False):
    """Configure colorized stderr logging for the llm_search logger hierarchy."""
    level = logging.DEBUG if verbose else logging.INFO

    old_factory = logging.getLogRecordFactory()

    def record_factory(*factory_args, **factory_kwargs):
        record = old_factory(*factory_args, **factory_kwargs)
        record.levelname_color = LEVEL_COLORS.get(record.levelname, "")
        return record

    logging.setLogRecordFactory(record_factory)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "\033[90m%(asctime)s\033[0m "
        "%(levelname_color)s%(levelname)-8s\033[0m "
        "%(message)s",
        datefmt="%H:%M:%S",
    ))

    root_logger = logging.getLogger("llm_search")
    root_logger.setLevel(level)
    if not root_logger.handlers:
        root_logger.addHandler(handler)
