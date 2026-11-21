"""Format a WhatsApp confirmation from expense rows and item rows."""

MONTHS = "Jan Feb Mar Apr Mei Jun Jul Agu Sep Okt Nov Des".split()
TIME_LABELS = {"Asia/Jakarta": "WIB", "Asia/Makassar": "WITA", "Asia/Jayapura": "WIT"}
MARKUP = str.maketrans("", "", "*_~`")


def rupiah(amount):
    return "Rp" + f"{amount:,}".replace(",", ".")


def label(value):
    return " ".join(value.translate(MARKUP).split()) or "Pengeluaran"


def when(row, show_time):
    occurred = row.occurred_at
    day = f"{occurred.day} {MONTHS[occurred.month - 1]} {occurred.year}"
    if not show_time:
        return day
    offset = occurred.strftime("%z")
    zone = TIME_LABELS.get(getattr(occurred.tzinfo, "key", ""))
    zone = zone or f"UTC{offset[:3]}:{offset[3:]}"
    date_time = f"{day}, {occurred:%H.%M} {zone}"
    return f"Waktu pesan · {date_time}" if row.timestamp_basis == "message_time" else date_time


def saved_expenses(records, payment_labels):
    lines = ["✅ *Tersimpan di Cashly*"]
    if len(records) > 1:
        total = sum(row.amount_idr for row, *_ in records)
        for row, _, _, show_time in records[:8]:
            title = label(row.merchant or row.description)
            lines.append(f"• {title} · {rupiah(row.amount_idr)} · {when(row, show_time)}")
        if len(records) > 8:
            lines.append(f"+ {len(records) - 8} transaksi lain di Cashly")
        lines.append(f"*Total {len(records)} transaksi · {rupiah(total)}*")
    else:
        row, items, category, show_time = records[0]
        title = label(row.merchant or row.description)
        lines.extend((f"*{title} · {rupiah(row.amount_idr)}*", when(row, show_time)))
        payment = payment_labels.get(row.payment_method)
        lines.append(f"{label(category)} · {payment}" if payment else label(category))
        if (
            len(items) > 1
            or label(items[0].label) != title
            or items[0].quantity != 1
            or row.tax_idr
            or row.discount_idr
        ):
            lines.extend(("", "*Rincian struk*" if row.source_type == "receipt" else "*Rincian*"))
            for item in items[:5]:
                lines.append(f"• {label(item.label)} ×{item.quantity} — {rupiah(item.amount_idr)}")
            if len(items) > 5:
                lines.append(f"+ {len(items) - 5} item lain di Cashly")
            if row.discount_idr:
                lines.append(f"• Diskon −{rupiah(row.discount_idr)}")
            if row.tax_idr:
                lines.append(f"• Pajak +{rupiah(row.tax_idr)}")
    lines.extend(("", "Ada yang kurang pas? Kamu bisa edit transaksinya di Cashly."))
    return "\n".join(lines)
