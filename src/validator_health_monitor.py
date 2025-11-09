from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bech32 import bech32_encode, convertbits

DEFAULT_BASE_URL = "https://rest.cosmos.directory/cosmoshub"
REQUEST_TIMEOUT = 15
SIGNING_INFOS_ENDPOINT = "/cosmos/slashing/v1beta1/signing_infos?pagination.limit=300"
VALIDATORS_ENDPOINT = (
    "/cosmos/staking/v1beta1/validators?status=BOND_STATUS_BONDED&pagination.limit=300"
)
DEFAULT_MISSED_BLOCKS_THRESHOLD = 500
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
STATUS_ICONS = {
    "JAILED": "❌",
    "AT_RISK": "⚠️",
    "HEALTHY": "✅",
}
STATUS_ICONS_ASCII = {
    "JAILED": "X",
    "AT_RISK": "!",
    "HEALTHY": "+",
}


@dataclass(frozen=True)
class AppConfig:
    base_url: str = DEFAULT_BASE_URL
    missed_blocks_threshold: int = DEFAULT_MISSED_BLOCKS_THRESHOLD
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS
    hide_healthy: bool = False
    max_results: int = 0
    html_output: Optional[str] = None
    html_title: str = "Cosmos Validator Health Report"


@dataclass(frozen=True)
class ReportRow:
    status: str
    icon_text: str
    icon_html: str
    moniker: str
    missed_blocks: int
    commission_display: str
    reason: str


@dataclass(frozen=True)
class ReportData:
    rows: List[ReportRow]
    status_counts: Dict[str, int]
    total_records: int
    total_shown: int
    filtered_out_healthy: bool


class ApiClientError(RuntimeError):
    """Generic error raised when the Cosmos Hub REST endpoint fails."""


def get_api_data(
    endpoint: str,
    *,
    base_url: str,
    max_retries: int,
    retry_backoff: float,
) -> Dict[str, Any]:
    """
    Execute a GET request against the given endpoint and return the JSON payload.

    Args:
        endpoint: Path relative to `base_url`, starting with "/".

    Raises:
        ApiClientError: If the request fails or the response is not valid JSON.

    Returns:
        Dictionary representing the decoded JSON payload.
    """
    if not endpoint.startswith("/"):
        raise ValueError("Endpoint must start with '/'.")

    url = f"{base_url}{endpoint}"
    attempt = 1
    while True:
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise ApiClientError(f"Failed to fetch data from {url}") from exc

            sleep_for = retry_backoff ** (attempt - 1)
            logging.warning(
                "Request to %s failed (%s). Attempt %d/%d. Waiting %.1fs before retrying.",
                url,
                exc,
                attempt,
                max_retries,
                sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1
            continue

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise ApiClientError(f"Invalid JSON response from {url}") from exc

    if not isinstance(data, dict):
        raise ApiClientError(
            f"Unexpected response type from {url}: {type(data).__name__}"
        )

    return data


def convert_pubkey_to_cons_address(pubkey_b64: str) -> str:
    """
    Convert a consensus public key (base64) into a `cosmosvalcons` bech32 address.

    Args:
        pubkey_b64: Base64-encoded public key (`consensus_pubkey.key` field).

    Returns:
        A bech32-encoded consensus address prefixed with `cosmosvalcons`.

    Raises:
        ValueError: If the base64 payload is invalid or conversion to bech32 fails.
    """
    try:
        pubkey_bytes = base64.b64decode(pubkey_b64, validate=True)
    except (base64.binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 consensus public key") from exc

    hash_digest = hashlib.sha256(pubkey_bytes).digest()
    address_bytes = hash_digest[:20]

    converted = convertbits(address_bytes, 8, 5)
    if converted is None:
        raise ValueError("Failed to convert consensus public key to bech32 format")

    return bech32_encode("cosmosvalcons", converted)


def fetch_signing_info_map(config: AppConfig) -> Dict[str, Dict[str, Any]]:
    """Fetch all signing infos and index them by consensus address."""
    payload = get_api_data(
        SIGNING_INFOS_ENDPOINT,
        base_url=config.base_url,
        max_retries=config.max_retries,
        retry_backoff=config.retry_backoff,
    )
    signing_infos = payload.get("info")

    if signing_infos is None:
        raise ApiClientError("Field 'info' is missing from the signing infos payload.")
    if not isinstance(signing_infos, list):
        raise ApiClientError("Field 'info' should be a list.")

    signing_info_map: Dict[str, Dict[str, Any]] = {}
    for entry in signing_infos:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if isinstance(address, str):
            signing_info_map[address] = entry

    return signing_info_map


def fetch_active_validators(config: AppConfig) -> List[Dict[str, Any]]:
    """Fetch the list of active validators (bonded status)."""
    payload = get_api_data(
        VALIDATORS_ENDPOINT,
        base_url=config.base_url,
        max_retries=config.max_retries,
        retry_backoff=config.retry_backoff,
    )
    validators = payload.get("validators")

    if validators is None:
        raise ApiClientError("Field 'validators' is missing from the validators payload.")
    if not isinstance(validators, list):
        raise ApiClientError("Field 'validators' should be a list.")

    return [item for item in validators if isinstance(item, dict)]


def collect_validator_records(
    signing_info_map: Dict[str, Dict[str, Any]],
    validators: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge validator metadata with signing info statistics."""
    records: List[Dict[str, Any]] = []

    for validator in validators:
        moniker = (
            validator.get("description", {}).get("moniker")
            if isinstance(validator.get("description"), dict)
            else None
        )
        moniker = moniker or validator.get("moniker") or "Unknown"

        consensus_pubkey = validator.get("consensus_pubkey") or {}
        pubkey_b64 = consensus_pubkey.get("key")
        if not isinstance(pubkey_b64, str):
            logging.warning(
                "Validator '%s' without a valid consensus_pubkey; skipping.", moniker
            )
            continue

        try:
            consensus_address = convert_pubkey_to_cons_address(pubkey_b64)
        except ValueError as exc:
            logging.warning(
                "Failed to derive consensus address for validator '%s': %s",
                moniker,
                exc,
            )
            continue

        signing_info = signing_info_map.get(consensus_address, {})
        missed_blocks_raw = signing_info.get("missed_blocks_counter", "0")
        try:
            missed_blocks = int(missed_blocks_raw)
        except (TypeError, ValueError):
            missed_blocks = 0

        commission_rates = (
            validator.get("commission", {}).get("commission_rates", {})
            if isinstance(validator.get("commission"), dict)
            else {}
        )
        commission_rate_raw = commission_rates.get("rate")
        try:
            commission_rate = float(commission_rate_raw)
        except (TypeError, ValueError):
            commission_rate = None

        records.append(
            {
                "moniker": moniker,
                "operator_address": validator.get("operator_address"),
                "consensus_address": consensus_address,
                "jailed": bool(validator.get("jailed")),
                "missed_blocks": missed_blocks,
                "commission_rate": commission_rate,
            }
        )

    return records


def determine_health_status(
    jailed: bool,
    missed_blocks: int,
    *,
    config: AppConfig,
) -> Dict[str, str]:
    """Determine the health status for an individual validator."""
    if jailed:
        return {"status": "JAILED", "reason": "Validator jailed"}
    if missed_blocks > config.missed_blocks_threshold:
        return {"status": "AT_RISK", "reason": "High missed blocks"}
    return {"status": "HEALTHY", "reason": "All checks passed"}


def resolve_status_icon(status: str) -> str:
    """Return an emoji icon if supported by the current terminal, otherwise ASCII."""
    icon = STATUS_ICONS.get(status, "")
    if not icon:
        return STATUS_ICONS_ASCII.get(status, "")

    encoding = sys.stdout.encoding or "utf-8"
    try:
        icon.encode(encoding)
        return icon
    except UnicodeEncodeError:
        return STATUS_ICONS_ASCII.get(status, "")


def safe_console_text(value: Any) -> str:
    """Return text safe to print on the current console, replacing unsupported chars."""
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def prepare_report_data(
    records: List[Dict[str, Any]],
    *,
    config: AppConfig,
) -> ReportData:
    """Organise validator records for presentation (console/HTML)."""
    total_records = len(records)

    # Sort by severity first, then by moniker for consistent output.
    status_order = {"JAILED": 0, "AT_RISK": 1, "HEALTHY": 2}

    def sort_key(entry: Dict[str, Any]) -> Any:
        health = determine_health_status(
            entry["jailed"],
            entry["missed_blocks"],
            config=config,
        )
        return (
            status_order.get(health["status"], 99),
            entry["moniker"].lower(),
        )

    rows: List[ReportRow] = []
    status_counts: Dict[str, int] = defaultdict(int)

    for record in sorted(records, key=sort_key):
        health = determine_health_status(
            record["jailed"],
            record["missed_blocks"],
            config=config,
        )
        status = health["status"]

        if config.hide_healthy and status == "HEALTHY":
            continue

        commission_rate = record["commission_rate"]
        commission_display = (
            f"{commission_rate * 100:.2f}%" if commission_rate is not None else "N/A"
        )
        missed_blocks = record["missed_blocks"]

        icon_html = STATUS_ICONS.get(status, STATUS_ICONS_ASCII.get(status, ""))
        icon_text = resolve_status_icon(status)

        rows.append(
            ReportRow(
                status=status,
                icon_text=icon_text,
                icon_html=icon_html,
                moniker=record["moniker"],
                missed_blocks=missed_blocks,
                commission_display=commission_display,
                reason=health["reason"],
            )
        )
        status_counts[status] += 1

        if config.max_results and len(rows) >= config.max_results:
            break

    filtered_out_healthy = (
        config.hide_healthy and total_records > 0 and len(rows) == 0
    )

    return ReportData(
        rows=rows,
        status_counts=dict(status_counts),
        total_records=total_records,
        total_shown=len(rows),
        filtered_out_healthy=filtered_out_healthy,
    )


def print_validator_report(report: ReportData, *, config: AppConfig) -> None:
    """Pretty-print the consolidated validator health report."""
    print("=== Validator Health Report ===")
    print(f"Missed blocks threshold: {config.missed_blocks_threshold}\n")

    if report.total_records == 0:
        print("No active validators found.")
        return

    if report.total_shown == 0:
        if report.filtered_out_healthy:
            print("No unhealthy validators found (healthy validators are hidden).")
        else:
            print("No validator records available.")
        return

    for row in report.rows:
        moniker_safe = safe_console_text(row.moniker)
        print(f"[{row.status}] {row.icon_text} Validator '{moniker_safe}'")
        if row.status == "JAILED":
            print(f"    - Status: JAILED ({row.reason})")
        elif row.status == "AT_RISK":
            print(f"    - Status: AT_RISK ({row.reason})")
        else:
            detail = (
                f" ({row.reason})"
                if row.reason and row.reason != "All checks passed"
                else ""
            )
            print(f"    - Status: HEALTHY{detail}")
        print(f"    - Missed Blocks: {row.missed_blocks}")
        print(f"    - Commission: {row.commission_display}")
        print()

    summary_parts = [
        f"{label}: {report.status_counts.get(label, 0)}"
        for label in ["JAILED", "AT_RISK", "HEALTHY"]
        if report.status_counts.get(label, 0)
    ]
    summary_suffix = "; ".join(summary_parts) if summary_parts else "no validators listed"
    print(f"Summary -> total shown: {report.total_shown}; {summary_suffix}")


def write_html_report(report: ReportData, *, config: AppConfig) -> None:
    """Render the report to a standalone HTML file."""
    if not config.html_output:
        return

    output_path = Path(config.html_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary_badges = "".join(
        f"<span class='badge {status.lower()}'>{status}: {report.status_counts.get(status, 0)}</span>"
        for status in ["JAILED", "AT_RISK", "HEALTHY"]
    )

    if report.total_records == 0:
        table_section = "<p class='empty'>No active validators found.</p>"
    elif report.total_shown == 0:
        message = (
            "No unhealthy validators found (healthy validators are hidden)."
            if report.filtered_out_healthy
            else "No validator records available."
        )
        table_section = f"<p class='empty'>{html.escape(message)}</p>"
    else:
        row_html = []
        for row in report.rows:
            status_class = row.status.lower()
            icon = html.escape(row.icon_html or row.icon_text)
            moniker = html.escape(row.moniker)
            reason = html.escape(row.reason)
            commission = html.escape(row.commission_display)
            row_html.append(
                "<tr class='status-row {cls}'>"
                "<td class='status-cell'><span class='status-icon'>{icon}</span> {status}</td>"
                "<td class='moniker'>{moniker}</td>"
                "<td class='numeric'>{missed}</td>"
                "<td class='numeric'>{commission}</td>"
                "<td class='reason'>{reason}</td>"
                "</tr>".format(
                    cls=status_class,
                    icon=icon,
                    status=row.status.replace("_", " "),
                    moniker=moniker,
                    missed=row.missed_blocks,
                    commission=commission,
                    reason=reason,
                )
            )

        table_section = (
            "<table class='report-table'>"
            "<thead>"
            "<tr>"
            "<th>Status</th>"
            "<th>Validator</th>"
            "<th>Missed Blocks</th>"
            "<th>Commission</th>"
            "<th>Notes</th>"
            "</tr>"
            "</thead>"
            "<tbody>"
            f"{''.join(row_html)}"
            "</tbody>"
            "</table>"
        )

    html_document = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(config.html_title)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {{
      color-scheme: dark light;
      --bg: #0f172a;
      --bg-panel: #1e293b;
      --text-primary: #f8fafc;
      --text-secondary: #cbd5f5;
      --border: #334155;
      --accent: #38bdf8;
      --jailed: #ef4444;
      --risk: #f97316;
      --healthy: #22c55e;
      --font: 'Segoe UI', 'Inter', system-ui, sans-serif;
    }}
    body {{
      margin: 0;
      padding: 32px;
      font-family: var(--font);
      background: radial-gradient(circle at top left, rgba(56,189,248,0.25), transparent 45%), var(--bg);
      color: var(--text-primary);
    }}
    .container {{
      max-width: 960px;
      margin: 0 auto;
    }}
    header {{
      margin-bottom: 24px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 2rem;
      letter-spacing: 0.02em;
    }}
    .meta {{
      color: var(--text-secondary);
      font-size: 0.95rem;
    }}
    .summary {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 24px 0;
    }}
    .badge {{
      padding: 6px 12px;
      border-radius: 999px;
      font-weight: 600;
      background: var(--bg-panel);
      border: 1px solid var(--border);
      color: var(--text-secondary);
    }}
    .badge.jailed {{ border-color: var(--jailed); color: var(--jailed); }}
    .badge.at_risk {{ border-color: var(--risk); color: var(--risk); }}
    .badge.healthy {{ border-color: var(--healthy); color: var(--healthy); }}
    .report-table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--bg-panel);
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 24px 48px rgba(15, 23, 42, 0.35);
    }}
    th, td {{
      padding: 14px 18px;
      border-bottom: 1px solid rgba(51, 65, 85, 0.6);
      text-align: left;
    }}
    th {{
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--text-secondary);
      background: rgba(15, 23, 42, 0.55);
    }}
    tr:last-child td {{
      border-bottom: none;
    }}
    .numeric {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .status-row.jailed {{
      background: linear-gradient(90deg, rgba(239, 68, 68, 0.15), transparent);
    }}
    .status-row.at_risk {{
      background: linear-gradient(90deg, rgba(249, 115, 22, 0.15), transparent);
    }}
    .status-row.healthy {{
      background: linear-gradient(90deg, rgba(34, 197, 94, 0.12), transparent);
    }}
    .status-icon {{
      font-size: 1.1rem;
      margin-right: 6px;
    }}
    .reason {{
      color: var(--text-secondary);
    }}
    .empty {{
      padding: 32px;
      background: var(--bg-panel);
      border-radius: 16px;
      border: 1px dashed var(--border);
      color: var(--text-secondary);
      text-align: center;
    }}
    footer {{
      margin-top: 32px;
      color: var(--text-secondary);
      font-size: 0.85rem;
      text-align: right;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>{html.escape(config.html_title)}</h1>
      <div class="meta">
        Generated at {timestamp} &middot; Base URL: {html.escape(config.base_url)}<br />
        Records processed: {report.total_records} &middot; Threshold: {config.missed_blocks_threshold} missed blocks
      </div>
      <div class="summary">
        {summary_badges}
      </div>
    </header>
    {table_section}
    <footer>
      Cosmos Validator Health Monitor &mdash; console + HTML output mode
    </footer>
  </div>
</body>
</html>
"""

    output_path.write_text(html_document, encoding="utf-8")
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validator health monitor for Cosmos-based networks.",
    )
    parser.add_argument(
        "--missed-threshold",
        type=int,
        default=DEFAULT_MISSED_BLOCKS_THRESHOLD,
        help="Missed block limit used to flag a validator as at-risk (default: %(default)s).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="Maximum number of API retry attempts (default: %(default)s).",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
        help=(
            "Exponential backoff base between retries (default: %(default)s). "
            "Sleep time follows retry_backoff^(attempt-1)."
        ),
    )
    parser.add_argument(
        "--hide-healthy",
        action="store_true",
        help="Hide healthy validators in the printed report.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="Limit the number of validators displayed (0 means show all).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level for the script (default: %(default)s).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=(
            "Override the Cosmos REST API base URL "
            "(default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--html-output",
        metavar="PATH",
        help="If provided, write a formatted HTML report to the given path.",
    )
    parser.add_argument(
        "--html-title",
        default="Cosmos Validator Health Report",
        help="Custom title for the generated HTML report (default: %(default)s).",
    )
    args = parser.parse_args()

    if args.missed_threshold < 0:
        parser.error("--missed-threshold must be an integer greater than or equal to 0.")
    if args.max_retries < 1:
        parser.error("--max-retries must be an integer greater than or equal to 1.")
    if args.retry_backoff <= 0:
        parser.error("--retry-backoff must be a positive number.")
    if args.top < 0:
        parser.error("--top must be an integer greater than or equal to 0.")

    logging.basicConfig(level=args.log_level, format="%(levelname)s: %(message)s")
    logging.info("Validator monitor started.")

    config = AppConfig(
        base_url=args.base_url,
        missed_blocks_threshold=args.missed_threshold,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        hide_healthy=args.hide_healthy,
        max_results=args.top,
        html_output=args.html_output,
        html_title=args.html_title,
    )
    logging.info("Using base URL: %s", config.base_url)

    try:
        signing_info_map = fetch_signing_info_map(config)
    except ApiClientError as exc:
        logging.error("Failed to fetch signing infos: %s", exc)
        print("\nUnable to produce the report because signing info retrieval failed.")
        return

    logging.info("Loaded %d signing info records.", len(signing_info_map))

    try:
        validators = fetch_active_validators(config)
    except ApiClientError as exc:
        logging.error("Failed to fetch validators: %s", exc)
        print("\nUnable to produce the report because validator retrieval failed.")
        return

    logging.info("Fetched %d bonded validators.", len(validators))

    records = collect_validator_records(signing_info_map, validators)
    logging.info("Prepared %d merged validator records.", len(records))
    report = prepare_report_data(records, config=config)
    logging.info("Report rows ready after filters: %d.", report.total_shown)
    print_validator_report(report, config=config)

    if config.html_output:
        try:
            write_html_report(report, config=config)
        except OSError as exc:
            logging.error("Failed to write HTML report to %s: %s", config.html_output, exc)
        else:
            logging.info("HTML report written to %s", config.html_output)


if __name__ == "__main__":
    main()

