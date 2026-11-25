"""Notifications name the product as OpenPrintHQ, not the upstream project."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.core.branding import APP_NAME
from backend.app.core.database import _migrate_test_template_title
from backend.app.models.notification_template import DEFAULT_TEMPLATES
from backend.app.services.notification_service import NotificationService
from backend.tests._testdb import make_test_engine


def test_default_test_template_uses_app_name():
    test = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == "test")
    assert test["title_template"] == "{app_name} Test"
    assert not any("Bambuddy" in t["title_template"] + t["body_template"] for t in DEFAULT_TEMPLATES)


async def test_fallback_test_title(monkeypatch):
    svc = NotificationService()
    seen = {}

    async def fake_webhook(config, title, message, **kw):
        seen["title"] = title
        return True, "ok"

    monkeypatch.setattr(svc, "_send_webhook", fake_webhook)
    ok, _ = await svc.send_test_notification("webhook", {"webhook_url": "https://example.invalid/h"}, None)
    assert ok and seen["title"] == f"{APP_NAME} Test"


@pytest.fixture
async def engine():
    from backend.app.models.notification_template import NotificationTemplate

    engine = make_test_engine(echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(NotificationTemplate.__table__.create)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_migration_renames_only_the_old_default(engine):
    insert = text(
        "INSERT INTO notification_templates (event_type, name, title_template, body_template, is_default) "
        "VALUES (:et, :name, :title, 'b', true)"
    )
    async with engine.begin() as conn:
        await conn.execute(insert, {"et": "test", "name": "Test Notification", "title": "Bambuddy Test"})
        await conn.execute(insert, {"et": "print_start", "name": "Print Started", "title": "Bambuddy Test"})
    async with engine.begin() as conn:
        await _migrate_test_template_title(conn)
        await _migrate_test_template_title(conn)
    async with engine.connect() as conn:
        rows = dict((await conn.execute(text("SELECT event_type, title_template FROM notification_templates"))).all())
    assert rows == {"test": "{app_name} Test", "print_start": "Bambuddy Test"}


async def test_migration_keeps_an_edited_title(engine):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO notification_templates (event_type, name, title_template, body_template, is_default) "
                "VALUES ('test', 'Test Notification', 'My farm test', 'b', true)"
            )
        )
        await _migrate_test_template_title(conn)
    async with engine.connect() as conn:
        title = (await conn.execute(text("SELECT title_template FROM notification_templates"))).scalar_one()
    assert title == "My farm test"
