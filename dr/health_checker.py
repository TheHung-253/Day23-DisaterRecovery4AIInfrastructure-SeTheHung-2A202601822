"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import datetime
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    url = f"{URL[region]}/readyz"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(url)
            if r.status_code == 200:
                data = r.json()
                if data.get("ready"):
                    return True, "ok"
                reasons = ",".join(data.get("reasons", [])) or "not_ready"
                return False, reasons
            else:
                try:
                    data = r.json()
                    reasons = ",".join(data.get("reasons", [])) or f"status_{r.status_code}"
                except Exception:
                    reasons = f"status_{r.status_code}"
                return False, reasons
    except httpx.TimeoutException:
        return False, "timeout"
    except httpx.ConnectError:
        return False, "connect_error"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL."""
    out.parent.mkdir(parents=True, exist_ok=True)
    state = {
        r: {
            "current": "HEALTHY",
            "consecutive_fails": 0,
            "consecutive_success": 0,
        }
        for r in URL
    }

    start_time = time.time()
    while time.time() - start_time < duration:
        loop_start = time.time()
        for region in list(URL.keys()):
            ready, reason = probe(region, timeout)
            st = state[region]

            if ready:
                st["consecutive_success"] += 1
                st["consecutive_fails"] = 0
                if st["current"] == "UNHEALTHY" and st["consecutive_success"] >= threshold:
                    prev = st["current"]
                    st["current"] = "HEALTHY"
                    ev = {
                        "ts": time.time(),
                        "iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "event": "state_change",
                        "region": region,
                        "from": prev,
                        "to": "HEALTHY",
                        "reason": reason,
                        "consecutive_fails": 0,
                        "interval_s": interval,
                        "threshold": threshold,
                    }
                    with out.open("a") as f:
                        f.write(json.dumps(ev) + "\n")
                    print(f"[{ev['iso']}] {region}: {prev} -> HEALTHY ({reason})")
            else:
                st["consecutive_fails"] += 1
                st["consecutive_success"] = 0
                if st["current"] == "HEALTHY" and st["consecutive_fails"] >= threshold:
                    prev = st["current"]
                    st["current"] = "UNHEALTHY"
                    ev = {
                        "ts": time.time(),
                        "iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "event": "state_change",
                        "region": region,
                        "from": prev,
                        "to": "UNHEALTHY",
                        "reason": reason,
                        "consecutive_fails": st["consecutive_fails"],
                        "interval_s": interval,
                        "threshold": threshold,
                    }
                    with out.open("a") as f:
                        f.write(json.dumps(ev) + "\n")
                    print(f"[{ev['iso']}] {region}: {prev} -> UNHEALTHY ({reason}, fails={st['consecutive_fails']})")

        elapsed = time.time() - loop_start
        sleep_time = max(0.0, interval - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
