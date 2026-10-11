
import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(Path(__file__).resolve().parent / ".env")

uri = os.getenv("MONGO_URI")
database_name = os.getenv("MONGO_DB")

if not uri or not database_name:
    raise RuntimeError("MONGO_URI or MONGO_DB is missing from .env")

client = MongoClient(uri, serverSelectionTimeoutMS=10000)

try:
    client.admin.command("ping")
    print("MongoDB Atlas connection successful!")
    print(f"Database configured: {database_name}")
except Exception as exc:
    print(f"MongoDB connection failed: {type(exc).__name__}: {exc}")
finally:
    client.close()
