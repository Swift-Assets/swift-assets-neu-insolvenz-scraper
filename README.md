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
- مرحلة الإفلاس (`insolvency_phase`) وعلامة قابلية الاستحواذ (`is_pre_verteilung`) — تُستخرَج من النص الكامل وتُكتَب افتراضياً في قاعدة البيانات (العمودان موجودان في `apify_cases`؛ الـ RPC يكتبهما fill-only)
- المحكمة والسجل التجاري (HRA/HRB/PR/VR + رقم)

#### تصنيف المرحلة (`insolvency_phase` / `is_pre_verteilung`)
يُحلَّل النص الكامل للإعلان:
- **قبل التوزيع (قابل للاستحواذ، `is_pre_verteilung = true`):** Eröffnung des Insolvenzverfahrens، vorläufige Insolvenzverwaltung، Bestellung des Insolvenzverwalters، Sicherungsmaßnahmen → القيم: `opening` / `preliminary_administration` / `administrator_appointed`
- **مرحلة متأخرة (غير قابل للاستحواذ، `is_pre_verteilung = false`):** Schlusstermin، Schlussverteilung، Verteilungsverzeichnis، Aufhebung، Einstellung/Abweisung mangels Masse (`dismissed_lack_of_assets`)، Masseunzulänglichkeit (`masseunzulaenglichkeit`)، Restschuldbefreiung
- المطابقة غير حساسة لحالة الأحرف وتتسامح مع تنويعات الـ Umlaut والمسافات؛ وعند الغموض تبقى القيمة `unknown`.

### الكتابة في Supabase
يستخدم RPC مخصص `neu_insolvenz_fill_only_upsert` يضمن:
- **إدراج** السجلات الجديدة
- **تحديث** الحقول الفارغة فقط على السجلات الموجودة
- **عدم استبدال** أي بيانات موجودة (مثل `announcement_text` المحفوظ مسبقاً)
- **تحديث `updated_at`** على كل تعديل

### مفتاح التفرّد (Unique Key)
مفتاح الهوية المستقر المستخدَم في الـ dedup وإعادة الجلب يعتمد على الحقول المعروفة وقت سحب القائمة فقط (يستثني `announcement_type_hint` لأنه فارغ وقت القائمة ويُملأ لاحقاً)، ويطابق منطق الـ RPC `neu_insolvenz_fill_only_upsert`:
```
(court, case_number, announcement_date,
 registry_court, registry_type, registry_number)
```
`announcement_type_hint` يُعامَل كحقل تعبئة فقط (fill-only) داخل الـ RPC، لا كجزء من المفتاح.

### إعادة جلب النص للسجلات الفارغة (Backfill)
بالإضافة إلى السجلات الجديدة، يُعيد السكرابر جلب النص الكامل للسجلات الموجودة مسبقاً التي يكون `announcement_text` فيها فارغاً/NULL:

- **مسار مباشر بالمعرّف (id) مستقل عن نافذة السحب:** يُحدِّد السكرابر السجلات الفارغة مباشرةً من قاعدة البيانات (وليس فقط ضمن نطاق تاريخ السحب الحالي)، **الأقدم أولاً**، ثم يستعيد فهرس الصف عبر بحث ليوم كل سجل لإعادة جلب نصّه. هكذا تصبح السجلات القديمة الواقعة خارج نافذة السحب اليومية قابلة للمعالجة (إصلاح BUG 1).
- **علامة المحاولات (attempt marker):** يحمل العمودان `detail_fetch_attempts` و`last_detail_attempt_at` (موجودان على `apify_cases`) عدد المحاولات. تُزاد المحاولة عند كل محاولة جلب لصفّ موجود (بنجاح أو بنص فارغ). يخرج الصفّ من مجموعة الـ backfill بعد `MAX_DETAIL_ATTEMPTS` محاولة (افتراضياً 3)، فلا تعلق الصفوف التي يُرجِع الخادم لها نصاً فارغاً في حلقة لا نهائية.
- **ميزانية محجوزة للسجلات الجديدة:** لا يأخذ الـ backfill أكثر من نصف `MAX_DETAIL_FETCHES`، فلا يمكن أن يُجوِّع السجلات الجديدة بالكامل. الإجمالي محدود بـ `MAX_DETAIL_FETCHES` (1500/تشغيل) بحيث يتوزّع أي تراكم على عدة أيام.

> ملاحظة: استعادة فهرس الصف تتطلب أن يظل اليوم قابلاً للبحث وأن يظهر السجل في نتائج البوابة؛ السجلات التي لم تَعُد تظهر تبقى في التراكم حتى تتجاوز `MAX_DETAIL_ATTEMPTS` بعد محاولتها.

## الإعداد الأولي (تم بالفعل)

### 1. إنشاء RPC في Supabase
تم تطبيق migration: `public.neu_insolvenz_fill_only_upsert(p_records jsonb)`

### 2. متغيرات GitHub Actions Secrets
- `SUPABASE_URL` — رابط مشروع Supabase
- `SUPABASE_SERVICE_ROLE_KEY` — مفتاح service role
- `SWIFT_ASSETS_GH_PAT` — GitHub Personal Access Token يُستخدم لإرسال `repository_dispatch` عبر المستودعات لتشغيل سير عمل إثراء Handelsregister بعد نجاح السحب والـ backfill. الصلاحية المطلوبة: classic PAT مع نطاق `repo`، أو fine-grained PAT مع صلاحية `Contents: Read & Write` مُخصّصة لتشمل مستودع `swift-assets-handelsregister-scraper`. إذا لم يُضبط هذا السر، يستمر السحب اليومي بالنجاح لكن يُتخطّى إرسال الـ dispatch الخاص بالإثراء.

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
| `MAX_DETAIL_FETCHES` | 1500 | حد أقصى لجلب التفاصيل في كل تشغيل (يُحجَز نصفه للسجلات الجديدة) |
| `MAX_DETAIL_ATTEMPTS` | 3 | عدد محاولات إعادة الجلب لصفّ فارغ قبل إخراجه من الـ backfill (يعتمد على `detail_fetch_attempts`) |
| `DETAIL_WORKERS` | 4 | عدد العمّال المتوازيين |
| `DRY_RUN` | — | `1` للاختبار بدون كتابة |
| `SKIP_DETAILS` | — | `1` لتخطّي مرحلة التفاصيل |
| `WRITE_PHASE_FIELDS` | `1` (مُفعّل) | كتابة عمودَي `insolvency_phase` و`is_pre_verteilung` (موجودان الآن في `apify_cases`). الافتراضي مُفعّل؛ اضبطه على `0` لتعطيله (يبقى الحقلان محسوبَين ويظهران في DRY_RUN) |

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
