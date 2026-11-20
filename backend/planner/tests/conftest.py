import os

# agent.py creates a boto3 Lambda client at import time, which needs a region
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
