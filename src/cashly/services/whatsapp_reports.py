import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from cashly.db.models import Expense, ExpenseCategory
from cashly.services.finance import serialize
from cashly.services.whatsapp_reply import MONTHS, rupiah

PDF_HELP = (
    "📄 *Pilih periode PDF-mu*\n"
    "• `/reportpdf hari ini`\n• `/reportpdf bulan ini`\n• `/reportpdf bulan lalu`\n"
    "• `/reportpdf 1 nov 2025 sampai 30 nov 2025`"
)
MONTH_NUMBERS = {name.lower(): index for index, name in enumerate(MONTHS, 1)}
MONTH_NUMBERS.update(
    dict(
        zip(
            (
                "januari februari maret april mei juni juli agustus "
                "september oktober november desember"
            ).split(),
            range(1, 13),
            strict=True,
        )
    )
)


def period(value, today):
    current_month = today.replace(day=1)
    if value == "hari ini":
        return today, today
    if value == "bulan ini":
        return current_month, today
    if value == "bulan lalu":
        end = current_month - timedelta(days=1)
        return end.replace(day=1), end
    parts = re.fullmatch(r"(\d{1,2}) ([a-z]+) (\d{4}) sampai (\d{1,2}) ([a-z]+) (\d{4})", value)
    if not parts:
        raise ValueError("Invalid period syntax")
    day, month, year, end_day, end_month, end_year = parts.groups()
    start = date(int(year), MONTH_NUMBERS.get(month, 0), int(day))
    end = date(int(end_year), MONTH_NUMBERS.get(end_month, 0), int(end_day))
    if start.year < 1900 or start > end or end > today or (end - start).days > 365:
        raise ValueError("Invalid period")
    return start, end


def period_label(start, end):
    def display(day):
        return f"{day.day} {MONTHS[day.month - 1]} {day.year}"

    return display(start) if start == end else f"{display(start)} – {display(end)}"


def expense_scope(account_id, timezone, start, end):
    zone = ZoneInfo(timezone)
    return (
        Expense.account_id == account_id,
        Expense.deleted_at.is_(None),
        Expense.occurred_at >= datetime.combine(start, time.min, zone),
        Expense.occurred_at < datetime.combine(end + timedelta(days=1), time.min, zone),
    )


def summary(session, account_id, timezone, start, end):
    total = func.sum(Expense.amount_idr)
    rows = session.execute(
        select(ExpenseCategory.label_id, total, func.count(Expense.id))
        .join(ExpenseCategory, ExpenseCategory.code == Expense.category_code)
        .where(*expense_scope(account_id, timezone, start, end))
        .group_by(ExpenseCategory.code, ExpenseCategory.label_id)
        .order_by(total.desc(), ExpenseCategory.code)
    ).all()
    heading = f"📊 *Laporan Cashly*\n{period_label(start, end)}"
    if not rows:
        return f"{heading}\nBelum ada pengeluaran di periode ini."
    total_idr = sum(row[1] for row in rows)
    count = sum(row[2] for row in rows)
    top, top_amount, _ = rows[0]
    return (
        f"{heading}\n*{rupiah(total_idr)}* dari {count} transaksi.\n"
        f"Terbesar: {top} · {rupiah(top_amount)}."
    )


def print_data(session, account_id, timezone, start, end):
    comparison_start = start - timedelta(days=(end - start).days + 1)
    rows = list(
        session.scalars(
            select(Expense)
            .where(*expense_scope(account_id, timezone, comparison_start, end))
            .order_by(Expense.occurred_at.desc(), Expense.id.desc())
            .limit(1001)
        )
    )
    if len(rows) > 1000:
        return None
    return {
        "expenses": [serialize(row, []) for row in rows],
        "timezone": timezone,
        "start": str(start),
        "end": str(end),
        "period_label": period_label(start, end),
    }
