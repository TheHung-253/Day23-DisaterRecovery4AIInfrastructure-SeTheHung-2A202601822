# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây trỏ về **một dòng log thật** (`đường/dẫn.jsonl:số_dòng`).

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T09:09:54` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.1s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | `+0.1s` | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | `+15.0s` | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | `+15.3s` | `step:2_restore_snapshot` | `reports/failover-events.jsonl:6` |
| Region phụ ready | `+15.3s` | `step:4_wait_ready` | `reports/failover-events.jsonl:8` |
| DNS cutover | `+15.3s` | `step:5_dns_cutover` | `reports/failover-events.jsonl:9` |
| **RTO đo được** | `16.3s` | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:33` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `16.3s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `4.01s` / `2` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | `15.0s` | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm interval (ví dụ 2s) hoặc threshold (2), nhưng tăng nguy cơ flapping khi mạng chập chờn |
| Snapshot restore | `0.0s` | 2_restore → 3_scale trong `reports/failover-events.jsonl:6` | Tối ưu I/O disk, dùng snapshot replication định kỳ ngắn hơn hoặc warm standby data |
| GPU pool warm-up | `0.0s` | `waited_s` ở `4_wait_ready` trong `reports/failover-events.jsonl:8` | Pre-warm GPU instances hoặc duy trì pool_state=warm thay vì cold boot |
| DNS/LB TTL cache | `1.0s` | t_recovered − t_cutover trong `reports/drill-2-withdr.jsonl:33` | Giảm DNS TTL (EDGE_TTL_SECONDS) trên Global Load Balancer / Route53 |
