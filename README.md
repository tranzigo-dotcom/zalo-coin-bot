# Zalo Coin Bot — 1 kèo coin mỗi ngày lúc 21:00

Mỗi tối 21:00 (giờ VN) bot gửi vào Zalo: kết quả kèo hôm qua, 1 kèo mới (xác suất chạm +5% trong 24h, phân tích kỹ thuật, bối cảnh thị trường) và chart. Sau 30 ngày gửi bản tổng kết rồi tự dừng.

Chạy miễn phí trên GitHub Actions.

## Chạy tay
Tab **Actions** → **Zalo coin bot** → **Run workflow** → chọn mode:
- `chatid`: lấy chat_id (nhắn cho bot trước)
- `test`: gửi 1 kèo thử ngay
- `daily`: chạy như thật

## Secrets cần có
`ZALO_BOT_TOKEN`, `ZALO_CHAT_ID` (Settings → Secrets and variables → Actions).

## Nhật ký
Mọi kèo và kết quả lưu ở `data/picks.csv`.

⚠️ Công cụ thử nghiệm thống kê, không phải lời khuyên đầu tư.
