# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: tập trung vào việc hoàn thiện quy trình và hệ thống, không quy trách nhiệm cá nhân.

## 1. Timeline (mọi dòng trỏ về evidence path:line thật)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T09:11:57 | Outage bắt đầu (Chaos kill Region A) | `chaos/chaos-events.jsonl:3` |
| 2026-08-25T09:11:57 | User đầu tiên bị ảnh hưởng (Request timeout/error) | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T09:12:12 | Health check alert (`to: UNHEALTHY, region: a`) | `reports/health-events.jsonl:2` |
| 2026-08-25T09:12:12 | Operator confirm & kích hoạt failover | `reports/runbook-run.jsonl:2` |
| 2026-08-25T09:12:12 | DNS cutover sang Region B hoàn tất | `reports/failover-events.jsonl:9` |
| 2026-08-25T09:12:13 | Resolved (Request đầu tiên thành công từ Region B) | `reports/drill-2-withdr.jsonl:33` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `16.3s` · gap: `283.7s` (vượt mục tiêu an toàn).
- RPO mục tiêu: 300s · đo được: `4.01s` (`2` docs bị mất) · gap: `295.99s`.
- **Bước tốn nhiều giây nhất:** `Health check detection floor` (15.0s) — chiếm ~92% tổng thời gian RTO (15.0s / 16.3s). Lý do: Cấu hình `interval=5.0s` kết hợp với `threshold=3` yêu cầu hệ thống phải quan sát 3 lần thất bại liên tiếp mới khẳng định outage, tránh tình trạng kích hoạt failover giả khi mạng chập chờn.

## 3. Root cause (5 Whys)

1. *Tại sao user nhận lỗi 503/timeout?* $\rightarrow$ Do Region A bị cô lập mạng (network partition / SIGSTOP do chaos injection).
2. *Tại sao traffic vẫn được gửi đến Region A?* $\rightarrow$ Do Edge proxy vẫn trỏ `active_region=a` theo thiết lập ban đầu.
3. *Tại sao hệ thống không cutover ngay lập tức ở giây đầu tiên?* $\rightarrow$ Do cơ chế anti-flapping yêu cầu 3 lần probe thất bại liên tiếp ($3 \times 5\text{s} = 15\text{s}$) trước khi chuyển cờ UNHEALTHY.
4. *Tại sao Region B cần khôi phục dữ liệu trước khi nhận traffic?* $\rightarrow$ Do Region B hoạt động ở mô hình warm-standby để tiết kiệm chi phí tài nguyên, cần nạp snapshot Vector DB và kiểm tra phiên bản model embedding.
5. *Tại sao sau khi cutover hệ thống phục hồi ngay lập tức?* $\rightarrow$ Do quy trình tự động hóa đã hoàn tất restore snapshot, scale pool và cập nhật DNS pointer trong vòng ~1.3s sau khi phát hiện outage.

## 4. Action items (có owner + deadline)

| # | Action item | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Điều chỉnh cấu hình health check xuống `interval=3s`, `threshold=3` cho các critical endpoints | SRE Team | 2026-09-01 | Giảm RTO ~6s (detection floor từ 15s xuống 9s) |
| 2 | Giảm chu kỳ replication snapshot xuống `every=10s` cho Vector DB | Data Platform Team | 2026-09-05 | Giảm RPO tối đa từ 30s xuống 10s |
| 3 | Tự động hóa kiểm tra tương thích phiên bản embedding model trước khi restore | MLOps Team | 2026-09-10 | Ngăn ngừa lỗi mismatch model weight |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - Giá trị: $5.0\text{s} \times 3 = 15.0\text{s}$.
   - Tỷ lệ: Chiếm $\approx 92.0\%$ tổng thời gian RTO đo được (15.0s / 16.3s).

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - Nếu hạ xuống 1s (`1s × 3 = 3s`), RTO giảm được $15\text{s} - 3\text{s} = 12\text{s}$.
   - Cái giá phải trả: Nguy cơ **flapping** rất cao khi mạng bị jitter, packet loss tạm thời hoặc spike load ngắn hạn. Hệ thống sẽ phát tín hiệu cảnh báo sai (false positive), gây kích hoạt failover qua lại liên tục giữa 2 region, dẫn đến gián đoạn kết nối, mất đồng bộ dữ liệu và tăng tải đột biến cho storage.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - `docs_lost` (2 documents trong drill này) đại diện cho các bản ghi/vector được ingest vào Region A trong khoảng thời gian giữa lần snapshot cuối cùng và thời điểm xảy ra sự cố.
   - Với khách hàng, điều này đồng nghĩa với việc các tương tác, tài liệu hoặc giao dịch phát sinh trong cửa sổ thời gian 4.01s đó bị mất vĩnh viễn và không thể truy vấn lại trong hệ thống RAG/Search. Khách hàng sẽ phải tải lại dữ liệu đó hoặc hệ thống cần cơ chế Event Log/WAL dự phòng để replay lại các event bị thiếu.
