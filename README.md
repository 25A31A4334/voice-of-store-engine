# R5 — Voice-of-Store Engine

**Team ID:** 000273

## What it does
An AI-powered retail feedback analysis platform that helps identify, classify, prioritize, and monitor customer issues across stores. It includes customer feedback workflows, store management features, dashboards, and emerging-issue detection.

## How to run
1. Install Python and the dependencies:
   `pip install -r FULL_CODE/requirements.txt`
2. Configure environment variables using `FULL_CODE/.env.example`.
3. Ensure the required input data files are available in the root `data/` directory.
4. Start the application:
   `python -m FULL_CODE.app`
5. Open the local address displayed by the application.

## How to run tests
`python -m pytest FULL_CODE -q`

## Architecture
- FastAPI application backend
- HTML, CSS, and JavaScript frontend
- Classification, severity, actionability, and emerging-issue detection modules
- Database and Supabase integration modules
- Role-based authentication and store access controls

## AI usage
AI/ML components support feedback classification, sentiment analysis, severity assessment, actionability assessment, and emerging-issue detection.

## Pre-built and third-party code
Third-party dependencies are listed in `FULL_CODE/requirements.txt`.

## Limitations
Application functionality requiring external services depends on valid environment configuration and service availability. Required input datasets must be available locally.
