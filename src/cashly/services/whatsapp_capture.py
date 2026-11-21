"""Process one verified WhatsApp event and atomically persist its outcome."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from starlette.concurrency import run_in_threadpool

from cashly.core.errors import ApiError
from cashly.db.models import Account, Expense, ExpenseCategory, ExpenseItem, Profile
from cashly.db.whatsapp_models import WhatsAppCaptureJob as Job
from cashly.repositories.accounts import set_account_context
from cashly.schemas.extraction import ChatEntry, ChatExtractInput
from cashly.schemas.receipt import ReceiptExtractInput, ReceiptImage
from cashly.schemas.voice import VoiceAudio, VoiceExtractInput
from cashly.services import gamification
from cashly.services import whatsapp as wa
from cashly.services.chat_extraction import extract_chat, logger
from cashly.services.receipt_extraction import extract_receipt, validate_image
from cashly.services.voice_audio import normalize_audio

RETRY = "Pesan ini belum bisa diproses. Tunggu sebentar, lalu kirim lagi, ya."
REDIRECT_CONTEXT_TTL = timedelta(minutes=10)
PAYMENTS = {
    "Tunai": "cash",
    "QRIS": "qris",
    "Transfer bank": "bank_transfer",
    "Kartu debit": "debit_card",
    "Kartu kredit": "credit_card",
    "Dompet digital": "e_wallet",
}


def outcome(job_id, event_id, status, reply=None):
    return {
        "data": {
            "job_id": job_id,
            "event_id": event_id,
            "status": status,
            "committed": status == "saved",
            "reply": reply,
        }
    }


def replay(session, settings, job):
    out = session.scalar(select(wa.Outbox).where(wa.Outbox.event_id == job.event_id))
    if out and out.state == "ready" and out.payload_cipher:
        reply = wa.decrypt(settings, out.payload_cipher)["reply"]
        return outcome(job.id, job.event_id, job.state, reply)
    return outcome(job.id, job.event_id, "ignored")


def complete(session, settings, job, event, sender, now, status, message):
    out = wa.Outbox(
        event_id=event.id,
        account_id=event.account_id,
        created_at=now,
        expires_at=now + wa.TTL,
        state="ready",
    )
    session.add(out)
    session.flush()
    reply = {"id": str(out.id), "text": message[:4000]}
    out.payload_cipher = wa.encrypt(
        settings,
        {
            "event_id": str(event.id),
            "bot_id": event.bot_id,
            "message_id": event.message_id,
            "sender_id": sender,
            "kind": job.kind,
            "verified_link": True,
            "action": "reply",
            "reply": reply,
        },
    )
    job.state, job.input_cipher, job.lease_id, job.lease_until = status, None, None, None
    return outcome(job.id, event.id, status, reply)


def active_profile(session, channel_id, account_id, *, lock=False):
    channel = session.get(wa.Channel, channel_id)
    if not channel or channel.account_id != account_id:
        return None
    set_account_context(session, account_id)
    query = select(Account).where(Account.id == account_id)
    account = session.scalar(query.with_for_update() if lock else query)
    return session.get(Profile, account_id) if account and account.status == "active" else None


def expire_jobs(session, settings, now):
    session.execute(
        update(Job)
        .where(
            or_(Job.context_cipher.is_not(None), Job.redirect_context_cipher.is_not(None)),
            Job.expires_at <= now,
        )
        .values(context_cipher=None, redirect_context_cipher=None)
    )
    jobs = session.scalars(
        select(Job)
        .where(
            Job.state.in_(("ready", "running")),
            or_(Job.expires_at <= now, and_(Job.state == "running", Job.lease_until <= now)),
        )
        .order_by(Job.expires_at)
        .limit(20)
    ).all()
    for job in jobs:
        event = session.get(wa.Event, job.event_id)
        if active_profile(session, job.channel_id, event.account_id):
            sender = wa.decrypt(settings, job.input_cipher)["sender_id"]
            complete(session, settings, job, event, sender, now, "failed", RETRY)
        else:
            job.state, job.input_cipher, job.lease_id, job.lease_until = "ignored", None, None, None


def begin(db, settings, job_id, body):
    with wa.transaction(db, settings) as (session, now, _):
        job = session.get(Job, job_id)
        if not job:
            return outcome(job_id, body.event_id, "ignored"), None
        if job.event_id != body.event_id or job.kind != body.kind:
            raise ApiError(409, "WHATSAPP_JOB_SCOPE", "Permintaan tidak sesuai dengan pesan.")
        event = session.get(wa.Event, job.event_id)
        profile = active_profile(session, job.channel_id, event.account_id)
        if not profile or not settings.whatsapp_recording_enabled:
            return outcome(job.id, event.id, "ignored"), None
        if job.state not in {"ready", "running"}:
            return replay(session, settings, job), None
        data = wa.decrypt(settings, job.input_cipher)
        if job.expires_at <= now or (job.state == "running" and job.lease_until <= now):
            return complete(
                session, settings, job, event, data["sender_id"], now, "failed", RETRY
            ), None
        if job.state == "running":
            return outcome(job.id, event.id, "queued"), None
        running = session.scalar(
            select(Job.id)
            .where(
                Job.channel_id == job.channel_id,
                Job.state == "running",
                Job.lease_until > now,
                Job.expires_at > now,
            )
            .limit(1)
        )
        if running is not None:
            message = (
                "Mochi masih memproses pesan sebelumnya. Kirim pesan ini lagi setelah selesai, ya."
            )
            return complete(
                session, settings, job, event, data["sender_id"], now, "failed", message
            ), None

        previous = session.scalar(
            select(Job)
            .where(
                Job.channel_id == job.channel_id,
                Job.context_cipher.is_not(None),
                Job.expires_at > now,
            )
            .order_by(Job.expires_at.desc())
            .limit(1)
        )
        if previous and job.kind != "text":
            message = (
                "Jawab dulu pertanyaan Mochi sebelumnya lewat chat, ya. "
                "Setelah itu, kirim media ini lagi."
            )
            return complete(
                session, settings, job, event, data["sender_id"], now, "needs_review", message
            ), None
        context = wa.decrypt(settings, previous.context_cipher) if previous else None
        history = []
        if job.kind == "text":
            recent = session.scalars(
                select(Job.redirect_context_cipher)
                .where(
                    Job.channel_id == job.channel_id,
                    Job.redirect_context_cipher.is_not(None),
                    Job.expires_at > now,
                )
                .order_by(Job.expires_at.desc(), Job.id.desc())
                .limit(2)
            ).all()
            for pair in reversed(recent):
                history.extend(wa.decrypt(settings, pair))
        job.state, job.lease_id, job.lease_until = "running", uuid4(), now + wa.LEASE
        job.expires_at = max(job.expires_at, job.lease_until)
        categories = session.scalars(select(ExpenseCategory).where(ExpenseCategory.is_active)).all()
        return None, {
            "lease": job.lease_id,
            "data": data,
            "timezone": profile.timezone,
            "categories": {category.code: category.label_id for category in categories},
            "context": context,
            "history": history,
            "previous_id": previous.id if previous else None,
        }


async def infer(settings, client, body, work, request_id):
    context, data = work["context"], work["data"]
    options = (work["categories"], work["timezone"], request_id)
    if body.kind == "text":
        if not data["text"] or len(data["text"]) > 1000:
            raise ApiError(422, "INVALID_CHAT", "Kirim catatan singkat, maksimal 1.000 karakter.")
        request = ChatExtractInput(
            message=data["text"],
            entries=context["entries"] if context else [],
            history=work["history"],
        )
        return await extract_chat(settings, client, request, *options)
    if body.input_error or body.media is None:
        raise ApiError(422, "INVALID_MEDIA", "Medianya belum bisa dibaca. Coba kirim ulang, ya.")
    media = body.media.model_dump()
    if body.kind == "receipt":
        image = ReceiptImage.model_validate(media)
        await run_in_threadpool(validate_image, image)
        request = ReceiptExtractInput(image=image, caption=data["caption"] or "")
        return await extract_receipt(settings, client, request, *options)
    audio = await run_in_threadpool(normalize_audio, VoiceAudio.model_validate(media))
    transcript = await extract_chat(
        settings,
        client,
        ChatExtractInput(message="Transkripsikan rekaman ini secara harfiah."),
        {},
        work["timezone"],
        request_id,
        media=audio,
    )
    if transcript.status != "clear":
        raise ApiError(
            422,
            "UNCLEAR_AUDIO",
            "VN-nya belum terdengar jelas. Coba rekam ulang atau ketik catatanmu.",
        )
    request = VoiceExtractInput(message=transcript.transcript)
    return await extract_chat(settings, client, request, *options, input_kind="voice")


def record_expenses(session, account_id, timezone, event, job, result, work, now):
    pending = work["context"]
    earlier = {item["id"] for item in pending["entries"]} if pending else set()
    rows = []
    for index, entry in enumerate(result.entries):
        draft = entry.draft
        occurred = datetime.fromisoformat(f"{draft.occurredDate}T{draft.occurredTime}").replace(
            tzinfo=ZoneInfo(timezone)
        )
        category = session.get(ExpenseCategory, draft.categoryCode)
        if not category or not category.is_active or occurred > now:
            raise ApiError(409, "INVALID_DRAFT", "Periksa tanggal atau kategori pengeluaranmu, ya.")
        kind = (
            pending["kinds"].get(str(entry.id), job.kind) if str(entry.id) in earlier else job.kind
        )
        subtotal = sum(int(item.quantity) * int(item.priceIdr) for item in draft.items)
        row = Expense(
            id=uuid4(),
            account_id=account_id,
            merchant=draft.merchant,
            description=draft.merchant or draft.items[0].name,
            category_code=draft.categoryCode,
            occurred_at=occurred,
            payment_method=PAYMENTS.get(draft.paymentMethod, "unknown"),
            amount_idr=subtotal + int(draft.taxIdr) - int(draft.discountIdr),
            discount_idr=int(draft.discountIdr),
            tax_idr=int(draft.taxIdr),
            source_platform="whatsapp",
            source_type=kind,
            source_message_id=event.message_id,
            source_item_index=index,
            timestamp_basis="receipt" if kind == "receipt" else "user_explicit",
        )
        rows.append((row, draft.items))
    for row, items in rows:
        session.add(row)
        session.flush()
        for index, item in enumerate(items):
            quantity, price = int(item.quantity), int(item.priceIdr)
            session.add(
                ExpenseItem(
                    expense_id=row.id,
                    item_index=index,
                    label=item.name,
                    quantity=quantity,
                    unit_price_idr=price,
                    amount_idr=quantity * price,
                )
            )
        gamification.on_expense(session, account_id, timezone, row.occurred_at)
    total = sum(row.amount_idr for row, _ in rows)
    rupiah = f"{total:,}".replace(",", ".")
    return f"Tersimpan di Cashly: {len(rows)} pengeluaran, total Rp{rupiah}."


def finish(db, settings, job_id, body, work, result, failure=None):
    with wa.transaction(db, settings) as (session, now, _):
        job = session.get(Job, job_id)
        if not job:
            return outcome(job_id, body.event_id, "ignored")
        event = session.get(wa.Event, job.event_id)
        profile = active_profile(session, job.channel_id, event.account_id, lock=True)
        if not profile or job.state != "running" or job.lease_id != work["lease"]:
            return outcome(job.id, event.id, "ignored")
        sender = work["data"]["sender_id"]
        if job.lease_until <= now or job.expires_at <= now:
            failure = RETRY
        if failure:
            return complete(session, settings, job, event, sender, now, "failed", failure)
        pending = work["context"]
        previous = session.get(Job, work["previous_id"]) if work["previous_id"] else None
        prior_entries = (
            [ChatEntry.model_validate(item) for item in pending["entries"]] if pending else []
        )
        changed = result.entries != prior_entries
        if (
            result.intent == "record"
            and changed
            and result.entries
            and all(entry.draft is not None for entry in result.entries)
        ):
            message = record_expenses(
                session, event.account_id, work["timezone"], event, job, result, work, now
            )
            if previous:
                previous.context_cipher = None
            return complete(session, settings, job, event, sender, now, "saved", message)
        if changed and result.entries:
            kinds = pending["kinds"] if pending else {}
            job.context_cipher = wa.encrypt(
                settings,
                {
                    "entries": [item.model_dump(mode="json") for item in result.entries],
                    "kinds": {
                        str(item.id): kinds.get(str(item.id), job.kind) for item in result.entries
                    },
                },
            )
            job.expires_at = now + timedelta(minutes=30)
            if previous:
                previous.context_cipher = None
        message = (
            result.reply.removesuffix("Periksa hasilnya lewat Tinjau sebelum Simpan.").strip()
            or "Aku bisa bantu mencatat pengeluaranmu. Coba kirim ‘kopi 18rb’."
        )
        if job.kind == "text" and result.intent == "out_of_scope":
            job.redirect_context_cipher = wa.encrypt(
                settings,
                [
                    {"role": "user", "text": work["data"]["text"]},
                    {"role": "assistant", "text": message},
                ],
            )
            job.expires_at = now + REDIRECT_CONTEXT_TTL
        return complete(session, settings, job, event, sender, now, "needs_review", message)


async def process(db, settings, client, job_id, body, request_id):
    response, work = await run_in_threadpool(begin, db, settings, job_id, body)
    if work is None:
        return response
    result, failure = None, None
    try:
        result = await infer(settings, client, body, work, request_id)
    except (ApiError, ValidationError) as error:
        logger.info(
            "whatsapp_capture_failed",
            extra={
                "request_id": request_id,
                "input_kind": body.kind,
                "outcome": error.code if isinstance(error, ApiError) else "invalid_input",
            },
        )
        failure = (
            error.public_message
            if isinstance(error, ApiError) and error.status_code in {413, 422}
            else RETRY
        )
    try:
        return await run_in_threadpool(finish, db, settings, job_id, body, work, result, failure)
    except ApiError:
        return await run_in_threadpool(finish, db, settings, job_id, body, work, None, RETRY)
