from urllib.parse import urlsplit


def guide(settings, *, linked, welcome=False):
    lines = [
        "✅ *WhatsApp kamu udah nyambung ke Cashly!*"
        if welcome
        else "👋 *Kenalan sama Mochi, yuk!*",
        "Cashly bantu kamu catat pengeluaran dan lihat uangmu paling banyak kepakai buat apa.",
        "",
    ]
    if not linked:
        lines.append("Buka *Cashly → Profil → Hubungkan WhatsApp*, lalu ikuti verifikasi OTP, ya.")
    elif settings.whatsapp_recording_enabled:
        lines.extend(
            [
                "💬 *Chat* — ketik `naspad 20rb` atau `kopi 18rb sama parkir 5rb`.",
                "🧾 *Struk* — kirim foto yang jelas atau PDF bukti bayar untuk satu transaksi.",
                "🎙️ *VN* — bilang aja beli apa dan berapa harganya.",
                "Kalau detailnya lengkap, Mochi langsung catat. Kalau ada yang kurang, balas "
                "pertanyaan Mochi di sini. Hasilnya bisa kamu cek dan edit di Cashly.",
            ]
        )
    if linked:
        lines.extend(
            ["", "📊 Cek pengeluaran pakai `/laporan hari ini` atau `/laporan bulan ini`."]
        )
        if settings.whatsapp_report_frontend_url:
            lines.append(
                "📄 Ketik `/reportpdf bulan ini` buat dapat file PDF. "
                "Ketik `/reportpdf` buat lihat contoh tanggalnya."
            )
    url = settings.whatsapp_app_url.strip()
    try:
        target = urlsplit(url)
        valid_url = target.scheme == "https" and target.hostname and not target.username
    except ValueError:
        valid_url = False
    if valid_url:
        lines.extend(["", f"*Buka Cashly* buat lihat dashboard atau pasang aplikasinya:\n{url}"])
    if welcome:
        lines.extend(["", "Lupa caranya? Ketik `/help` kapan aja."])
    return "\n".join(lines)
