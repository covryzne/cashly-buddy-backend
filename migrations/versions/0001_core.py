"""Initial Cashly core storage; Supabase-owned auth objects are reference-only.

Revision ID: 0001_core
Revises: None
Frozen PostgreSQL DDL; do not import live ORM metadata into historical revisions.
"""

from alembic import op

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE cashly.accounts (
	id UUID NOT NULL, 
	status VARCHAR(16) DEFAULT 'active' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_accounts PRIMARY KEY (id), 
	CONSTRAINT ck_accounts_status CHECK (status IN ('active','disabled'))
)""",
    """CREATE TABLE cashly.expense_categories (
	code VARCHAR(32) NOT NULL, 
	label_id VARCHAR(100) NOT NULL, 
	is_active BOOLEAN DEFAULT true NOT NULL, 
	sort_order INTEGER DEFAULT '0' NOT NULL, 
	CONSTRAINT pk_expense_categories PRIMARY KEY (code)
)""",
    """CREATE TABLE cashly.account_auth_links (
	auth_user_id UUID NOT NULL, 
	account_id UUID NOT NULL, 
	verified_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	CONSTRAINT pk_account_auth_links PRIMARY KEY (auth_user_id), 
	CONSTRAINT fk_account_auth_links_auth_user_id_users FOREIGN KEY(auth_user_id) REFERENCES auth.users (id), 
	CONSTRAINT fk_account_auth_links_account_id_accounts FOREIGN KEY(account_id) REFERENCES cashly.accounts (id)
)""",
    """CREATE INDEX ix_auth_links_account ON cashly.account_auth_links (account_id)""",
    """CREATE TABLE cashly.expenses (
	id UUID NOT NULL, 
	account_id UUID NOT NULL, 
	amount_idr BIGINT NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	category_code VARCHAR(32) NOT NULL, 
	description VARCHAR(500) NOT NULL, 
	merchant VARCHAR(200), 
	payment_method VARCHAR(20) DEFAULT 'unknown' NOT NULL, 
	source_platform VARCHAR(16) NOT NULL, 
	source_type VARCHAR(16) NOT NULL, 
	source_message_id VARCHAR(255) NOT NULL, 
	source_item_index INTEGER DEFAULT '0' NOT NULL, 
	timestamp_basis VARCHAR(20) NOT NULL, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_expenses PRIMARY KEY (id), 
	CONSTRAINT ck_expenses_positive_amount CHECK (amount_idr > 0), 
	CONSTRAINT ck_expenses_description CHECK (char_length(btrim(description)) BETWEEN 1 AND 500), 
	CONSTRAINT ck_expenses_source_item_index CHECK (source_item_index >= 0), 
	CONSTRAINT ck_expenses_message_id CHECK (char_length(source_message_id) BETWEEN 1 AND 255), 
	CONSTRAINT ck_expenses_payment_method CHECK (payment_method IN ('cash','qris','bank_transfer','debit_card','credit_card','e_wallet','unknown')), 
	CONSTRAINT ck_expenses_source_type CHECK (source_type IN ('text','voice','receipt','dashboard')), 
	CONSTRAINT ck_expenses_source_platform CHECK (source_platform IN ('whatsapp','dashboard')), 
	CONSTRAINT ck_expenses_timestamp_basis CHECK (timestamp_basis IN ('receipt','user_explicit','message_time')), 
	CONSTRAINT uq_expenses_source UNIQUE (account_id, source_platform, source_message_id, source_item_index), 
	CONSTRAINT fk_expenses_account_id_accounts FOREIGN KEY(account_id) REFERENCES cashly.accounts (id), 
	CONSTRAINT fk_expenses_category_code_expense_categories FOREIGN KEY(category_code) REFERENCES cashly.expense_categories (code)
)""",
    """CREATE INDEX ix_expenses_account_category_time ON cashly.expenses (account_id, category_code, occurred_at DESC) WHERE deleted_at IS NULL""",
    """CREATE INDEX ix_expenses_account_time ON cashly.expenses (account_id, occurred_at DESC, id DESC) WHERE deleted_at IS NULL""",
    """CREATE INDEX ix_expenses_category ON cashly.expenses (category_code)""",
    """CREATE TABLE cashly.idempotency_records (
	id UUID NOT NULL, 
	account_id UUID NOT NULL, 
	operation VARCHAR(120) NOT NULL, 
	key VARCHAR(128) NOT NULL, 
	request_fingerprint VARCHAR(64) NOT NULL, 
	response_status INTEGER NOT NULL, 
	response_body JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_idempotency_records PRIMARY KEY (id), 
	CONSTRAINT uq_idempotency_scope UNIQUE (account_id, operation, key), 
	CONSTRAINT ck_idempotency_records_response_status CHECK (response_status BETWEEN 100 AND 599), 
	CONSTRAINT ck_idempotency_records_key_length CHECK (char_length(key) BETWEEN 8 AND 128), 
	CONSTRAINT ck_idempotency_records_fingerprint CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'), 
	CONSTRAINT fk_idempotency_records_account_id_accounts FOREIGN KEY(account_id) REFERENCES cashly.accounts (id)
)""",
    """CREATE TABLE cashly.profiles (
	account_id UUID NOT NULL, 
	display_name VARCHAR(40) NOT NULL, 
	timezone VARCHAR(64) DEFAULT 'Asia/Jakarta' NOT NULL, 
	theme_preference VARCHAR(8) DEFAULT 'system' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_profiles PRIMARY KEY (account_id), 
	CONSTRAINT ck_profiles_display_name CHECK (char_length(btrim(display_name)) BETWEEN 2 AND 40), 
	CONSTRAINT ck_profiles_theme CHECK (theme_preference IN ('system','light','dark')), 
	CONSTRAINT fk_profiles_account_id_accounts FOREIGN KEY(account_id) REFERENCES cashly.accounts (id)
)""",
    """CREATE TABLE cashly.expense_items (
	id UUID NOT NULL, 
	expense_id UUID NOT NULL, 
	item_index INTEGER NOT NULL, 
	label VARCHAR(200) NOT NULL, 
	amount_idr BIGINT NOT NULL, 
	CONSTRAINT pk_expense_items PRIMARY KEY (id), 
	CONSTRAINT uq_expense_items_position UNIQUE (expense_id, item_index), 
	CONSTRAINT ck_expense_items_item_index CHECK (item_index >= 0), 
	CONSTRAINT ck_expense_items_line_amount CHECK (amount_idr >= 0), 
	CONSTRAINT ck_expense_items_label CHECK (char_length(btrim(label)) BETWEEN 1 AND 200), 
	CONSTRAINT fk_expense_items_expense_id_expenses FOREIGN KEY(expense_id) REFERENCES cashly.expenses (id)
)""",
)


def upgrade():
    for statement in DDL:
        op.execute(statement)
    _security()
    _categories()


def _security():
    op.execute("REVOKE ALL ON SCHEMA cashly FROM PUBLIC, anon, authenticated")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA cashly REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated"
    )
    tables = (
        "accounts",
        "profiles",
        "account_auth_links",
        "expense_categories",
        "expenses",
        "expense_items",
        "idempotency_records",
        "alembic_version",
    )
    for table in tables:
        op.execute(f"ALTER TABLE cashly.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON cashly.{table} FROM PUBLIC, anon, authenticated")
    account = "nullif(current_setting('cashly.account_id', true), '')::uuid"
    auth_user = "nullif(current_setting('cashly.auth_user_id', true), '')::uuid"
    for table, column in (
        ("accounts", "id"),
        ("profiles", "account_id"),
        ("expenses", "account_id"),
        ("idempotency_records", "account_id"),
    ):
        predicate = f"{column} = {account}"
        op.execute(
            f"CREATE POLICY account_scope ON cashly.{table} FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
        )
    op.execute(
        f"CREATE POLICY identity_read ON cashly.account_auth_links FOR SELECT USING (auth_user_id = {auth_user})"
    )
    for action in ("INSERT", "UPDATE", "DELETE"):
        predicate = f"account_id = {account} AND auth_user_id = {auth_user}"
        using = f"USING ({predicate})" if action != "INSERT" else ""
        check = f"WITH CHECK ({predicate})" if action != "DELETE" else ""
        op.execute(
            f"CREATE POLICY identity_{action.lower()} ON cashly.account_auth_links FOR {action} {using} {check}"
        )
    parent = f"EXISTS (SELECT 1 FROM cashly.expenses e WHERE e.id = expense_id AND e.account_id = {account} AND e.deleted_at IS NULL)"
    op.execute(
        f"CREATE POLICY parent_scope ON cashly.expense_items FOR ALL USING ({parent}) WITH CHECK ({parent})"
    )
    op.execute("CREATE POLICY reference_read ON cashly.expense_categories FOR SELECT USING (true)")
    op.execute("CREATE POLICY version_read ON cashly.alembic_version FOR SELECT USING (true)")
    op.execute("""CREATE FUNCTION cashly.set_updated_at() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END; $$""")
    op.execute("REVOKE ALL ON FUNCTION cashly.set_updated_at() FROM PUBLIC, anon, authenticated")
    for table in ("accounts", "profiles", "expenses"):
        op.execute(
            f"CREATE TRIGGER set_updated_at BEFORE UPDATE ON cashly.{table} FOR EACH ROW EXECUTE FUNCTION cashly.set_updated_at()"
        )


def _categories():
    op.execute("""INSERT INTO cashly.expense_categories(code, label_id, sort_order) VALUES
        ('food_drink', 'Makanan & Minuman', 1), ('transport', 'Transportasi', 2),
        ('groceries', 'Belanja Kebutuhan', 3), ('bills', 'Tagihan', 4),
        ('shopping', 'Belanja Lainnya', 5), ('health', 'Kesehatan', 6),
        ('entertainment', 'Hiburan', 7), ('education', 'Pendidikan', 8),
        ('other', 'Lainnya', 9)""")


def downgrade():
    op.execute("DROP POLICY version_read ON cashly.alembic_version")
    # No CASCADE, no auth/public objects, no global role deletion. Non-empty
    # tables lose data: run only on disposable dev or after a reviewed backup.
    for table in (
        "expense_items",
        "idempotency_records",
        "expenses",
        "account_auth_links",
        "profiles",
        "expense_categories",
        "accounts",
    ):
        op.drop_table(table, schema="cashly")
    op.execute("DROP FUNCTION cashly.set_updated_at()")
    # Retain schema/version bookkeeping, managed by Alembic.
