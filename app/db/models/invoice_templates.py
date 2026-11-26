"""Invoice templates: the layout, logo, bank details and footer an invoice is printed with.

A practice has one default template; a client may choose another (``clients.invoice_template_id``).
The PDF reads the template: its layout, letterhead, bank details, footer, accent colour and
watermark.
"""

from sqlalchemy import Boolean, CheckConstraint, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, SoftDeleteMixin, TimestampMixin

INVOICE_LAYOUTS = ("standard", "detailed", "summary")
TEMPLATE_STATUSES = ("active", "inactive")


class InvoiceTemplate(Base, IdMixin, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "invoice_templates"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_invoice_templates_organization_id_id"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="name_length"),
        CheckConstraint("layout IN ('standard', 'detailed', 'summary')", name="layout"),
        CheckConstraint("status IN ('active', 'inactive')", name="status"),
        CheckConstraint(
            "footer_text IS NULL OR char_length(footer_text) BETWEEN 1 AND 1000",
            name="footer_text_length",
        ),
        CheckConstraint(
            "accent_color IS NULL OR REGEXP_LIKE(accent_color, '^#[0-9A-Fa-f]{6}$', 'c')",
            name="accent_color_format",
        ),
        CheckConstraint(
            "watermark_text IS NULL OR REGEXP_LIKE(watermark_text, '^[A-Za-z0-9 -]{1,24}$', 'c')",
            name="watermark_text_format",
        ),
        CheckConstraint(
            "(watermark_image_key IS NULL) = (watermark_image_type IS NULL)",
            name="watermark_image_all_or_none",
        ),
        CheckConstraint(
            "watermark_text IS NULL OR watermark_image_key IS NULL", name="watermark_text_or_image"
        ),
        CheckConstraint("NOT is_default OR status = 'active'", name="default_is_active"),
        Index(
            "uq_invoice_templates_organization_id_name_lower",
            text("(CASE WHEN deleted_at IS NULL THEN `organization_id` END)"),
            text("(CASE WHEN deleted_at IS NULL THEN lower(name) END)"),
            unique=True,
        ),
        # At most one default template per practice.
        Index(
            "uq_invoice_templates_organization_id_default",
            text("(CASE WHEN is_default AND deleted_at IS NULL THEN `organization_id` END)"),
            unique=True,
        ),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    layout: Mapped[str] = mapped_column(String(32), nullable=False, server_default="standard")
    show_logo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    show_bank_details: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    footer_text: Mapped[str | None] = mapped_column(String(1000))
    # ``#RRGGBB``: the title, the practice name and the table headings. Null: black and grey.
    accent_color: Mapped[str | None] = mapped_column(String(7))
    # Printed large and pale across every page of an issued document. Null: none.
    watermark_text: Mapped[str | None] = mapped_column(String(24))
    # Or a picture, printed the same way: its key in the file store and its content type.
    watermark_image_key: Mapped[str | None] = mapped_column(Text)
    watermark_image_type: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")


__all__ = ["INVOICE_LAYOUTS", "TEMPLATE_STATUSES", "InvoiceTemplate"]
