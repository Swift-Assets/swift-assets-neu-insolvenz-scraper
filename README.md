# سكرابر إعلانات الإفلاس الألمانية

سكرابر مباشر لموقع [neu.insolvenzbekanntmachungen.de](https://neu.insolvenzbekanntmachungen.de/) يعمل يومياً عبر GitHub Actions ويكتب البيانات في Supabase.

## الميزات

- ✅ **مجاني تماماً** — يعمل ضمن الحد المجاني لـ GitHub Actions (2000 دقيقة/شهر)
- ✅ **يومي تلقائي** — كل يوم الساعة 00:05 بتوقيت برلين (بعد انتهاء المحاكم من النشر)
- ✅ **سحب ذكي** — يتخطى السجلات الموجودة مسبقاً ليوفر الوقت والتكلفة
- ✅ **تحديث آمن** — يحدّث الحقول الفارغة فقط، ولا يستبدل البيانات الموجودة
- ✅ **متوازي** — 4 عمّال متوازيين لسحب التفاصيل بسرعة (~1 ثانية/سجل)
- ✅ **إشعارات الفشل** — يفتح GitHub Issue تلقائياً عند فشل التشغيل

## التكلفة المتوقعة

| البند | التكلفة |
|------|---------|
| GitHub Actions | **مجاني** (272 دقيقة/شهر متوسطاً، الحد 2000) |
| Supabase | كما هو (نفس الجدول الموجود) |
| **الإجمالي** | **0 € شهرياً** |

مقارنة بالإعداد السابق على Apify (~600 €/شهر).

## البنية التقنية

### المرحلة الأولى: البحث (طلب واحد)
```
POST /ap/suche.jsf → الحصول على ~6500 سجل في 6 ثواني
```

### المرحلة الثانية: سحب التفاصيل (طلبان لكل سجل جديد فقط)
```
POST /ap/ergebnis.jsf  (AJAX click)
GET  /ap/text.xhtml    (النص الكامل)
```

### استخراج الحقول من النص
- اسم مدير الإفلاس (`insolvency_administrator`)
- تاريخ الافتتاح (`opening_date`)
- موعد تقديم المطالبات (`claims_deadline`)
- نوع الإعلان (`announcement_type_hint`: Eröffnung / Aufhebung / etc.)
- المحكمة والسجل التجاري (HRA/HRB/PR/VR + رقم)

### الكتابة في Supabase
يستخدم RPC مخصص `neu_insolvenz_fill_only_upsert` يضمن:
- **إدراج** السجلات الجديدة
- **تحديث** الحقول الفارغة فقط على السجلات الموجودة
- **عدم استبدال** أي بيانات موجودة (مثل `announcement_text` المحفوظ مسبقاً)
- **تحديث `updated_at`** على كل تعديل

### مفتاح التفرّد (Unique Key)
يعتمد على القيد الموجود في `apify_cases`:
```
(court, case_number, announcement_date, announcement_type_hint,
 registry_court, registry_type, registry_number)
```

## الإعداد الأولي (تم بالفعل)

### 1. إنشاء RPC في Supabase
تم تطبيق migration: `public.neu_insolvenz_fill_only_upsert(p_records jsonb)`

### 2. متغيرات GitHub Actions Secrets
- `SUPABASE_URL` — رابط مشروع Supabase
- `SUPABASE_SERVICE_ROLE_KEY` — مفتاح service role

### 3. الجدولة
يعمل تلقائياً يومياً الساعة `22:05 UTC` = `00:05 برلين` (بعد منتصف الليل).

## التشغيل اليدوي

من تبويب **Actions** في GitHub:
1. اختر workflow "Daily Insolvency Scrape"
2. اضغط **Run workflow**
3. (اختياري) حدد تاريخ محدد أو حد أقصى للتفاصيل

## التشغيل المحلي للاختبار

```bash
# تثبيت المتطلبات
pip install -r requirements.txt

# اختبار بدون كتابة (Dry Run)
DRY_RUN=1 \
SCRAPE_DATE_FROM=2026-06-15 \
SCRAPE_DATE_TO=2026-06-15 \
MAX_DETAIL_FETCHES=5 \
python scraper.py

# تشغيل حقيقي
SUPABASE_URL="https://YOURPROJECT.supabase.co" \
SUPABASE_SERVICE_ROLE_KEY="..." \
python scraper.py
```

## متغيرات البيئة

| المتغير | الافتراضي | الوصف |
|---------|-----------|--------|
| `SUPABASE_URL` | — | **مطلوب** (إلا في DRY_RUN) |
| `SUPABASE_SERVICE_ROLE_KEY` | — | **مطلوب** (إلا في DRY_RUN) |
| `SCRAPE_DATE_FROM` | اليوم | تاريخ البداية YYYY-MM-DD |
| `SCRAPE_DATE_TO` | اليوم | تاريخ النهاية YYYY-MM-DD |
| `MAX_DETAIL_FETCHES` | 1500 | حد أقصى لجلب التفاصيل في كل تشغيل |
| `DETAIL_WORKERS` | 4 | عدد العمّال المتوازيين |
| `DRY_RUN` | — | `1` للاختبار بدون كتابة |
| `SKIP_DETAILS` | — | `1` لتخطّي مرحلة التفاصيل |

## الأداء المتوقع

- **سحب القائمة:** ~6 ثواني (طلب واحد، حتى 6500 سجل)
- **سحب التفاصيل:** ~1 ثانية/سجل مع 4 عمّال
- **مدة التشغيل اليومي:** ~10-15 دقيقة (للسجلات الجديدة فقط)
- **أيام الذروة (الإثنين):** قد تصل لـ 30-35 دقيقة

## الاستكشاف عند المشاكل

### فشل التشغيل
1. افتح تبويب **Actions** في GitHub
2. راجع log آخر run فاشل
3. سيُفتح Issue تلقائياً مع رابط للـ run

### السجلات لا تظهر في Supabase
تحقق من:
- `SUPABASE_URL` و `SUPABASE_SERVICE_ROLE_KEY` في GitHub Secrets
- صلاحية مفتاح service role (لا انتهت)
- وجود RPC: `select * from pg_proc where proname = 'neu_insolvenz_fill_only_upsert';`

### الموقع لا يستجيب
الموقع قد يكون في صيانة. السكرابر سيعيد المحاولة في اليوم التالي.

## بنية البيانات في Supabase

البيانات تُكتب في جدول `public.apify_cases` مع:
- `source_actor = 'neu_insolvenz_direct'` (لتمييزها عن السكرابرات الأخرى)
- `source_run_id` (معرّف فريد لكل تشغيل)
- `created_at`, `updated_at`, `scraped_at`

## الترخيص والاستخدام

أداة داخلية لـ Swift-Assets. البيانات المسحوبة من مصدر حكومي ألماني عام (insolvenzbekanntmachungen.de).
