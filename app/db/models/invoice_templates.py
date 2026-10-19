"""Invoice templates: the layout, logo, bank details and footer an invoice is printed with.

A practice has one default template; a client may choose another (``clients.invoice_template_id``).
The PDF still draws every invoice with the practice letterhead and footer: reading the template
there is the billing module's job.
"""

from sqlalchemy import Boolean, CheckConstraint, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, SoftDeleteMixin, TimestampMixin

INVOICE_LAYOUTS = ("standard", "detailed", "summary")
TEMPLATE_STATUSES = ("active", "inactive")


class InvoiceTemplate(Base, IdMixin, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "invoice_templates"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_invoice_templates_organization_id_id"),
        CheckConstraint("length(name) BETWEEN 1 AND 80", name="name_length"),
        CheckConstraint("layout IN ('standard', 'detailed', 'summary')", name="layout"),
        CheckConstraint("status IN ('active', 'inactive')", name="status"),
        CheckConstraint(
            "footer_text IS NULL OR length(footer_text) BETWEEN 1 AND 1000",
            name="footer_text_length",
        ),
        CheckConstraint("NOT is_default OR status = 'active'", name="default_is_active"),
        Index(
            "uq_invoice_templates_organization_id_name_lower",
            "organization_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # At most one default template per practice.
        Index(
            "uq_invoice_templates_organization_id_default",
            "organization_id",
            unique=True,
            postgresql_where=text("is_default AND deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    layout: Mapped[str] = mapped_column(Text, nullable=False, server_default="standard")
    show_logo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    show_bank_details: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    footer_text: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")


__all__ = ["INVOICE_LAYOUTS", "TEMPLATE_STATUSES", "InvoiceTemplate"]
