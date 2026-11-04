"""Manual check: python -m scripts.try_preprocess "your question" [drug_name]"""
import asyncio
import sys

from app.planner.preprocess import preprocess
from app.schemas.request import QueryRequest


async def main():
    req = QueryRequest(query=sys.argv[1], drug_name=sys.argv[2] if len(sys.argv) > 2 else None)
    plan = await preprocess(req)
    print(plan.model_dump_json(indent=2, exclude_none=True))


asyncio.run(main())