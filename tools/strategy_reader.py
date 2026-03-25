"""
tools/strategy_reader.py — Strategy docs reader for BlitzTrader.

Reads from the master trading library and any NSE-specific strategy docs.
"""
import logging
from pathlib import Path

logger = logging.getLogger("BlitzTrader.StrategyReader")


class StrategyReader:
    """
    Reads strategy documents for Claude to understand trading rules.
    """

    def __init__(self, master_file: Path, strategies_dir: Path):
        """
        :param master_file: Path to master_trading_library.md
        :param strategies_dir: Path to BlitzTrader/strategies/ for NSE-specific docs
        """
        self._master_file = master_file
        self._strategies_dir = strategies_dir
        self._strategies_dir.mkdir(exist_ok=True)

    def get_strategy_docs(self) -> dict:
        """
        Read all strategy documents and return their contents.

        Reads:
        1. master_trading_library.md (all formalized strategies)
        2. Any .md files in BlitzTrader/strategies/ (NSE-specific refinements)

        :returns: {master_library: str, nse_strategies: [{name, content}], total_chars}
        """
        result = {
            "master_library": "",
            "nse_strategies": [],
            "total_chars": 0,
        }

        # Read master library
        if self._master_file.exists():
            try:
                content = self._master_file.read_text(encoding="utf-8")
                # Truncate if too large for context window
                if len(content) > 50_000:
                    content = content[:50_000] + "\n\n... [TRUNCATED — full file is larger]"
                result["master_library"] = content
                result["total_chars"] += len(content)
                logger.info(f"Loaded master library: {len(content)} chars")
            except OSError as e:
                logger.error(f"Failed to read master library: {e}")
                result["master_library"] = f"Error reading file: {e}"
        else:
            result["master_library"] = "Master trading library not found."
            logger.warning(f"Master library not found at {self._master_file}")

        # Read NSE-specific strategy docs (skip master file to avoid double-loading)
        if self._strategies_dir.exists():
            for md_file in sorted(self._strategies_dir.glob("*.md")):
                if md_file.resolve() == self._master_file.resolve():
                    continue
                try:
                    content = md_file.read_text(encoding="utf-8")
                    result["nse_strategies"].append({
                        "name": md_file.stem,
                        "content": content,
                    })
                    result["total_chars"] += len(content)
                    logger.info(f"Loaded NSE strategy: {md_file.name}")
                except OSError:
                    logger.exception(f"Failed to read {md_file}")

        return result
