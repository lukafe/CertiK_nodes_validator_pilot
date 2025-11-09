# Cosmos Validator Health Monitor

Single-file Python utility that retrieves validator metadata and signing statistics from a Cosmos SDK REST API, merges both data sources, and prints a concise health report to the terminal.

The default configuration targets the Cosmos Hub mainnet through Cosmos Directory (`https://rest.cosmos.directory/cosmoshub`), but any compatible REST endpoint can be provided via CLI flags.

## Requirements
- Python 3.10 or newer
- Dependencies listed in `requirements.txt`

Install them with:

```bash
pip install -r requirements.txt
```

## Quick Start
Run the monitor with default settings:

```bash
python src/validator_health_monitor.py
```

Sample output (trimmed):

```
INFO: Validator monitor started.
INFO: Using base URL: https://rest.cosmos.directory/cosmoshub
=== Validator Health Report ===
Missed blocks threshold: 500

[AT_RISK] ! Validator 'ExampleNode'
    - Status: AT_RISK (High missed blocks)
    - Missed Blocks: 547
    - Commission: 5.00%
```

> The script automatically falls back to ASCII icons when the active terminal cannot render emoji (common on Windows consoles that use the CP1252 code page).

## Useful CLI Flags
- `--base-url <url>`: point the monitor to a different Cosmos REST endpoint (e.g. another Cosmos Hub node, a Theta testnet node, or a local sentry).
- `--missed-threshold <int>`: change the block-miss limit that classifies validators as `AT_RISK` (default: `500`).
- `--hide-healthy`: hide healthy validators and print only jailed / at-risk ones.
- `--max-retries <int>` and `--retry-backoff <float>`: tune the retry strategy for REST calls (exponential backoff).
- `--top <int>`: limit the number of validators displayed; helpful for dashboards or quick checks.
- `--html-output <path>`: additionally save the report as a styled HTML file (UTF‑8, dark/light friendly).
- `--html-title <str>`: override the HTML `<title>` when exporting to a file.
- `--log-level DEBUG`: surface verbose logging for troubleshooting.

Example (focus on non-healthy validators while targeting a custom endpoint):

```bash
python src/validator_health_monitor.py \
  --base-url https://rest.cosmos.directory/theta-testnet-001 \
  --missed-threshold 300 \
  --top 5 \
  --hide-healthy \
  --html-output reports/theta-at-risk.html \
  --log-level DEBUG
```

Every report ends with a status summary (counts by `JAILED`, `AT_RISK`, `HEALTHY`) for the validators actually shown.

## HTML Output
- Styled dashboard-like layout with badges, gradient background, and status-aware row highlighting.
- Automatically creates parent directories for the target path (e.g. `reports/health.html`).
- Includes generation timestamp, REST base URL, threshold, and per-status counts.
- Emoji are preserved in the HTML regardless of console encoding (ASCII fallback is still used for terminal output).

## Testing Against Alternates
Some public nodes rotate or go offline. If the default base URL fails, try one of the following:

| Network             | Sample REST base URL                                     | Notes                                  |
|---------------------|-----------------------------------------------------------|----------------------------------------|
| Cosmos Hub mainnet  | `https://rest.cosmos.directory/cosmoshub`                | Default shipped with the script        |
| Cosmos Hub testnet  | `https://rest.cosmos.directory/theta-testnet-001`        | Community-maintained via Cosmos Dir.   |
| Custom node         | `https://<your-node-host>:1317`                           | Replace with your own validator/sentry |

Use the `--base-url` flag to switch between them without editing the source code.

## Development Notes
- The monitor merges `/cosmos/staking/v1beta1/validators` with `/cosmos/slashing/v1beta1/signing_infos` by deriving consensus addresses from consensus pubkeys (SHA-256 + bech32).
- All logging, messaging, and comments are in English for easier collaboration.
- Feel free to wrap the script with systemd/cron for periodic checks or feed the output into alerting pipelines.

