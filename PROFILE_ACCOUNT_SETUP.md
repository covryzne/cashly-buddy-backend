# Profil, keamanan akun, dan streak (ADR-0083)

Kode FE/BE tetap di branch dev. Target Auth dan database dipilih dari env yang
sama; file ini tidak berisi kredensial. Perubahan belum diterapkan ke staging.

## Jalankan migrasi

Dari root backend, Conda `cashly` aktif, setelah memeriksa bahwa DATABASE_URL,
MIGRATION_DATABASE_URL, dan SUPABASE_URL menunjuk project staging yang sama:

```cmd
python -m cashly check --env staging --env-source file
python -m cashly db-sql > migration-preview.sql
python -m cashly db-upgrade --env staging --env-source file
python -m cashly db-check --env staging --env-source file
python -m cashly dev --env staging --env-source file
```

Head baru `0004_profile_protection` setelah `0003_game_and_crud`. Wrapper sekarang
mendukung dev/staging, tetap memeriksa kecocokan project, dan menolak prod.
Preview db-sql mencakup seluruh chain; jangan mengeksekusi seluruh preview pada
database yang sudah memiliki revision. Gunakan db-upgrade untuk revision tersisa.

Migrasi menambahkan username tersimpan/unik, penanda password, tabel
streak_protections dan account_security_challenges beserta RLS. Nama awal akun
lama berbentuk cashly_1 dst.; akun baru mendapat username acak yang bisa diedit.
Jika cashly_runtime sudah ada, grants tabel baru otomatis ditambahkan. Jika role
dibuat setelah migrasi, jalankan SQL yang sudah ditinjau di sql/runtime_role.sql.
Tidak ada reset, akun demo baru, atau transaksi dummy yang dibuat oleh migrasi.
Data XP/max_stage lama dipertahankan untuk histori, tetapi tidak dipakai sebagai
sumber hero/reward. Protected dates lama dibawa ke tabel baru, beserta bulan
pemakaian menurut timezone akun. Biaya lama pada bulan berjalan ikut mengurangi
jatah lima tanggal. Pengeluaran dan identitas Auth dipertahankan.

Rollback tersedia lewat Alembic ke 0003_game_and_crud dengan env terpilih, tetapi
menghapus username baru, challenge dan catatan proteksi baru. Backup dahulu;
gunakan rollback hanya bersama versi FE/BE lama. Tidak dijalankan otomatis.

## Pengaturan Supabase staging

Custom SMTP Mailtrap yang sudah terpasang tetap digunakan. Tidak perlu menambah
token Mailtrap atau service-role key ke FE/BE untuk alur ini.

1. Pertahankan Confirm email aktif, OTP length 6, OTP expiry **300 detik**.
2. Aktifkan **Secure Email Change** supaya alamat lama dan baru sama-sama
   dikonfirmasi. BE juga menolak penyelesaian dengan hanya satu konfirmasi.
3. Authentication → Emails → Templates, isi subject dan Body Source, lalu Save:

| Template dashboard | Subject | File Body |
|---|---|---|
| Confirm sign up | Kode verifikasi akun Cashly | supabase/templates/confirm_signup.html |
| Change email address | Konfirmasi perubahan email Cashly | supabase/templates/change_email.html |
| Reset password | Konfirmasi kata sandi Cashly | supabase/templates/reset_password.html |

File lokal tidak otomatis memperbarui template hosted. Semua memakai .Token,
enam kotak, mascot publik, tanpa tombol redirect/clipboard yang tidak bisa
dijalankan klien email. OTP dimasukkan pada dialog Cashly yang masih terbuka.

Password memakai `/recover` → `/verify` type recovery → update password pada
Supabase user **yang sama**. Karena itu template-nya Reset password, bukan
Reauthentication. Nonce reauthentication native dapat dilewati oleh sesi baru;
recovery memberi bukti email juga sesaat setelah login Google. Sesi recovery
hanya berada di backend dan ditutup; tidak menjadi cookie browser. Kebijakan
password Supabase jangan bertentangan dengan kebijakan aplikasi 8–16 karakter.
Recovery kadang meminta verifikasi tambahan sesuai konfigurasi MFA provider;
MFA bukan bagian dari implementasi ini.

Referensi: [Email templates](https://supabase.com/docs/guides/auth/auth-email-templates),
[password updates](https://supabase.com/docs/reference/javascript/auth-updateuser),
[source pemeriksaan password/reauthentication](https://github.com/supabase/auth/blob/master/internal/api/user.go).

## Flow uji bersama

- Google → masuk langsung → Profil → Akun. Password belum disiapkan tidak
  menghalangi pemakaian. Buat kata sandi → isi/konfirmasi → kode email → simpan
  → dialog berhasil → login ulang dengan Google atau email/password. Identitas,
  username, dan data sama. Signup ulang tidak membuat Cashly account kedua;
  Supabase dapat memberikan respons generik untuk alamat yang telah terdaftar.
- Edit profil: nama dan username tersimpan; username tidak membedakan kapital,
  bentrok memberi pesan; email asli disabled. Tidak ada unggah foto/bio.
- Ganti email → email baru → kode dari email lama → kode dari email baru →
  dialog berhasil → login ulang. Email lama berlaku sampai dua konfirmasi selesai.
- OTP kedaluwarsa 5 menit; kirim ulang setelah 60 detik, maksimal 8 percobaan
  per challenge. Challenge baru menutup challenge sebelumnya. Provider juga
  menerapkan batas pengiriman. Jangan menyimpan kode/token di tiket atau log.
- Streak 30, lewat Selasa–Jumat, Sabtu: Pulihkan & check-in → konfirmasi biaya
  4 → hasil 31, sisa 1. Protected dates tidak menambah hari aktif. Jika sampai
  Minggu belum check-in/pulihkan, lima tanggal selesai terlewat: streak 0/hero 1.
  Check-in baru mulai dari 1. Bulan baru mendapat 5, tanpa akumulasi.

Seeder tetap eksplisit/lokal saja: scripts/seed_development.py, akun yang sama
dan 16 transaksi; 12 check-in, hero 1, jatah bulanan 5 untuk skenario baru.
Rerun tidak menghapus edit atau mengisi ulang jatah yang sudah digunakan.
Test otomatis memakai Postgres sementara dan provider loopback; pengiriman
Mailtrap/Google staging asli tetap diuji bersama setelah setup ini selesai.
