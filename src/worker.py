"""
Index Worker — consumes messages from RabbitMQ and writes to Elasticsearch + PostgreSQL.

Run standalone: python -m src.worker
"""

import asyncio
import json
import logging

import aio_pika

from src.config.settings import settings
from src.services import database, elasticsearch, queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("worker")

MAX_RETRIES = 3


async def handle_index(message: aio_pika.abc.AbstractIncomingMessage) -> None:
    async with message.process(requeue=False):
        body = json.loads(message.body.decode())
        tenant_id = body["tenant_id"]
        doc_id = body["document_id"]
        payload = body["payload"]

        logger.info(f"Processing index event: doc={doc_id} tenant={tenant_id}")

        try:
            # Write to Elasticsearch
            await elasticsearch.index_document(tenant_id, doc_id, payload)

            # Update PG status to 'indexed'
            async with database.async_session() as session:
                await database.update_document_status(session, doc_id, "indexed")

            logger.info(f"Successfully indexed doc {doc_id}")

        except Exception as e:
            logger.error(f"Failed to index doc {doc_id}: {e}")
            # Mark as failed in PG so we can track it
            try:
                async with database.async_session() as session:
                    await database.update_document_status(session, doc_id, "failed")
            except Exception:
                pass
            raise  # Let aio_pika handle the requeue / DLQ


async def handle_delete(message: aio_pika.abc.AbstractIncomingMessage) -> None:
    async with message.process(requeue=False):
        body = json.loads(message.body.decode())
        tenant_id = body["tenant_id"]
        doc_id = body["document_id"]

        logger.info(f"Processing delete event: doc={doc_id} tenant={tenant_id}")

        try:
            await elasticsearch.delete_document(tenant_id, doc_id)
            logger.info(f"Successfully deleted doc {doc_id} from ES")
        except Exception as e:
            logger.error(f"Failed to delete doc {doc_id} from ES: {e}")
            raise


async def main():
    logger.info("Worker starting up...")

    # Initialize SQLite (idempotent — creates tables if they don't exist)
    await database.init_db()

    # Set up RabbitMQ topology (idempotent)
    await queue.setup_topology()

    channel = await queue.get_channel()

    # Consume from indexing queue
    index_queue = await channel.get_queue(queue.INDEX_QUEUE)
    await index_queue.consume(handle_index)
    logger.info(f"Consuming from '{queue.INDEX_QUEUE}' queue")

    # Consume from deletion queue
    delete_queue = await channel.get_queue(queue.DELETION_QUEUE)
    await delete_queue.consume(handle_delete)
    logger.info(f"Consuming from '{queue.DELETION_QUEUE}' queue")

    logger.info("Worker is running. Waiting for messages...")

    # Keep the worker alive
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Worker shutting down...")
        await queue.close_connection()
        await elasticsearch.close_es_client()


if __name__ == "__main__":
    asyncio.run(main())
