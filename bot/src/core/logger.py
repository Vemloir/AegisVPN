import logging
import sys

from .config import settings

def setup_logger():
    # JSON formatting in prod could be added here by replacing the formatter
    formatter = logging.Formatter(
        '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
    ) if settings.log_level.upper() == "INFO" else logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    
    logger = logging.getLogger()
    logger.setLevel(settings.log_level.upper())
    logger.handlers.clear()
    logger.addHandler(handler)
    
    # Silence third-party logs
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    return logger

logger = logging.getLogger(__name__)
