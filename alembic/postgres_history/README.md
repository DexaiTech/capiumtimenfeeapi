# PostgreSQL migration history (archived, not run)

Migrations `001`-`081` built the schema on PostgreSQL 18 until the move to MySQL 8.4
(`docs/implementation-plans/AZURE-MYSQL.md`). Alembic no longer reads this folder: the MySQL
schema starts at `alembic/versions/100_mysql_baseline.py`, which is equivalent to `081` apart
from the differences listed in that file.

Kept as the reference for what each table and rule means, and as the source schema for the
data-migration ticket that copies a live PostgreSQL database into MySQL.
