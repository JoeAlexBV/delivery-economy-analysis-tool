# Delivery Economy Analysis Tool

A Python-based market intelligence pipeline designed to analyze gig economy delivery potential in the Crestwood, KY market. The system leverages Playwright for network interception, processes raw GraphQL/XHR data through a logic engine, and builds a historical data lake on AWS for long-term trend analysis.

## Features

- **Network Interception**: Captures GraphQL and JSON responses directly from delivery platforms.
- **Custom Logic Engine**: Calculates potential earnings based on price levels, surge multipliers, and tip percentages.
- **Distance Penalty**: Implements a "North Oldham" penalty ($0.65/mile) to account for long-distance delivery overhead.
- **Config-Driven**: All market assumptions are centralized in `config.json` for easy adjustment.
- **Cloud-Native Data Lake**: Converts analysis into optimized **Parquet** files and uploads them to **Amazon S3** for querying via **Amazon Athena**.
- **Persistence**: Maintains a local time-series record in **SQLite** for instant historical lookups

## Architecture Overview

The tool follows a classic "Medallion" architecture style adapted for gig-economy data:

1.  **Ingestion (Bronze)**: `scraper.py` launches a headless browser to mimic a user in Crestwood. It intercepts the raw JSON traffic from the platform's API and saves it to `market_snapshots.json`.
2.  **Transformation (Silver)**: `main.py` parses the snapshots, cleans "junk" data, and discovery-maps new restaurants. The `MarketAnalyzer` applies the mathematical model to calculate potential.
3.  **Storage & Modeling (Gold)**: 
    *   **Local**: Incremental data is logged to `market_history.db` (SQLite).
    *   **Cloud**: Implements a **Star Schema** with Fact (hourly potential) and Dimension (restaurant metadata) tables stored in Parquet format on **Amazon S3** (us-east-2).
4.  **Presentation**: Data is exposed via a denormalized SQL View (`v_crestwood_market_intelligence`) in **Amazon Athena** to feed a **Power BI** dashboard.

## Project Structure & File Locations

All files are contained within the project root. Below is the mapping of components:

### Logic & Code
- **`main.py`**: The central orchestrator. Handles CLI arguments, triggers the scraper, runs the database upserts, and manages S3 uploads.
- **`scraper.py`**: The web automation engine. Contains the Playwright logic, bot-bypass headers, and the network response listener.
- **`analyzer.py`**: The mathematical engine. Calculates the "Potential Earnings" formula based on parameters in the config.
- **`run_scraper.bat`**: A Windows batch file used by Task Scheduler to automate the 30-minute polling cycle.

### Data & Configuration
- **`config.json`**: The global configuration. Contains geolocation coordinates, AWS bucket names, and the economic variables (base pay, tip %, etc.).
- **`restaurants.json`**: The "Known Store" database. Stores restaurant IDs, names, and their price levels ($-$$$$).
- **`market_snapshots.json`**: (Temporary) Stores the raw network traffic captured during the most recent scrape.
- **`market_history.db`**: (Ignored by Git) A local SQLite database storing every analysis run for local time-series reporting.
- **`latest_analysis.parquet`**: (Temporary) The flattened representation of the latest run, prepared for S3 upload.
- **`restaurants_dim.parquet`**: The flattened dimension table synced to S3 for metadata joins.

### Troubleshooting & Metadata
- **`debug_market_view.png`**: A screenshot captured by the scraper to visually verify that the bot successfully bypassed overlays.
- **`requirements.txt`**: List of Python dependencies (Playwright, Pandas, PyArrow, Boto3).

## Setup

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Install Playwright Browsers**:
   ```bash
   playwright install chromium
   ```

3. **AWS Credentials**:
   To maintain a professional security posture, this project follows the **Principle of Least Privilege**:
   
   *   **Production/Daemon Access**: We use an **IAM User** with a scoped policy (restricted to `S3:PutObject` on the target bucket) for the unattended background service.
   *   **Developer Access (Recommended)**: For exploration and debugging, it is recommended to use the **AWS Toolkit for VS Code** integrated with **IAM Identity Center (SSO)**. This avoids long-lived access keys on your local machine.
   
   **Configuration Steps**:
   1.  Install the AWS CLI.
   2.  **Option A (Service Key)**: Run `aws configure` and provide the credentials for your restricted IAM User. (Ensure the policy includes `s3:ListAllMyBuckets` if using the VS Code AWS Toolkit).
   3.  **Option B (Modern/SSO)**: Use `aws configure sso` to link your local environment to IAM Identity Center. `boto3` will automatically detect the active SSO session.
   
   *Security Note: Never commit your `.aws/` directory or hardcode keys in any configuration files.*

4. **S3 Bucket**:
   Ensure the bucket name in `config.json` matches your created bucket in S3.

## Usage

### Live Market Polling
To trigger a browser session, scrape real-time data, and update the cloud data lake:
```bash
python main.py --live
```

### Local Analysis
To re-run the analysis logic on the existing `market_snapshots.json` without launching a browser:
```bash
python main.py
```

## Automation
The tool includes an internal **AsyncIO Scheduler** (APScheduler). To run the pipeline as a continuous background service (recommended for 24/7 market monitoring):

```bash
python main.py --live --schedule --interval 15
```
This eliminates the need for external OS-level scheduling (like Cron or Windows Task Scheduler) and manages the scraping lifecycle entirely within Python.

## Power BI Connectivity Tips
When connecting Power BI to the Athena data lake:
1. Use the **Native Amazon Athena Connector** (built-in) instead of ODBC to avoid DSN configuration errors.
2. Ensure your **S3 Staging Directory** is set to a dedicated folder (e.g., `s3://your-bucket/query-results/`) so Athena can process the Parquet files.
3. If using **DirectQuery**, your dashboard will update every time the scraper pushes a new file to S3 and you refresh the visual.
