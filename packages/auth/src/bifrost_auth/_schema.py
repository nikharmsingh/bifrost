"""Tables shared by every Bifrost app, on one MetaData so create_all builds them together."""

import sqlalchemy as sa

metadata = sa.MetaData()
users_table = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("github_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("login", sa.String, nullable=False),
    sa.Column("name", sa.String),
    sa.Column("avatar_url", sa.String),
    sa.Column("created_at", sa.String, nullable=False),
)
user_keys_table = sa.Table(
    "user_keys",
    metadata,
    sa.Column("user_id", sa.String, primary_key=True),
    sa.Column("provider", sa.String, primary_key=True),
    # AES-GCM ciphertext bound to (user_id, provider); the plaintext key is never stored
    sa.Column("ciphertext", sa.String, nullable=False),
    sa.Column("last4", sa.String, nullable=False),
    sa.Column("updated_at", sa.String, nullable=False),
)
