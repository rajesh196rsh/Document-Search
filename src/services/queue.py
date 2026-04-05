import json
import logging

import aio_pika

from src.config.settings import settings

logger = logging.getLogger(__name__)

_connection: aio_pika.abc.AbstractRobustConnection | None = None
_channel: aio_pika.abc.AbstractChannel | None = None

EXCHANGE_NAME = "doc.events"
INDEX_QUEUE = "indexing"
DELETION_QUEUE = "deletion"
DLX_EXCHANGE = "doc.events.dlx"
DLQ_QUEUE = "dead_letter"


async def get_connection() -> aio_pika.abc.AbstractRobustConnection:
    global _connection
    if _connection is None or _connection.is_closed:
        _connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    return _connection


async def get_channel() -> aio_pika.abc.AbstractChannel:
    global _channel
    conn = await get_connection()
    if _channel is None or _channel.is_closed:
        _channel = await conn.channel()
        await _channel.set_qos(prefetch_count=10)
    return _channel


async def setup_topology() -> None:
    """Declare exchanges, queues, and bindings. Safe to call multiple times."""
    channel = await get_channel()

    # Dead letter exchange + queue
    dlx = await channel.declare_exchange(DLX_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True)
    dlq = await channel.declare_queue(DLQ_QUEUE, durable=True)
    await dlq.bind(dlx)

    # Main topic exchange
    exchange = await channel.declare_exchange(EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True)

    # Indexing queue with DLX
    index_q = await channel.declare_queue(
        INDEX_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-message-ttl": 300_000,  # 5 min TTL for stuck messages
        },
    )
    await index_q.bind(exchange, routing_key="doc.index")

    # Deletion queue with DLX
    delete_q = await channel.declare_queue(
        DELETION_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DLX_EXCHANGE,
        },
    )
    await delete_q.bind(exchange, routing_key="doc.delete")

    logger.info("RabbitMQ topology set up: exchanges, queues, and bindings ready")


async def publish_index_event(tenant_id: str, document_id: str, payload: dict) -> None:
    channel = await get_channel()
    exchange = await channel.get_exchange(EXCHANGE_NAME)

    message_body = {
        "event_type": "document.index",
        "tenant_id": tenant_id,
        "document_id": document_id,
        "payload": payload,
    }

    await exchange.publish(
        aio_pika.Message(
            body=json.dumps(message_body, default=str).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key="doc.index",
    )
    logger.info(f"Published index event for doc {document_id}")


async def publish_delete_event(tenant_id: str, document_id: str) -> None:
    channel = await get_channel()
    exchange = await channel.get_exchange(EXCHANGE_NAME)

    message_body = {
        "event_type": "document.delete",
        "tenant_id": tenant_id,
        "document_id": document_id,
    }

    await exchange.publish(
        aio_pika.Message(
            body=json.dumps(message_body).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key="doc.delete",
    )
    logger.info(f"Published delete event for doc {document_id}")


async def close_connection() -> None:
    global _connection, _channel
    if _channel and not _channel.is_closed:
        await _channel.close()
    if _connection and not _connection.is_closed:
        await _connection.close()
    _channel = None
    _connection = None


async def check_health() -> dict:
    try:
        conn = await get_connection()
        if conn and not conn.is_closed:
            return {"status": "healthy"}
        return {"status": "unhealthy", "error": "connection closed"}
    except Exception as e:
        logger.error(f"RabbitMQ health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}
