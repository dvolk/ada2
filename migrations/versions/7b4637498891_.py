"""empty message

Revision ID: 7b4637498891
Revises: 8e73e5f1123e
Create Date: 2023-07-28 20:31:59.136500

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7b4637498891'
down_revision = '8e73e5f1123e'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('ssh_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('private_key', sa.Text(), nullable=True),
    sa.Column('public_key', sa.Text(), nullable=True),
    sa.Column('authorized_keys', sa.Text(), nullable=True),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('ssh_keys')
    # ### end Alembic commands ###
