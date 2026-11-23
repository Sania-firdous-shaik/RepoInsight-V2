"""Add active Qdrant index generation without altering existing repository data."""
from alembic import op
import sqlalchemy as sa

revision = "0002_index_generation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("repositories", sa.Column("index_generation", sa.String(length=128), nullable=True))

def downgrade():
    op.drop_column("repositories", "index_generation")
