"""Migration operations that only touch the database on PostgreSQL.

The migration state is still updated everywhere, so `makemigrations --check` stays clean
on SQLite while the GIN index only exists where it is supported.
"""
from django.db import migrations


def _is_postgres(schema_editor) -> bool:
    return schema_editor.connection.vendor == "postgresql"


class PostgresOnlyAddIndex(migrations.AddIndex):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if _is_postgres(schema_editor):
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if _is_postgres(schema_editor):
            super().database_backwards(app_label, schema_editor, from_state, to_state)

    def deconstruct(self):
        name, args, kwargs = super().deconstruct()
        return "documents.db_operations.PostgresOnlyAddIndex", args, kwargs
