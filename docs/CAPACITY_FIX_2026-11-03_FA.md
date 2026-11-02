# اصلاح ظرفیت پس از آزمون native ASGI

هدف: رفع رقابت شروع اجرای یکسان و تعیین محل انتظار پیش از تغییر تنظیمات میزبان.

کد پایه: PR #25، e2b725cd0e1924d2dfcbb16f3cbb19d59496bd64. Staging: LiteSpeed → loopback Uvicorn دو worker؛ Production همچنان Passenger. گزارش قبلی در PR #26 ثبت شده است.

## تغییرات

- PostgreSQL: قفل تراکنشی شناسهٔ start_request_id و قفل ردیف assignment پیش از خواندن ledger/progress؛ SQLite: BEGIN IMMEDIATE پیش از خواندن.
- آزمون رقابتی درخواست یکسان، شناسهٔ تازه و حفظ run قبلی در SQLite و PostgreSQL مستقل CI.
- middleware خالص ASGI و ContextVar برای اندازه‌گیری درخواست و DB؛ پیش‌فرض خاموش، فعال‌سازی فقط Staging. لاگ خصوصی و چرخشی Supervisor، بدون هویت یا payload.
- بستهٔ runtime اکنون request_metrics.py را هم لازم دارد. launcher مقید به hash باید همراه فایل برنامه به hash نسخهٔ تست‌شده به‌روزرسانی شود؛ restart با Supervisor انجام می‌شود.

## محدودیت فعلی

health اصلی در شروع این نوبت بدون بار timeout داشت. بار سنگین تا تأیید سلامت پایه یا رفع علت متوقف است. هیچ reset یا حذف داده‌ای مجاز نیست و تست PostgreSQL smoke هرگز روی دیتابیس اصلی یا Staging اجرا نمی‌شود؛ فقط دیتابیس disposable CI.
