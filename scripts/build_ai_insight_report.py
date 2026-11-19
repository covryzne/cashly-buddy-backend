"""Render the sanitized verification artifacts into a user-facing insight report."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    verification = ROOT / "specs/verification"
    live = json.loads((verification / "ai-live-e2e.json").read_text(encoding="utf-8"))
    browser = json.loads((verification / "multimodal-browser.json").read_text(encoding="utf-8"))
    probes = json.loads((verification / "ai-multimodal-live.json").read_text(encoding="utf-8"))
    ai = {
        f"{row['input_kind']}/{row['phase']}": row
        for row in live["measurements"]
        if row["event"] == "ai_extraction_completed"
    }
    saves = [row for row in live["measurements"] if row.get("route") == "/expenses/chat-batch"]
    decode = next(row for row in live["measurements"] if row.get("phase") == "decode")
    rows = "\n".join(
        f"| {row['suite']} | {row['passed']} | {row['failed']} | {row['skipped']} |"
        for row in browser["suites"]
    )
    providers = "\n".join(
        (
            f"| {row['provider']} | {row['input_kind']} | `{row['model']}` | "
            f"{row['outcome']} | {row['duration_ms'] / 1000:.2f} s |"
        )
        for row in probes["checks"]
    )
    message = f"""# Laporan VN, multimodal, dan performance - 19 November 2026

Task 4-6 sudah diimplementasikan: rekam/upload audio -> transkrip asli yang bisa
dikoreksi -> ekstraksi bersama -> pratinjau editable -> simpan eksplisit. Tidak ada
transaksi yang otomatis disimpan oleh OCR, transkripsi, atau ekstraksi.

## Hasil pengujian

Backend: **108 tes terkait lolos**, mencakup chat/OCR/VN, tujuh format audio, durasi hasil
decode, audio hening/rusak, timeout, respons tidak valid, log aman, akun nonaktif,
serta penyimpanan receipt/voice dan idempotensi di PostgreSQL disposable.

| Suite browser | Lolos | Gagal | Dilewati |
| --- | ---: | ---: | ---: |
{rows}

Suite browser memakai Chrome desktop/mobile dan WebKit dengan viewport iPhone
(termasuk tema gelap). Satu tes native MediaRecorder dilewati karena runtime
WebKit Windows ini tidak menyediakan MediaRecorder; upload/transkrip/pratinjau
tetap diuji. Ini **belum merupakan pengujian iPhone fisik**.

E2E nyata: **{"lolos" if live["browser_passed"] else "belum lolos"}**. Browser memakai
Gemini nyata, FastAPI nyata, dan PostgreSQL lokal disposable. Login/identity tetap
fixture sintetis; tidak ada klaim bahwa tes ini menguji Supabase Auth hosted.

- OCR disimpan sebagai `receipt`; VN disimpan sebagai `voice`.
- Transkrip VN dikoreksi dari nominal fixture ke Rp15.000 sebelum ekstraksi;
  draft dan transaksi tersimpan mengikuti koreksi tersebut.
- Respons simpan VN sengaja dibuat terputus sesudah backend commit. Retry dengan
  ID/key yang sama tidak menambah transaksi lagi.
- Total transaksi unik saat pengujian: **{live["stored_expenses"]}**, dengan sumber
  `{", ".join(live["stored_sources"])}`; nominal sesuai fixture: **{live["saved_totals_match"]}**.
- Penghapusan lewat API browser: **{live["soft_deleted_by_browser"]}**; hard cleanup akun,
  transaksi, item, idempotency, dan efek reward uji: **{live["cleanup_passed"]}**.
- Penulisan ke database staging: **{live["hosted_database_writes"]}**.

Tes offline meliputi refresh, navigasi data tersimpan, zero API hit saat offline,
pemisahan akun A/B, logout lintas tab, snapshot pemilik yang salah, session expiry,
reconnect, dan storage tidak tersedia. Input foto/VN di dialog yang masih terbuka
juga bertahan saat koneksi putus dan tersambung lagi. Media/transkrip yang belum
disimpan tidak dipersist ke snapshot; refresh atau menutup dialog membuangnya.

## Pengukuran nyata

Angka berikut adalah observasi fixture kecil, bukan p95 atau SLA produksi.
Database adalah PostgreSQL lokal; jangan menganggapnya sebagai latency DB staging.

| Operasi pada E2E nyata | Waktu |
| --- | ---: |
| OCR Gemini | {ai["receipt/extract"]["duration_ms"] / 1000:.2f} s |
| Decode/normalisasi audio {decode["audio_duration_seconds"]:.3f} s | {decode["decode_ms"]:.2f} ms |
| Transkripsi Gemini | {ai["voice/transcribe"]["duration_ms"] / 1000:.2f} s |
| Ekstraksi dari transkrip yang dikoreksi | {ai["voice/extract"]["duration_ms"] / 1000:.2f} s |
| Simpan OCR | {saves[0]["duration_ms"]:.2f} ms / {saves[0]["db_queries"]} query |
| Simpan VN | {saves[1]["duration_ms"]:.2f} ms / {saves[1]["db_queries"]} query |
| Replay simpan VN | {saves[2]["duration_ms"]:.2f} ms / {saves[2]["db_queries"]} query |

Pada sampel ini waktu tunggu didominasi provider AI. Transaksi DB untuk validasi
akun/kategori selesai sebelum menunggu model; koneksi DB tidak ditahan sepanjang
request AI. Query count di atas berasal dari satu transaksi dengan satu item;
belum ada benchmark batch besar untuk menyimpulkan perilaku N+1 pada semua ukuran.

## Kemampuan provider yang benar-benar diuji

| Provider | Input/skenario | Model konfigurasi | Hasil | Durasi probe |
| --- | --- | --- | --- | ---: |
{providers}

Gemini aktif `gemini-3.5-flash-lite` berhasil untuk chat, foto, dan VN. Foto yang
diburamkan secara berat menghasilkan klarifikasi dengan **0 draft siap simpan**.
Audio berupa nada tanpa ucapan menghasilkan `unclear`; tidak dibuat transkrip
ucapan atau transaksi rekaan. Ini pengujian input sintetis, bukan tolok ukur
akurasi semua tulisan tangan, aksen, atau kondisi bising di dunia nyata.

OpenRouter/free berhasil untuk chat dan OCR. Pada probe, model terpilih masing-
masing `inclusionai/ling-3.2-flash:free` dan `nvidia/nemotron-3.5-super-vl:free`.
Rute gratis dapat memilih model yang berbeda pada request berikutnya.

Untuk audio, katalog OpenRouter mengiklankan model gratis
`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` dengan audio input, tetapi tidak
mengiklankan native `json_schema`. Menghilangkan persyaratan schema native
mengubah respons rute dari 404 menjadi **upstream 402**. Pengujian model tersebut
secara eksplisit juga menerima **upstream 402**, bukan transkrip. Keberhasilan VN
OpenRouter **belum terverifikasi**. Status 503 pada JSON probe adalah pemetaan error
aplikasi; status provider yang diamati adalah 402. Penyebab akun/billing/provider
lebih rinci belum dikonfirmasi, dan tidak disimpulkan dari keberhasilan chat.

Backend tetap memvalidasi JSON transkrip dengan Pydantic walaupun OpenRouter audio
menggunakan schema dalam prompt. Respons invalid menampilkan error dan menyimpan
input untuk retry. Update berikutnya menambahkan fallback model otomatis sesuai
ADR-0104; hasil pengujian di laporan ini merupakan baseline implementasi VN.

## Perilaku menunggu dan insight log

Status membedakan membaca foto, mendengarkan VN, dan merapikan transkrip. Pembatalan
tidak menghapus input. Jika browser tidak dapat memutar codec upload, transkripsi
server tetap tersedia dengan keterangan bahwa preview audio belum dapat diputar.

Log mencatat provider/model, resolved model bila ada, input_kind, phase, request ID,
upstream status, outcome, durasi provider, durasi audio, dan decode_ms. Log HTTP
tetap memuat insight auth/DB/proxy. Media, transkrip, caption, isi transaksi,
credential, dan pesan error mentah provider tidak dicatat ke log aplikasi.

## Yang perlu disiapkan

Aktifkan env backend yang dipakai, lalu:

```bash
python -m pip install -r requirements.txt
```

PyAV `19.0.2` ditambahkan dan dipin. `requirements.txt` mengacu ke lock yang sama.
Restart backend; tidak perlu migrasi database atau API key baru untuk Gemini yang
sudah aktif. Model/key tetap dibaca dari environment backend.

Untuk memilih OpenRouter pada chat/OCR sambil memakai Gemini untuk transkripsi:

```dotenv
AI_PROVIDER=openrouter
OPENROUTER_MODEL=openrouter/free
AI_AUDIO_PROVIDER=gemini
GEMINI_AUDIO_MODEL=gemini-3.5-flash-lite
```

`AI_AUDIO_PROVIDER` hanya mengatur transkripsi audio; ekstraksi transkrip yang sudah
dikoreksi tetap memakai `AI_PROVIDER`. Blank override mengikuti provider utama.
Model gambar/audio per provider juga opsional: `GEMINI_IMAGE_MODEL`,
`GEMINI_AUDIO_MODEL`, `OPENROUTER_IMAGE_MODEL`, `OPENROUTER_AUDIO_MODEL`.

## Reproduksi dan pengecekan perangkat

- Frontend: `npm run test:multimodal` membangun `.next-offline-test`, lalu menjalankan
  suite OCR, VN, chat, dan offline secara berurutan. Instal browser pengujian dengan
  `npx playwright install webkit` bila runtime WebKit belum tersedia. Build ini tidak
  mengganti cache `.next/dev` milik development yang sedang berjalan.
- Backend Windows: `powershell -File scripts/make_synthetic_voice.ps1` membuat WAV
  sintetis lokal. `python scripts/check_multimodal.py --app-env staging --providers
  gemini openrouter --audio .pytest-tmp-vn-live-assets/expense.wav` menguji modalitas
  tanpa menulis DB. `--kinds` dapat membatasi probe dan `--report` menyimpan metadata.
- Live E2E backend opt-in: set `CASHLY_LIVE_AI=1`, lalu jalankan
  `python -m pytest tests/test_live_multimodal_e2e.py`. Membutuhkan PostgreSQL lokal,
  build QA frontend, dan WAV sintetis. Tidak menjalankan penulisan hosted DB.
- Pada iPhone fisik: gunakan HTTPS/localhost yang sesuai perangkat, izinkan mikrofon,
  rekam ucapan Indonesia, cek playback, buat/koreksi transkrip, preview/simpan, lalu
  ulangi dengan upload M4A dan perpindahan background/foreground. Hasil perangkat
  fisik ini masih perlu diverifikasi.

Artefak: [provider probes](../verification/ai-multimodal-live.json),
[live E2E/cleanup dan metrik](../verification/ai-live-e2e.json),
[ringkasan browser](../verification/multimodal-browser.json),
[keputusan implementasi](../ADR/0103-voice-transcription-and-multimodal-verification.md).
"""
    destination = ROOT / "specs/reports/2026-11-19-vn-multimodal-insight.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(message, encoding="utf-8")


if __name__ == "__main__":
    main()
