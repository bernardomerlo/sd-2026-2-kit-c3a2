"""Simula o Gateway: publica um job e acompanha o status.
Uso: python tools/publish_test.py [redis_url]"""
import json
import sys
import time
import uuid

import redis

url = sys.argv[1] if len(sys.argv) > 1 else "redis://localhost:6379/0"
r = redis.Redis.from_url(url, decode_responses=True)
job_id = str(uuid.uuid4())
r.hset(f"job:{job_id}", mapping={"status": "pending"})
r.expire(f"job:{job_id}", 3600)
r.rpush("inference.jobs", json.dumps({
    "job_id": job_id,
    "payload": {"text": "texto de teste", "top_k": 3},
    "trace_context": {}}))
print("job_id:", job_id)
for _ in range(30):
    d = r.hgetall(f"job:{job_id}")
    print(d)
    if d.get("status") in ("completed", "failed"):
        break
    time.sleep(1)
print("TTL restante:", r.ttl(f"job:{job_id}"))
