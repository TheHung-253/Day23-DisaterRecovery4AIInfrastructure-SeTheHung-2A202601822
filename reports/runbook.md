# Runbook 1 trang — Region chính down

Runbook này phục vụ vận hành on-call lúc 3h sáng khi Region chính gặp sự cố (outage).

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `curl -s localhost:8001/readyz \| jq .ready` | Trả về `false` hoặc connection timeout/refused $\ge 3$ lần | On-call Engineer |
| 2 | Mở incident + bấm giờ RTO | `python3 -c "import time; print('INCIDENT_OPENED_AT:', time.time())"` | Timestamp được ghi nhận vào incident channel và `reports/runbook-run.jsonl` | Incident Commander |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | Output JSON có `restored_at` và `embed_model_version` hợp lệ | Storage Lead / On-call |
| 4 | Scale pool warm $\rightarrow$ full | `echo "full" > state/region-b/pool_state && curl -s localhost:8002/readyz \| jq .ready` | `/readyz` của Region B trả về HTTP 200 và `"ready": true` | Infrastructure Lead |
| 5 | DNS/LB cutover | `printf "b" > edge/active_region && curl -s localhost:8080/edge/state` | Endpoint trả về `{"active_region": "b", ...}` | Traffic / Network Lead |
| 6 | Verify golden signals | `for i in {1..10}; do curl -s localhost:8080/v1/infer; done` | Toàn bộ 10 requests trả về status 200, `edge_region="b"`, error rate = 0%, p95 < 100ms | QA / On-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Kết quả trả về `"rto_verdict": "PASS"`, RTO $\le 300$s | Incident Commander |

## Điều kiện Rollback (Failover ngược về Region A)

**Điều kiện bắt buộc trước khi chuyển traffic về Region A:**
1. Region A đã được khôi phục hoàn toàn: tiến trình sống (`/healthz` 200), vector DB đã được đồng bộ dữ liệu mới nhất phát sinh từ Region B trong suốt thời gian outage (`state/snapshot.py put --region b` $\rightarrow$ `get --region a`).
2. `/readyz` của Region A trả về HTTP 200 liên tục trong ít nhất 5 phút (tránh flapping).
3. Đã chạy 20 test probe thử nghiệm trực tiếp vào Region A với tỷ lệ thành công 100%.

**Thẩm quyền quyết định rollback:** Incident Commander phê duyệt sau khi có xác nhận từ Infrastructure Lead.
*(Theo §4 Anti-Patterns: Tuyệt đối không bật chế độ full-auto failback để tránh hiện tượng split-brain hoặc flapping hai chiều liên tục).*
