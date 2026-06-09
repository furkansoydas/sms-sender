"""Background worker that processes the SMS queue."""
import time
import threading
import uuid
from database import (
    get_next_queued, mark_queue_done,
    create_batch, add_sms_log, finalize_batch, log_action
)

_worker_thread = None
_stop_flag = threading.Event()


def process_queue_item(item: dict, sms_client) -> dict:
    """Process a single queue item; create batch + sms_log entries."""
    phones = item.get("phones", [])
    firmas = item.get("firmas", [])

    if not phones:
        mark_queue_done(item["id"], "failed", "Numara listesi boş")
        return {"success": False, "error": "no_phones"}

    batch_id = create_batch(
        batch_uid=item["batch_uid"],
        username=item["username"],
        message=item["message"],
        recipients=len(phones),
        send_type=item.get("send_type", "bulk"),
    )

    if sms_client is None:
        # No SMS client → mark as failed
        for i, p in enumerate(phones):
            firma = firmas[i] if i < len(firmas) else ""
            add_sms_log(batch_id, p, firma, "failed", "Verimor API ayarları eksik")
        finalize_batch(batch_id, 0, len(phones), "failed", "SMS client yok")
        mark_queue_done(item["id"], "failed", "SMS client yok")
        log_action(item["username"], "sms_send_failed", f"Batch {item['batch_uid']}: API yok", status="error")
        return {"success": False, "error": "no_client"}

    try:
        result = sms_client.send_to_phones(phones, item["message"])
        is_ok = result["status"] == 200
        status = "success" if is_ok else "failed"
        for i, p in enumerate(phones):
            firma = firmas[i] if i < len(firmas) else ""
            add_sms_log(batch_id, p, firma, status, result.get("response", "")[:500])

        if is_ok:
            finalize_batch(batch_id, len(phones), 0, "completed")
            mark_queue_done(item["id"], "completed")
            log_action(item["username"], "sms_sent",
                       f"Batch {item['batch_uid']}: {len(phones)} numara")
            return {"success": True, "batch_id": batch_id}
        else:
            finalize_batch(batch_id, 0, len(phones), "failed", str(result.get("response", "")))
            mark_queue_done(item["id"], "failed", str(result.get("response", "")))
            log_action(item["username"], "sms_send_failed",
                       f"Batch {item['batch_uid']}: HTTP {result['status']}", status="error")
            return {"success": False, "error": "api_error"}
    except Exception as e:
        for i, p in enumerate(phones):
            firma = firmas[i] if i < len(firmas) else ""
            add_sms_log(batch_id, p, firma, "failed", str(e)[:500])
        finalize_batch(batch_id, 0, len(phones), "failed", str(e)[:500])
        mark_queue_done(item["id"], "failed", str(e)[:500])
        log_action(item["username"], "sms_send_error", str(e)[:200], status="error")
        return {"success": False, "error": str(e)}


def _worker_loop(get_sms_client_fn):
    """Main worker loop — polls queue every 3 seconds."""
    while not _stop_flag.is_set():
        try:
            item = get_next_queued()
            if item:
                sms_client = get_sms_client_fn()
                process_queue_item(item, sms_client)
                # No sleep between items — process as fast as possible
                continue
        except Exception as e:
            print(f"[QUEUE WORKER ERROR] {e}")
        # Idle: poll every 3s
        _stop_flag.wait(3)


def start_worker(get_sms_client_fn):
    """Start background worker thread (idempotent)."""
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_flag.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, args=(get_sms_client_fn,), daemon=True
    )
    _worker_thread.start()
    print("[QUEUE WORKER] started")


def stop_worker():
    _stop_flag.set()


def generate_batch_uid() -> str:
    return uuid.uuid4().hex[:12]
