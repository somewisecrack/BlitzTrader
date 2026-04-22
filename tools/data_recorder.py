"""
tools/data_recorder.py - Markdown audit recorder for market data and indicators.

Writes every live-feed tick, indicator payload, and deterministic strategy
signal the agent sees into append-only Markdown files. At EOD it can copy the
daily export folder to a mounted Google Drive path or use an rclone remote if
configured.
"""
import json
import logging
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import pytz

logger = logging.getLogger("BlitzTrader.DataRecorder")
IST = pytz.timezone("Asia/Kolkata")


class DataRecorder:
    """Thread-safe Markdown recorder for feed ticks, indicators, and scanner signals."""

    FEED_FIELDS = [
        "recorded_at",
        "token",
        "symbol",
        "tradingsymbol",
        "ltp",
        "best_bid",
        "best_ask",
        "bid_qty",
        "ask_qty",
        "volume",
        "oi",
        "open",
        "high",
        "low",
        "prev_close",
        "feed_timestamp",
        "raw_json",
    ]
    SIGNAL_FIELDS = [
        "recorded_at",
        "symbol",
        "interval",
        "time",
        "signal_date",
        "signal_datetime_ist",
        "strategy",
        "direction",
        "entry_reference",
        "stop_loss",
        "target",
        "requires_volume_confirmation",
        "reason",
        "raw_json",
    ]

    def __init__(
        self,
        base_dir: Path,
        nse_tokens: dict,
        google_drive_upload_dir: str = "",
        rclone_remote: str = "",
        rclone_folder: str = "",
    ):
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._drive_dir = Path(google_drive_upload_dir).expanduser() if google_drive_upload_dir else None
        self._rclone_remote = rclone_remote.strip()
        self._rclone_folder = rclone_folder.strip().strip("/")

        # Build two lookup maps from token:
        #   token → logical symbol name  (e.g. "66691" → "NIFTY")
        #   token → futures tsym          (e.g. "66691" → "NIFTY28APR26F")
        self._token_to_symbol: dict[str, str] = {}
        self._token_to_tsym: dict[str, str] = {}
        for symbol, info in nse_tokens.items():
            tok = str(info.get("token", "") or "")
            if tok:
                self._token_to_symbol[tok] = symbol
                tsym = info.get("tsym", "")
                if tsym:
                    self._token_to_tsym[tok] = tsym

        self._lock = threading.Lock()
        self._uploaded = False
        self._date = datetime.now(IST).strftime("%Y%m%d")
        self._day_dir = self._base_dir / self._date
        self._day_dir.mkdir(parents=True, exist_ok=True)

    def update_token_map(self, nse_tokens: dict) -> None:
        """Update the token→symbol mapping (call after futures tokens are resolved)."""
        with self._lock:
            for symbol, info in nse_tokens.items():
                tok = str(info.get("token", "") or "")
                if tok:
                    self._token_to_symbol[tok] = symbol
                    tsym = info.get("tsym", "")
                    if tsym:
                        self._token_to_tsym[tok] = tsym

    @property
    def day_dir(self) -> Path:
        return self._day_dir

    def record_feed_tick(self, token: str, quote: dict) -> None:
        """Append one live-feed tick/merged quote to feed_ticks.md.

        Each record includes:
          symbol:         logical name (NIFTY / BANKNIFTY / FINNIFTY / INDIA VIX)
          tradingsymbol:  actual futures tsym (e.g. NIFTY28APR26F) — blank for index tokens
          token:          numeric Shoonya token
        """
        try:
            tok_str = str(token)
            row = {
                "recorded_at": self._now(),
                "token": token,
                "symbol": self._token_to_symbol.get(tok_str, ""),
                "tradingsymbol": self._token_to_tsym.get(tok_str, ""),
                "ltp": quote.get("ltp"),
                "best_bid": quote.get("best_bid"),
                "best_ask": quote.get("best_ask"),
                "bid_qty": quote.get("bid_qty"),
                "ask_qty": quote.get("ask_qty"),
                "volume": quote.get("volume"),
                "oi": quote.get("oi"),
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "prev_close": quote.get("prev_close"),
                "feed_timestamp": quote.get("timestamp"),
            }
            self._append_markdown_record(
                self._day_dir / "feed_ticks.md",
                "Feed Ticks",
                "Feed Tick",
                row,
                raw_payload=quote,
            )
        except Exception:
            logger.exception("Failed to record feed tick")

    def record_indicators(self, symbol: str, interval: str, indicators: dict) -> None:
        """Append one indicator snapshot to indicators.md."""
        try:
            flat = {
                key: value for key, value in indicators.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }
            row = {
                "recorded_at": self._now(),
                "symbol": symbol,
                "interval": interval,
                **flat,
            }
            self._append_markdown_record(
                self._day_dir / "indicators.md",
                "Indicators",
                f"Indicator Snapshot: {symbol} {interval}m",
                row,
                raw_payload=indicators,
            )
        except Exception:
            logger.exception("Failed to record indicators")

    def record_strategy_signals(self, result: dict) -> None:
        """Append each emitted deterministic strategy signal to strategy_signals.md."""
        try:
            for signal in result.get("signals", []):
                row = {
                    "recorded_at": self._now(),
                    "symbol": signal.get("symbol"),
                    "interval": signal.get("interval"),
                    "time": signal.get("time"),
                    "signal_date": signal.get("signal_date"),
                    "signal_datetime_ist": signal.get("signal_datetime_ist"),
                    "strategy": signal.get("strategy"),
                    "direction": signal.get("direction"),
                    "entry_reference": signal.get("entry_reference"),
                    "stop_loss": signal.get("stop_loss"),
                    "target": signal.get("target"),
                    "requires_volume_confirmation": signal.get("requires_volume_confirmation"),
                    "reason": signal.get("reason"),
                }
                self._append_markdown_record(
                    self._day_dir / "strategy_signals.md",
                    "Strategy Signals",
                    f"Strategy Signal: {signal.get('strategy', 'unknown')}",
                    row,
                    raw_payload=signal,
                )
        except Exception:
            logger.exception("Failed to record strategy signals")

    def finalize_and_upload(self) -> dict:
        """
        Copy the daily export folder to Google Drive via configured destination.

        Supported destinations:
        - GOOGLE_DRIVE_UPLOAD_DIR=/path/to/mounted/GoogleDrive/folder
        - RCLONE_REMOTE=gdrive and optional RCLONE_FOLDER=BlitzTrader
        """
        with self._lock:
            if self._uploaded:
                return {"status": "already_uploaded", "local_dir": str(self._day_dir)}

        result = {"status": "no_destination_configured", "local_dir": str(self._day_dir)}
        if self._drive_dir:
            dest = self._drive_dir / "BlitzTrader" / self._date
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(self._day_dir, dest)
            result = {"status": "uploaded", "method": "copy", "destination": str(dest), "local_dir": str(self._day_dir)}
            with self._lock:
                self._uploaded = True
            logger.info(f"Markdown export copied to Google Drive folder: {dest}")
            return result

        if self._rclone_remote:
            remote_path = f"{self._rclone_remote}:"
            if self._rclone_folder:
                remote_path += f"{self._rclone_folder}/"
            remote_path += f"BlitzTrader/{self._date}"
            subprocess.run(["rclone", "copy", str(self._day_dir), remote_path], check=True)
            result = {"status": "uploaded", "method": "rclone", "destination": remote_path, "local_dir": str(self._day_dir)}
            with self._lock:
                self._uploaded = True
            logger.info(f"Markdown export uploaded via rclone: {remote_path}")
            return result

        with self._lock:
            self._uploaded = True
        logger.warning("Markdown export not uploaded: configure GOOGLE_DRIVE_UPLOAD_DIR or RCLONE_REMOTE")
        return result

    def _append_markdown_record(
        self,
        path: Path,
        document_title: str,
        record_title: str,
        fields: dict,
        raw_payload: dict | None = None,
    ) -> None:
        with self._lock:
            needs_title = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8") as f:
                if needs_title:
                    f.write(f"# {document_title} - {self._date}\n\n")
                    f.write("Append-only trading audit generated by BlitzTrader.\n\n")

                recorded_at = fields.get("recorded_at") or self._now()
                f.write(f"## {recorded_at} - {record_title}\n\n")
                f.write("| Field | Value |\n")
                f.write("|---|---|\n")
                for key, value in fields.items():
                    f.write(f"| {self._md(key)} | {self._md(value)} |\n")
                if raw_payload is not None:
                    f.write("\n<details><summary>Raw JSON</summary>\n\n")
                    f.write("```json\n")
                    f.write(self._json(raw_payload))
                    f.write("\n```\n\n</details>\n")
                f.write("\n")

    @staticmethod
    def _now() -> str:
        return datetime.now(IST).isoformat()

    @staticmethod
    def _json(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _md(value) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            text = str(value)
        return text.replace("|", "\\|").replace("\n", "<br>")
