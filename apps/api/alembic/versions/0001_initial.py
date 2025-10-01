"""initial

Revision ID: 0001_initial
Revises: 
Create Date: 2025-09-23 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'app_meta',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('value', sa.String(256), nullable=True),
    )

def downgrade() -> None:
    op.drop_table('app_meta')
