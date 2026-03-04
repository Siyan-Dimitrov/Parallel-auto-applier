import logging
import sys

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

console = Console(theme=Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
}))


def setup_logging(level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    """Configure logging with Rich handler for console and optional file output."""
    logger = logging.getLogger("appbot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    # Rich console handler
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
    )
    rich_handler.setLevel(logging.DEBUG)
    logger.addHandler(rich_handler)

    # File handler
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    """Get the application logger."""
    return logging.getLogger("appbot")
