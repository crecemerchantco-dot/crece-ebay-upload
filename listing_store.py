"""Durable intent/outcome journal. No OAuth credentials are stored here."""
import json
from contextlib import contextmanager
import psycopg
from psycopg.types.json import Jsonb


class StoreError(Exception):
    pass


class Journal:
    def __init__(self, url):
        self.url = url

    @contextmanager
    def locked(self, sku):
        try:
            with psycopg.connect(self.url, autocommit=True, connect_timeout=5) as db:
                db.execute('''CREATE TABLE IF NOT EXISTS crece_listing_intents (
                    sku text PRIMARY KEY, record jsonb NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now())''')
                # Session lock spans durable, individually committed writes and the
                # remote call. A crashed process releases it; SUBMITTING stays saved.
                acquired = db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 0))', (sku,)).fetchone()[0]
                if not acquired:
                    raise StoreError('A request for this item is already running. Wait before checking again.')
                try:
                    yield Entry(db, sku)
                finally:
                    db.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 0))', (sku,))
        except psycopg.Error:
            raise StoreError('The saved listing journal is unavailable. No further submission will be sent.') from None


class Entry:
    def __init__(self, db, sku):
        self.db, self.sku = db, sku

    def get(self):
        row = self.db.execute('SELECT record FROM crece_listing_intents WHERE sku=%s', (self.sku,)).fetchone()
        return row[0] if row else None

    def save(self, record):
        self.db.execute('''INSERT INTO crece_listing_intents(sku, record) VALUES (%s,%s)
          ON CONFLICT(sku) DO UPDATE SET record=excluded.record,updated_at=now()''', (self.sku, Jsonb(record)))
