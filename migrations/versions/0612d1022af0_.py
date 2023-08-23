"""empty message

Revision ID: 0612d1022af0
Revises: 8e186b4a4a0c
Create Date: 2023-08-23 14:59:15.445867

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0612d1022af0'
down_revision = '8e186b4a4a0c'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('group_pre_approved_users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['group.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('group_pre_approved_users')
    # ### end Alembic commands ###