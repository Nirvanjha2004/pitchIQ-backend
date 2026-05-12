"""Logging configuration"""

import logging
import sys
from typing import Optional

class Logger:
    """Application logger"""
    
    _instance: Optional[logging.Logger] = None
    
    @staticmethod
    def get_logger(name: str = "pitchiq") -> logging.Logger:
        """Get or create logger"""
        if Logger._instance is None:
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            
            # Console handler
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
            Logger._instance = logger
        
        return Logger._instance

def get_logger(name: str = "pitchiq") -> logging.Logger:
    """Convenience function"""
    return Logger.get_logger(name)
