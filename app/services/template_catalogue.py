"""The built-in email templates, their merge fields and the sample data of previews.

Every key here can be overridden per practice (``email_templates``). The notification types have
the key of their type; ``invoice``, ``staff_invitation`` and ``account_created`` are
the practice-facing system emails, whose built-in design (``app/templates/email/*.html``) is
what a practice that has not customised them sends. The defaults below are the plain
``{{ field }}`` equivalents a practice starts from; account emails of the central login
(verification, password reset, ...) are not practice-specific and are not listed.

An HTML body is the message itself: it is placed in the shared branded layout when sent.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.core.notification_types import CATALOGUE as NOTIFICATION_TYPES


@dataclass(frozen=True, slots=True)
class MergeField:
    name: str
    label: str
    description: str
    sample: str


_FIELDS = (
    MergeField("recipient_name", "Recipient name", "Who the email is addressed to.", "Alex Morgan"),
    MergeField("practice_name", "Practice name", "The name of your practice.", "Smith & Co"),
    MergeField("app_name", "Product name", "The name of the product.", "Capium Time & Fees"),
    MergeField(
        "link",
        "Link",
        "Opens the item in the app (the one-time link, in an invitation).",
        "https://app.example.com/smith-co/time/approvals",
    ),
    MergeField(
        "login_url", "Sign-in link", "The sign-in page of the app.", "https://app.example.com/login"
    ),
    MergeField("week_start", "Week starting", "First day of the timesheet week.", "13/10/2026"),
    MergeField("submitter_name", "Submitted by", "Who submitted the timesheet.", "Jamie Lee"),
    MergeField("approver_name", "Returned by", "Who returned the timesheet.", "Sam Patel"),
    MergeField(
        "decision_note",
        "Reason",
        "The note given when the timesheet was returned.",
        "Please describe Tuesday's entries.",
    ),
    MergeField("total_hours", "Total hours", "Hours on the timesheet, as h:mm.", "37:30"),
    MergeField("client_name", "Client name", "The client concerned.", "Widgets Ltd"),
    MergeField("job_name", "Job name", "The job concerned.", "Year-end accounts 2026"),
    MergeField("budget_percent", "Budget used (%)", "Share of the budget used.", "80"),
    MergeField("invoice_number", "Invoice number", "The invoice's number.", "INV-00042"),
    MergeField("amount_due", "Amount due", "What is still owed on the invoice.", "£1,200.00"),
    MergeField("issue_date", "Invoice date", "The date the invoice was issued.", "01/10/2026"),
    MergeField("due_date", "Due date", "The date payment is due.", "31/10/2026"),
    MergeField("days_overdue", "Days overdue", "Days past the due date.", "14"),
    MergeField(
        "sent_to", "Sent to", "The addresses the invoice was emailed to.", "bills@widgets.example"
    ),
    MergeField(
        "bank_details",
        "Bank details",
        "How to pay, from the practice settings.",
        "Sort code 12-34-56",
    ),
    MergeField(
        "job_type", "What failed", "The kind of background job that failed.", "Invoice email"
    ),
    MergeField("attempts", "Attempts", "How many times it was tried.", "5"),
    MergeField("invited_by", "Invited by", "Who sent the invitation.", "Sam Patel"),
    MergeField("added_by", "Added by", "Who created the account.", "Sam Patel"),
    MergeField("claimant_name", "Claimed by", "Who the expense claim is for.", "Jamie Lee"),
    MergeField(
        "claim_title", "Claim", "The title of the expense claim.", "Client visit, 13/10/2026"
    ),
    MergeField("claim_total", "Claim total", "The gross total of the expense claim.", "£84.50"),
    MergeField(
        "decider_name", "Decided by", "Who approved or returned the expense claim.", "Sam Patel"
    ),
    MergeField(
        "claim_reason",
        "Reason",
        "The reason the expense claim was returned.",
        "Please attach the hotel receipt.",
    ),
    MergeField("invoice_total", "Invoice total", "The gross total of the invoice.", "£1,200.00"),
    MergeField(
        "invoice_submitter",
        "Submitted by",
        "Who submitted the invoice for approval.",
        "Jamie Lee",
    ),
    MergeField(
        "invoice_decider", "Decided by", "Who approved or rejected the invoice.", "Sam Patel"
    ),
    MergeField(
        "invoice_reason",
        "Reason",
        "The reason the invoice was rejected.",
        "The VAT code on the second line is wrong.",
    ),
    MergeField(
        "payment_amount",
        "Payment amount",
        "The amount of the payment that was reversed.",
        "£480.00",
    ),
    MergeField(
        "payment_date", "Payment received", "The day the payment was received.", "01/10/2026"
    ),
    MergeField("payment_reversed_by", "Reversed by", "Who reversed the payment.", "Sam Patel"),
    MergeField(
        "payment_reason",
        "Reason",
        "Why the payment was reversed.",
        "Cheque returned by the bank.",
    ),
    MergeField("invoice_count", "Number of invoices", "How many invoices the email lists.", "2"),
    MergeField(
        "invoice_list",
        "Overdue invoices",
        "One line per overdue invoice: number, amount, due date and days overdue.",
        "INV-00042 - £1,200.00 - due 01/10/2026 (17 days overdue)",
    ),
    MergeField("total_due", "Total overdue", "What is overdue in all.", "£1,200.00"),
    MergeField("statement_date", "Statement date", "The date the statement runs to.", "18/10/2026"),
    MergeField(
        "statement_balance", "Balance", "What the client owes at the statement date.", "£1,200.00"
    ),
)

MERGE_FIELDS: dict[str, MergeField] = {field.name: field for field in _FIELDS}

type Category = Literal["notification", "system"]


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    key: str
    name: str
    description: str
    category: Category
    fields: tuple[str, ...]
    subject: str
    html: str
    text: str
    # Fields a template must use (a one-time link, without which the email is useless).
    required: tuple[str, ...] = ()


_H1 = '<h1 style="font-size:20px;margin:0 0 12px;">'


def _button(label: str) -> str:
    return (
        '<p style="margin:24px 0;"><a href="{{ link }}" style="display:inline-block;'
        "background:#5b5bf0;color:#ffffff;text-decoration:none;font-weight:600;"
        f'padding:12px 22px;border-radius:10px;">{label}</a></p>'
        '<p style="font-size:13px;color:#7a7f99;word-break:break-all;">If the button does not '
        "work, copy this link into your browser:<br>{{ link }}</p>"
    )


_COMMON = ("recipient_name", "practice_name", "app_name")

_NOTIFICATIONS: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        key="timesheet_reminder",
        name="Timesheet reminder",
        description="Reminds someone to submit a timesheet that is still open.",
        category="notification",
        fields=(*_COMMON, "week_start", "link"),
        subject="Reminder: submit your timesheet for the week of {{ week_start }}",
        html=(
            f"{_H1}Timesheet reminder</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>Your timesheet for the week starting {{ week_start }} has not been submitted yet. "
            "Please complete it and submit it for approval.</p>" + _button("Open my timesheet")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "Your timesheet for the week starting {{ week_start }} has not been submitted yet. "
            "Please complete it and submit it for approval.\n\n"
            "Open it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="timesheet_returned",
        name="Timesheet returned",
        description="Tells someone their timesheet was returned for changes.",
        category="notification",
        fields=(*_COMMON, "week_start", "approver_name", "decision_note", "link"),
        subject="Your timesheet for the week of {{ week_start }} was returned",
        html=(
            f"{_H1}Timesheet returned</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ approver_name }} returned your timesheet for the week starting "
            "{{ week_start }}.</p>"
            "<p><strong>Reason:</strong> {{ decision_note }}</p>"
            "<p>Please make the changes and submit it again.</p>" + _button("Open my timesheet")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ approver_name }} returned your timesheet for the week starting "
            "{{ week_start }}.\n\n"
            "Reason: {{ decision_note }}\n\n"
            "Please make the changes and submit it again: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="approval_pending",
        name="Approval pending",
        description="Tells an approver a timesheet is waiting for them.",
        category="notification",
        fields=(*_COMMON, "submitter_name", "week_start", "total_hours", "link"),
        subject="{{ submitter_name }} submitted a timesheet for approval",
        html=(
            f"{_H1}Timesheet waiting for approval</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ submitter_name }} submitted their timesheet for the week starting "
            "{{ week_start }} ({{ total_hours }} hours).</p>" + _button("Review timesheets")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ submitter_name }} submitted their timesheet for the week starting "
            "{{ week_start }} ({{ total_hours }} hours).\n\n"
            "Review it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="budget_alert",
        name="Budget alert",
        description="Warns when a job has used a set share of its budget.",
        category="notification",
        fields=(*_COMMON, "client_name", "job_name", "budget_percent", "link"),
        subject="Budget alert: {{ job_name }} has used {{ budget_percent }}% of its budget",
        html=(
            f"{_H1}Budget alert</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>The job <strong>{{ job_name }}</strong> for {{ client_name }} has used "
            "{{ budget_percent }}% of its budget.</p>" + _button("Open the job")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "The job {{ job_name }} for {{ client_name }} has used {{ budget_percent }}% of its "
            "budget.\n\nOpen it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="invoice_delivery",
        name="Invoice delivery",
        description="Confirms to the sender that an invoice was emailed to the client.",
        category="notification",
        fields=(
            *_COMMON,
            "client_name",
            "invoice_number",
            "amount_due",
            "sent_to",
            "link",
        ),
        subject="Invoice {{ invoice_number }} was sent to {{ client_name }}",
        html=(
            f"{_H1}Invoice sent</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>Invoice <strong>{{ invoice_number }}</strong> ({{ amount_due }}) was emailed to "
            "{{ sent_to }} for {{ client_name }}.</p>" + _button("Open the invoice")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "Invoice {{ invoice_number }} ({{ amount_due }}) was emailed to {{ sent_to }} for "
            "{{ client_name }}.\n\nOpen it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="payment_reminder",
        name="Payment reminder",
        description="Tells staff an invoice is overdue and needs chasing.",
        category="notification",
        fields=(
            *_COMMON,
            "client_name",
            "invoice_number",
            "amount_due",
            "due_date",
            "days_overdue",
            "link",
        ),
        subject="Payment reminder: invoice {{ invoice_number }} from {{ client_name }} is overdue",
        html=(
            f"{_H1}Payment reminder</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>Invoice <strong>{{ invoice_number }}</strong> for {{ client_name }} "
            "({{ amount_due }}) was due on {{ due_date }} and is {{ days_overdue }} days "
            "overdue.</p>" + _button("Open the invoice")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "Invoice {{ invoice_number }} for {{ client_name }} ({{ amount_due }}) was due on "
            "{{ due_date }} and is {{ days_overdue }} days overdue.\n\n"
            "Open it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="failed_job",
        name="Failed job",
        description="Tells practice administrators a background job failed.",
        category="notification",
        fields=(*_COMMON, "job_type", "attempts"),
        subject="A background job failed in {{ practice_name }}",
        html=(
            f"{_H1}A background job failed</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ job_type }} failed after {{ attempts }} attempts and will not be retried. "
            "Check the item concerned and try again, or ask your administrator.</p>"
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ job_type }} failed after {{ attempts }} attempts and will not be retried. "
            "Check the item concerned and try again, or ask your administrator.\n\n"
            "{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="expense_pending",
        name="Expense claim pending",
        description="Tells an approver an expense claim is waiting for them.",
        category="notification",
        fields=(*_COMMON, "claimant_name", "claim_title", "claim_total", "link"),
        subject="{{ claimant_name }} submitted an expense claim for approval",
        html=(
            f"{_H1}Expense claim waiting for approval</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ claimant_name }} submitted the expense claim <strong>{{ claim_title }}</strong> "
            "({{ claim_total }}).</p>" + _button("Review expense claims")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ claimant_name }} submitted the expense claim {{ claim_title }} "
            "({{ claim_total }}).\n\n"
            "Review it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="expense_approved",
        name="Expense claim approved",
        description="Tells someone their expense claim was approved.",
        category="notification",
        fields=(*_COMMON, "claim_title", "claim_total", "decider_name", "link"),
        subject="Your expense claim was approved",
        html=(
            f"{_H1}Expense claim approved</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ decider_name }} approved your expense claim <strong>{{ claim_title }}</strong> "
            "({{ claim_total }}).</p>" + _button("Open my expenses")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ decider_name }} approved your expense claim {{ claim_title }} "
            "({{ claim_total }}).\n\n"
            "Open it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="expense_returned",
        name="Expense claim returned",
        description="Tells someone their expense claim was returned for changes.",
        category="notification",
        fields=(*_COMMON, "claim_title", "claim_total", "decider_name", "claim_reason", "link"),
        subject="Your expense claim was returned",
        html=(
            f"{_H1}Expense claim returned</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ decider_name }} returned your expense claim <strong>{{ claim_title }}</strong> "
            "({{ claim_total }}).</p>"
            "<p><strong>Reason:</strong> {{ claim_reason }}</p>"
            "<p>Please make the changes and submit it again.</p>" + _button("Open my expenses")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ decider_name }} returned your expense claim {{ claim_title }} "
            "({{ claim_total }}).\n\n"
            "Reason: {{ claim_reason }}\n\n"
            "Please make the changes and submit it again: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="invoice_approval_pending",
        name="Invoice approval pending",
        description="Tells an approver an invoice is waiting for their approval.",
        category="notification",
        fields=(*_COMMON, "client_name", "invoice_total", "invoice_submitter", "link"),
        subject="{{ invoice_submitter }} submitted an invoice for {{ client_name }} for approval",
        html=(
            f"{_H1}Invoice waiting for approval</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ invoice_submitter }} submitted an invoice for "
            "<strong>{{ client_name }}</strong> "
            "({{ invoice_total }}) for your approval.</p>" + _button("Review the invoice")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ invoice_submitter }} submitted an invoice for {{ client_name }} "
            "({{ invoice_total }}) for your approval.\n\n"
            "Review it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="invoice_approved",
        name="Invoice approved",
        description="Tells the person who prepared an invoice that it was approved.",
        category="notification",
        fields=(*_COMMON, "client_name", "invoice_total", "invoice_decider", "link"),
        subject="Your invoice for {{ client_name }} was approved",
        html=(
            f"{_H1}Invoice approved</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ invoice_decider }} approved your invoice for <strong>{{ client_name }}</strong> "
            "({{ invoice_total }}). It can now be issued.</p>" + _button("Open the invoice")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ invoice_decider }} approved your invoice for {{ client_name }} "
            "({{ invoice_total }}). It can now be issued.\n\n"
            "Open it here: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="invoice_rejected",
        name="Invoice rejected",
        description="Tells the person who prepared an invoice that it was rejected, and why.",
        category="notification",
        fields=(
            *_COMMON,
            "client_name",
            "invoice_total",
            "invoice_decider",
            "invoice_reason",
            "link",
        ),
        subject="Your invoice for {{ client_name }} was rejected",
        html=(
            f"{_H1}Invoice rejected</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ invoice_decider }} rejected your invoice for <strong>{{ client_name }}</strong> "
            "({{ invoice_total }}). It is a draft again.</p>"
            "<p><strong>Reason:</strong> {{ invoice_reason }}</p>"
            "<p>Please make the changes and submit it again.</p>" + _button("Open the invoice")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ invoice_decider }} rejected your invoice for {{ client_name }} "
            "({{ invoice_total }}). It is a draft again.\n\n"
            "Reason: {{ invoice_reason }}\n\n"
            "Please make the changes and submit it again: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="payment_reversed",
        name="Payment reversed",
        description="Tells billing staff a payment from a client was reversed.",
        category="notification",
        fields=(
            *_COMMON,
            "client_name",
            "payment_amount",
            "payment_date",
            "payment_reversed_by",
            "payment_reason",
            "link",
        ),
        subject="A payment from {{ client_name }} was reversed",
        html=(
            f"{_H1}Payment reversed</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ payment_reversed_by }} reversed the payment of {{ payment_amount }} from "
            "<strong>{{ client_name }}</strong> received on {{ payment_date }}. The invoices it "
            "paid are owing again.</p>"
            "<p><strong>Reason:</strong> {{ payment_reason }}</p>" + _button("Open the payments")
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ payment_reversed_by }} reversed the payment of {{ payment_amount }} from "
            "{{ client_name }} received on {{ payment_date }}. The invoices it paid are owing "
            "again.\n\n"
            "Reason: {{ payment_reason }}\n\n"
            "Open the payments: {{ link }}\n\n{{ practice_name }}"
        ),
    ),
)

_SYSTEM: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        key="invoice",
        name="Invoice email",
        description="The email that carries an invoice PDF to the client.",
        category="system",
        fields=(
            "recipient_name",
            "practice_name",
            "app_name",
            "client_name",
            "invoice_number",
            "issue_date",
            "due_date",
            "amount_due",
            "bank_details",
        ),
        subject="Invoice {{ invoice_number }} from {{ practice_name }}",
        html=(
            "<p>Hello {{ recipient_name }},</p>"
            "<p>Please find attached invoice <strong>{{ invoice_number }}</strong> for "
            "{{ client_name }}.</p>"
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="margin:16px 0;font-size:14px;">'
            '<tr><td style="padding:2px 16px 2px 0;color:#7a7f99;">Invoice date</td>'
            "<td>{{ issue_date }}</td></tr>"
            '<tr><td style="padding:2px 16px 2px 0;color:#7a7f99;">Amount due</td>'
            "<td><strong>{{ amount_due }}</strong></td></tr>"
            '<tr><td style="padding:2px 16px 2px 0;color:#7a7f99;">Due by</td>'
            "<td>{{ due_date }}</td></tr></table>"
            '<p style="white-space:pre-line;">{{ bank_details }}</p>'
            "<p>Thank you,<br>{{ practice_name }}</p>"
        ),
        text=(
            "Hello {{ recipient_name }},\n\n"
            "Please find attached invoice {{ invoice_number }} for {{ client_name }}.\n\n"
            "Invoice date: {{ issue_date }}\n"
            "Amount due:   {{ amount_due }}\n"
            "Due by:       {{ due_date }}\n\n"
            "{{ bank_details }}\n\n"
            "Thank you,\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="staff_invitation",
        name="Staff invitation",
        description="Invites a staff member to sign in and choose a password.",
        category="system",
        fields=(
            "recipient_name",
            "practice_name",
            "app_name",
            "invited_by",
            "link",
            "login_url",
        ),
        required=("link",),
        subject="You are invited to {{ practice_name }} on {{ app_name }}",
        html=(
            f"{_H1}You are invited</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ invited_by }} invited you to sign in to {{ practice_name }} on "
            "{{ app_name }}.</p>"
            "<p>Choose your password with the button below. The link is valid for 7 days and "
            "can be used once.</p>"
            + _button("Accept the invitation")
            + '<p>After that, sign in at <a href="{{ login_url }}">{{ login_url }}</a> with this '
            "email address.</p>"
            "<p>If you were not expecting this, you can ignore this email.</p>"
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ invited_by }} invited you to sign in to {{ practice_name }} on {{ app_name }}.\n\n"
            "Choose your password with this link. It is valid for 7 days and can be used once:\n\n"
            "{{ link }}\n\n"
            "After that, sign in at {{ login_url }} with this email address.\n\n"
            "If you were not expecting this, you can ignore this email."
        ),
    ),
    TemplateSpec(
        key="account_created",
        name="Account created",
        description="Tells a new staff member their account is ready and how to choose a password.",
        category="system",
        fields=(
            "recipient_name",
            "practice_name",
            "app_name",
            "added_by",
            "link",
            "login_url",
        ),
        required=("link",),
        subject="Your {{ app_name }} account is ready",
        html=(
            f"{_H1}Your account is ready</h1>"
            "<p>Hi {{ recipient_name }},</p>"
            "<p>{{ added_by }} created an account for you at {{ practice_name }} on "
            "{{ app_name }}.</p>"
            "<p>Choose your password with the button below. The link is valid for 7 days and "
            "can be used once.</p>"
            + _button("Choose your password")
            + '<p>After that, sign in at <a href="{{ login_url }}">{{ login_url }}</a> with this '
            "email address.</p>"
            "<p>If you were not expecting this, you can ignore this email.</p>"
        ),
        text=(
            "Hi {{ recipient_name }},\n\n"
            "{{ added_by }} created an account for you at {{ practice_name }} on "
            "{{ app_name }}.\n\n"
            "Choose your password with this link. It is valid for 7 days and can be used once:\n\n"
            "{{ link }}\n\n"
            "After that, sign in at {{ login_url }} with this email address.\n\n"
            "If you were not expecting this, you can ignore this email."
        ),
    ),
    TemplateSpec(
        key="client_payment_reminder",
        name="Payment reminder (to the client)",
        description="The reminder emailed to a client about its overdue invoices.",
        category="system",
        fields=(
            "recipient_name",
            "practice_name",
            "app_name",
            "client_name",
            "invoice_count",
            "invoice_list",
            "total_due",
            "bank_details",
        ),
        subject="Payment reminder from {{ practice_name }}",
        html=(
            "<p>Dear {{ recipient_name }},</p>"
            "<p>Our records show that {{ invoice_count }} invoice(s) for {{ client_name }} "
            "are now overdue:</p>"
            '<p style="white-space:pre-line;">{{ invoice_list }}</p>'
            "<p>The total overdue is <strong>{{ total_due }}</strong>. If you have already "
            "paid, please ignore this reminder and accept our thanks. Otherwise, please arrange "
            "payment as soon as you can.</p>"
            '<p style="white-space:pre-line;">{{ bank_details }}</p>'
            "<p>Thank you,<br>{{ practice_name }}</p>"
        ),
        text=(
            "Dear {{ recipient_name }},\n\n"
            "Our records show that {{ invoice_count }} invoice(s) for {{ client_name }} are now "
            "overdue:\n\n{{ invoice_list }}\n\n"
            "The total overdue is {{ total_due }}. If you have already paid, please ignore this "
            "reminder and accept our thanks. Otherwise, please arrange payment as soon as you "
            "can.\n\n{{ bank_details }}\n\n"
            "Thank you,\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="client_payment_final_notice",
        name="Final payment notice (to the client)",
        description="The firmer notice emailed to a client whose invoices are still unpaid.",
        category="system",
        fields=(
            "recipient_name",
            "practice_name",
            "app_name",
            "client_name",
            "invoice_count",
            "invoice_list",
            "total_due",
            "bank_details",
        ),
        subject="Final notice: overdue invoices from {{ practice_name }}",
        html=(
            "<p>Dear {{ recipient_name }},</p>"
            "<p>Despite our earlier reminders, {{ invoice_count }} invoice(s) for "
            "{{ client_name }} remain unpaid:</p>"
            '<p style="white-space:pre-line;">{{ invoice_list }}</p>'
            "<p>The total overdue is <strong>{{ total_due }}</strong>. Please arrange payment "
            "immediately, or contact us today if there is a problem with any of these "
            "invoices.</p>"
            '<p style="white-space:pre-line;">{{ bank_details }}</p>'
            "<p>Yours sincerely,<br>{{ practice_name }}</p>"
        ),
        text=(
            "Dear {{ recipient_name }},\n\n"
            "Despite our earlier reminders, {{ invoice_count }} invoice(s) for {{ client_name }} "
            "remain unpaid:\n\n{{ invoice_list }}\n\n"
            "The total overdue is {{ total_due }}. Please arrange payment immediately, or "
            "contact us today if there is a problem with any of these invoices.\n\n"
            "{{ bank_details }}\n\n"
            "Yours sincerely,\n{{ practice_name }}"
        ),
    ),
    TemplateSpec(
        key="client_statement",
        name="Client statement",
        description="The email that carries a client's statement of account (PDF attached).",
        category="system",
        fields=(
            "recipient_name",
            "practice_name",
            "app_name",
            "client_name",
            "statement_date",
            "statement_balance",
            "bank_details",
        ),
        subject="Statement of account from {{ practice_name }}",
        html=(
            "<p>Dear {{ recipient_name }},</p>"
            "<p>Please find attached the statement of account for {{ client_name }} up to "
            "{{ statement_date }}.</p>"
            "<p>The balance is <strong>{{ statement_balance }}</strong>.</p>"
            '<p style="white-space:pre-line;">{{ bank_details }}</p>'
            "<p>If anything looks wrong, please get in touch.</p>"
            "<p>Thank you,<br>{{ practice_name }}</p>"
        ),
        text=(
            "Dear {{ recipient_name }},\n\n"
            "Please find attached the statement of account for {{ client_name }} up to "
            "{{ statement_date }}.\n\n"
            "The balance is {{ statement_balance }}.\n\n"
            "{{ bank_details }}\n\n"
            "If anything looks wrong, please get in touch.\n\n"
            "Thank you,\n{{ practice_name }}"
        ),
    ),
)

TEMPLATES: dict[str, TemplateSpec] = {spec.key: spec for spec in (*_NOTIFICATIONS, *_SYSTEM)}
NOTIFICATION_KEYS = frozenset(entry.code for entry in NOTIFICATION_TYPES)


class UnknownTemplateError(KeyError):
    """The key is not in the catalogue."""


def get_template(key: str) -> TemplateSpec:
    try:
        return TEMPLATES[key]
    except KeyError as exc:
        raise UnknownTemplateError(key) from exc


def sample_values(spec: TemplateSpec, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Example values for every field of ``spec`` (what a preview shows)."""
    values = {name: MERGE_FIELDS[name].sample for name in spec.fields}
    if overrides:
        values.update({k: v for k, v in overrides.items() if k in values})
    return values
