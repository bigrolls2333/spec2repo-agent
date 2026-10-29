"""Streams are repeatable notifications; MySQL owns the business state."""

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import ResponseError
from redis.retry import Retry


class Streams:
    def __init__(self, client: Redis, *, stream="spec2repo:dispatch", group="dispatch-simulation"):
        self.client = client
        self.stream = stream
        self.group = group
        self.dead = stream + ":rejected"

    def ensure_group(self):
        try:
            # Start at 0 so messages published before group creation are consumed.
            self.client.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if not str(exc).startswith("BUSYGROUP"):
                raise

    def publish(self, event_id, run_id, version, generation=0):
        return self.client.xadd(
            self.stream,
            {
                "event_id": event_id,
                "run_id": run_id,
                "version": str(version),
                "generation": str(generation),
            },
        )

    def read(self, consumer, *, count=1):
        self.ensure_group()
        rows = self.client.xreadgroup(self.group, consumer, {self.stream: ">"}, count=count)
        return rows[0][1] if rows else []

    def reclaim(self, consumer, *, idle_ms=30000, cursor="0-0", count=10):
        self.ensure_group()
        result = self.client.xautoclaim(
            self.stream,
            self.group,
            consumer,
            idle_ms,
            start_id=cursor,
            count=count,
        )
        # Redis 7 also returns IDs removed from the PEL because their entries vanished.
        return result[0], result[1]

    def ack(self, message_id):
        self.client.xack(self.stream, self.group, message_id)

    def reject(self, message_id, reason):
        # Do not copy untrusted payloads or credentials into the rejected stream.
        self.client.eval(
            "redis.call('XADD', KEYS[1], '*', 'message_id', ARGV[2], 'reason', ARGV[3]); "
            "return redis.call('XACK', KEYS[2], ARGV[1], ARGV[2])",
            2,
            self.dead,
            self.stream,
            self.group,
            message_id,
            reason,
        )


def redis_client(url):
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry_on_timeout=False,
        retry=Retry(NoBackoff(), 0),
        health_check_interval=30,
    )
