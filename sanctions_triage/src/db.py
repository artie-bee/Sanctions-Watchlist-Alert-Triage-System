"""
Connection helpers for DynamoDB Local + sanctions.db SQLite.

Credentials match alert_intake.py (aws_access_key_id='dummy') so we
land in the same DynamoDB Local namespace where the seed data lives.
"""
import os
import sqlite3
from pathlib import Path

import boto3

# Honour DYNAMODB_ENDPOINT/REGION from the environment (set by
# docker-compose to reach the `dynamodb` service); fall back to the
# local DynamoDB Local defaults for non-container runs.
DYNAMO_ENDPOINT = os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8001")
DYNAMO_REGION   = os.environ.get("DYNAMODB_REGION", "us-east-1")

SANCTIONS_DB = Path(__file__).resolve().parent.parent.parent / "sanctions.db"


def get_dynamodb():
    return boto3.resource(
        "dynamodb",
        endpoint_url=DYNAMO_ENDPOINT,
        region_name=DYNAMO_REGION,
        aws_access_key_id="dummy",
        aws_secret_access_key="dummy",
    )


def get_sanctions_db():
    conn = sqlite3.connect(SANCTIONS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def get_table(table_name: str):
    return get_dynamodb().Table(table_name)
