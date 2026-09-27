"""Phase 2: continuous aggregate topic_daily_stats

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 连续聚合不能在事务内创建
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS topic_daily_stats
            WITH (timescaledb.continuous) AS
            SELECT topic_id,
                   region,
                   time_bucket(INTERVAL '1 day', hour) AS day,
                   sum(mentions)       AS mentions,
                   sum(new_items)      AS new_items,
                   sum(engagement)     AS engagement,
                   max(sources_count)  AS sources_count,
                   max(z)              AS max_z,
                   max(score)          AS max_score
            FROM topic_hourly_stats
            GROUP BY topic_id, region, time_bucket(INTERVAL '1 day', hour)
            WITH NO DATA
            """
        )
        op.execute(
            """
            SELECT add_continuous_aggregate_policy('topic_daily_stats',
                start_offset => INTERVAL '30 days',
                end_offset => INTERVAL '1 hour',
                schedule_interval => INTERVAL '1 hour',
                if_not_exists => true)
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS topic_daily_stats")
