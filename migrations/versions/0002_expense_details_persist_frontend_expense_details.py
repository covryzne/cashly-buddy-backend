"""Persist frontend item details; no seeds or destructive cleanup.

Legacy lines represent a single unit with their original amount as unit price.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_expense_details"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("discount_idr", "tax_idr"):
        op.add_column(
            "expenses",
            sa.Column(column, sa.BigInteger(), nullable=False, server_default="0"),
            schema="cashly",
        )
    for name, expression in (
        ("discount_amount", "discount_idr >= 0"),
        ("tax_amount", "tax_idr >= 0"),
    ):
        op.create_check_constraint(f"ck_expenses_{name}", "expenses", expression, schema="cashly")
    op.add_column(
        "expense_items",
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        schema="cashly",
    )
    op.add_column(
        "expense_items",
        sa.Column("unit_price_idr", sa.BigInteger(), nullable=True),
        schema="cashly",
    )
    op.execute("UPDATE cashly.expense_items SET unit_price_idr = amount_idr")
    op.alter_column("expense_items", "unit_price_idr", nullable=False, schema="cashly")
    for name, expression in (
        ("quantity", "quantity > 0"),
        ("unit_price", "unit_price_idr >= 0"),
        ("line_total", "amount_idr = quantity::numeric * unit_price_idr::numeric"),
    ):
        op.create_check_constraint(
            f"ck_expense_items_{name}", "expense_items", expression, schema="cashly"
        )


def downgrade():
    # Original line/parent totals survive. Quantity and adjustments are lost.
    for name in ("line_total", "unit_price", "quantity"):
        op.drop_constraint(
            f"ck_expense_items_{name}", "expense_items", schema="cashly", type_="check"
        )
    op.drop_column("expense_items", "unit_price_idr", schema="cashly")
    op.drop_column("expense_items", "quantity", schema="cashly")
    for name in ("tax_amount", "discount_amount"):
        op.drop_constraint(f"ck_expenses_{name}", "expenses", schema="cashly", type_="check")
    op.drop_column("expenses", "tax_idr", schema="cashly")
    op.drop_column("expenses", "discount_idr", schema="cashly")
