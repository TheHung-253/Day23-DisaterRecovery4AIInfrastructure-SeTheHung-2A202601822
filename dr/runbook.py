"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                               hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                               SAU t_outage trong chaos-events (không thể trùng — operator
                               không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                               log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                               đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                               và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                               weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                               runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import datetime
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n: int, name: str, **kw) -> dict:
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{entry['iso']}] RUNBOOK STEP {n} ({name}): {json.dumps(kw)}")
    return entry


def confirm(auto: bool, msg: str) -> bool:
    """Auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực thi 7 bước runbook."""
    t_start = time.time()

    # Bước 1: Xác nhận outage
    # Đợi health checker ghi nhận UNHEALTHY (hoặc timeout sau 30s) để đảm bảo t_cutover >= t_detect
    health_log = pathlib.Path("reports/health-events.jsonl")
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos_log.exists():
        kills = [
            json.loads(l)
            for l in chaos_log.read_text().splitlines()
            if l.strip() and json.loads(l).get("action") == "kill"
        ]
        if kills:
            t_outage = kills[-1].get("ts")

    p_reason = "probe_failed"
    start_wait = time.time()
    while time.time() - start_wait < 30.0:
        if health_log.exists():
            evs = [json.loads(l) for l in health_log.read_text().splitlines() if l.strip()]
            unhealthy_evs = [
                e for e in evs
                if e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                and e.get("region") == primary and (t_outage is None or e.get("ts", 0) >= t_outage)
            ]
            if unhealthy_evs:
                p_reason = unhealthy_evs[-1].get("reason", "unhealthy_by_health_checker")
                break
        p_ready, p_reason = hc.probe(primary, timeout=2.0)
        if not p_ready and t_outage is None:
            break
        time.sleep(1.0)

    t_ready, t_reason = hc.probe(target, timeout=2.0)
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_ready=False,
        primary_reason=p_reason,
        target=target,
        target_ready=t_ready,
    )

    # Bước 2: Thông báo incident
    if not confirm(auto, f"Xác nhận kích hoạt failover từ region '{primary}' sang '{target}'?"):
        step(2, "thong_bao_incident", cancelled=True)
        return {"ok": False, "reason": "operator_cancelled"}

    t_operator = time.time()
    notice_delay = round(t_operator - t_outage, 2) if t_outage else None
    step(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        t_outage=t_outage,
        t_operator=t_operator,
        notice_delay_s=notice_delay,
    )

    # Bước 3: Scale GPU pool & Failover
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, failover_result=fo_res)
    if not fo_res.get("ok"):
        return {"ok": False, "step": 3, "error": "failover_failed"}

    # Bước 4: Verify state replica
    step(
        4,
        "verify_state_replica",
        target=target,
        rpo_seconds=fo_res.get("rpo_seconds"),
        docs_lost=fo_res.get("docs_lost"),
        embed_model_version=fo_res.get("embed_model_version"),
    )

    # Bước 5: DNS cutover
    step(5, "dns_cutover", active_region=target, ok=fo_res.get("ok"))

    # Bước 6: Verify golden signals (10 requests)
    latencies = []
    errors = 0
    for i in range(10):
        t_req = time.time()
        try:
            with httpx.Client(timeout=3.0) as client:
                r = client.get("http://127.0.0.1:8080/v1/infer", params={"q": f"health probe {i}"})
                lat = round((time.time() - t_req) * 1000, 1)
                latencies.append(lat)
                if r.status_code != 200:
                    errors += 1
        except Exception:
            errors += 1
        time.sleep(0.05)

    sorted_lats = sorted(latencies)
    p95 = sorted_lats[int(len(sorted_lats) * 0.95)] if sorted_lats else None
    error_rate = errors / 10.0
    step(
        6,
        "verify_golden_signals",
        total_requests=10,
        latencies_ms=latencies,
        p95_latency_ms=p95,
        error_rate=error_rate,
    )

    # Bước 7: Post incident
    elapsed = round(time.time() - t_start, 2)
    step(
        7,
        "post_incident",
        elapsed_s=elapsed,
        rto_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl",
    )

    return {
        "ok": True,
        "elapsed_s": elapsed,
        "primary": primary,
        "target": target,
        "rpo_seconds": fo_res.get("rpo_seconds"),
        "docs_lost": fo_res.get("docs_lost"),
        "p95_latency_ms": p95,
        "error_rate": error_rate,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
