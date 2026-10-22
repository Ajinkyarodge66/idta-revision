# Accounts, leaderboard & admin dashboard: setup

The backend is a Google Apps Script attached to a Google Sheet you own. It stores student accounts, saved progress and activity, sends password-reset codes from your Gmail, and powers the leaderboard and admin dashboard.

## First-time setup

1. Go to https://sheets.new, then click **Extensions → Apps Script**.
2. Replace the sample code with everything in [`Code.gs`](Code.gs) and click **Save**.
3. Set your admin password. Paste this at the bottom of the file, put your password in the quotes, choose `setAdminKey` next to **Run**, click **Run**, and then **delete these lines**:
   ```js
   function setAdminKey() { PropertiesService.getScriptProperties().setProperty('ADMIN_KEY', 'your-password'); }
   ```
4. Choose `setup` next to **Run** and click **Run**. Approve the permissions. Gmail permission is needed to email reset codes. The log shows the sheet link and "ADMIN_KEY is set."
5. Click **Deploy → New deployment → ⚙️ Web app**. Set **Execute as: Me** and **Who has access: Anyone**, then click **Deploy** and copy the `/exec` URL.
6. Put the URL in `index.html` as `const SYNC_URL = "…/exec";`.

## Private university packs (`Pack.gs`)

The University of Portsmouth IDTA material (module notes and all 219 module questions, including the 25 official sample-paper questions) is **not in this public repository**. It lives in a second script file that only your Google Script can read, and it's served only to students whose university email is verified.

1. In Apps Script, click **+ (Add a file) → Script** and name it `Pack`.
2. Paste the private `Pack.gs` you were given and click **Save**. Never commit it to GitHub or share it.
3. Students verify by entering their **@myport.ac.uk** (or @port.ac.uk) email in **My university**. They receive a 6-digit code, and once they enter it the pack unlocks.
4. In **Admin → Overview → Students**, the **Verified** column lets you **Grant** or **Revoke** access by hand.

To add another university later, add its email domain in Admin and its pack to `Pack.gs`.

## Exam access and coupon codes

Lessons, Practice, Calc Lab and flashcards are free for everyone, and verified university students always get their module exam free. The **AI & ML course exam** can be locked so students need a code to take it.

- **Admin → Exam & coupons** lets you turn the lock on or off, set the price shown (default £3), and write the "how to pay / get a code" text that logged-in students see on the lock screen.
- **Create a coupon code**: leave the code blank to generate one (e.g. `IDTA-7F3K2A`), or type your own (e.g. `WELCOME`). Choose how many students can use it and when it expires. The note is only for you, e.g. who paid.
- Students enter the code on the **Mock exam** tab, and it unlocks immediately for their account on every device.
- Each code has a **value per use** (e.g. £3, or 0 for a free promo code). To unlock one student directly, use **Paid £3** or **Free** in **Admin → Overview → Students**.
- The **Sales** panel at the top of Exam & coupons totals what you recorded: revenue, this month, paid vs free unlocks, conversion and a 30-day chart. It only counts what you record, so check it against your bank or payment app.

**Payment screenshots.** On the lock screen, a student can upload a screenshot of their payment (with an optional transaction ID). You get an email, and it appears at the top of **Admin → Exam & coupons → Payments to check** (the tab shows a red count, refreshed every 30 seconds). Click **View screenshot**, then **Approve & unlock** (records the sale) or **Reject** with a reason. The student's page unlocks by itself within about 20 seconds, and they get an email either way. Screenshots are saved in a private Drive folder, *IDTA Revision payment screenshots (private)*; the `setup` log prints its link. Emails go to your Google account; to use another address, add a script property `NOTIFY_EMAIL`.

**Refunds.** Students see the policy before paying (they must tick to agree): a full refund if they cancel **before starting their first course exam**; after that, the payment is non-refundable. Eligible students get a **Cancel and request a refund** button in **My account**, enter where to send the money, and their exam access ends immediately. You get an email, and the request appears in **Exam & coupons → Refund requests**. Send the money yourself, then click **Mark refunded** (the student is emailed). **Decline** gives their access back.

**Refer & earn.** Every student has a personal link in the **Refer & earn** tab. They earn **£1** for each friend who signs up with it, unlocks the course exams (paid, not refunded) and studies on **10 different days**. They request payouts in the app; you get an email, pay them yourself, then click **Mark paid** under **Referrals**. The amounts are `REF_REWARD` and `REF_DAYS` at the top of `Code.gs`. The Sales panel shows net income after refunds and referral rewards.

Codes and who used them are stored in the **Coupons** sheet. The lock is on by default once this version of `Code.gs` is deployed.

## Exam camera photos

Every exam (course exams, papers, topic tests, quick mocks and the Portsmouth module mock) needs the camera. Before an exam, students see what's photographed, why, who sees it and that photos are deleted after 30 days, and they tick to agree. Their preview and a red "Camera on" dot stay visible throughout. Without a working camera the exam won't start, and the screen says how to fix it. If the camera turns off mid-exam, the exam pauses until it's back on; the timer keeps running.

Photos are taken at the start, every 5 minutes, when the student comes back from another tab, and at the end. They're saved as small JPEGs in a private Drive folder, *IDTA Revision exam photos (private, auto-deleted)*, which the `setup` log links to. To see them, open **Admin → Overview → Students**, click a name, then **Exam photos → View photos**. Each exam shows its score and how many times the student left the tab.

Photos older than `PHOTO_DAYS` (30) are deleted automatically a few times a day. You can also run `cleanPhotos` from the editor. Deleting an account deletes that student's photos.

## Two websites, one backend: IDTA Revision and JobReady AI

Both websites use this same script and Google Sheet, so accounts, progress, payments and Admin are shared.
- **IDTA Revision** (`/idta-revision/`): the UK edition, with University of Portsmouth packs.
- **JobReady AI** (`/jobready-ai/`): the international edition, with no university packs, its own branding, and a **Career hub** (12-week roadmap, 5 portfolio projects, 35 interview answers, cheat sheets).

Students choose their **country** at sign-up, which also sets the phone code. Someone outside the UK who signs up on IDTA Revision is taken straight to JobReady AI, already logged in. Existing students see a banner suggesting the other edition, unless they're verified for a university pack.

**Prices by country** (Admin → Exam & coupons): each region has a currency, an amount, the label students see (e.g. ₹199) and optional "how to pay" text, for example your UPI ID for India. Defaults: UK £3 · India ₹199 · Pakistan Rs 499 · Bangladesh ৳249 · Nigeria ₦1,500 · UAE AED 12 · USA/Canada $3.99 · Europe €3.49 · everyone else $2.99. Payments are recorded in their own currency. Sales totals are converted to pounds at approximate rates (`TO_GBP` in `Code.gs`). Analytics can be filtered by website and shows sign-ups by country.

## JobReady AI levels and capstone

JobReady AI has three levels: **Foundation** (the 26 core lessons), **Professional** (linear algebra, probability and statistics, A/B testing, training deep networks, computer vision, transformers) and **Advanced** (LLM internals, fine-tuning and RAG evaluation, agents and safety, data pipelines and SQL, deployment and monitoring, ML system design). That's 38 lessons and 52 extra questions. Foundation certificates come from Papers 1–3 or the random exam; the **Professional exam** and **Advanced exam** (30 questions, 60 minutes) give their own certificates at 70%+.

**Capstone projects:** students submit a title, a public GitHub link, an optional demo link and a summary in **Career hub → Capstone**. You get an email. Mark it out of 100 in **Admin → Capstones** using the rubric (framing 15, data 20, modelling & evaluation 25, deployment 15, communication 15, responsible AI 10) and add feedback. A mark of 50+ issues a verifiable certificate automatically (Merit 60+, Distinction 70+), and the student is emailed either way. Submissions are stored in the **Capstones** sheet.

## Analytics and student tracker

**Admin → Analytics** shows, for the last 7, 30 or 90 days or 12 months:
- **Sign-up funnel:** visitors → signed up → studied → unlocked exams → took an exam → got a certificate, with the conversion at each step.
- **Daily charts** for visitors, sign-ups, active students and exam unlocks.
- **Where students come from:** visitors, sign-ups, conversion, unlocks and revenue per source. WhatsApp hides where visitors came from, so make **tagged links** (`…/?src=msc-ai-whatsapp`) with the builder and use a different one for each group or post.
- **Engagement:** active today, this week and this month, average study days, estimated study time, and phone vs laptop.
- **Retention:** the share of students who studied again 1, 7 and 30 days after signing up.

In **Admin → Overview → Students**, click a name to open their **tracker**: source, device, who invited them, study days, a 30-day activity strip, accuracy by topic, a day-by-day timeline (questions, exams, Calc Lab, payments and refunds), certificates and referrals.

Visits are counted once per browser session with no cookies, IDs or IP addresses (the **Visits** sheet). Your own Admin visits aren't counted.

## Updating the script (keeps the same URL)

1. Paste the new `Code.gs` and click **Save**.
2. Run `setup` once. This is needed whenever the new version asks for new permissions.
3. Click **Deploy → Manage deployments → ✏️ (edit) → Version: New version → Deploy**.

Don't use "New deployment" for updates: it creates a new URL, and the app would need changing.

## Sheets it creates

| Sheet | Contents |
|---|---|
| **Users** | ID, created, name, mobile, email, university, course, password hash + salt (never the password), leaderboard choice, saved progress, last login, reset code hash, consent time, university email, verified university, verification code hash, exam access (how it was unlocked), when they first started a course exam (ends the refund window), who referred them, sign-up source and device, which website they signed up on, country |
| **Sessions** | Login tokens, stored only as hashes, with a 30-day expiry |
| **Events** | One row per answer, mock or Calc Lab attempt, plus `unlock`/`revoke` rows for exam access (with the amount) |
| **Announcements** | Banners you post from the admin page (title, message, style, expiry) |
| **Questions** | Questions you add from the admin page (IDs from 1000 up) |
| **Coupons** | Exam coupon codes: code, max uses, times used, expiry, your note, which accounts used it, and value per use |
| **Payments** | Uploaded payment screenshots: ID, time, student, mobile, amount, reference, Drive file ID, status (pending/approved/rejected), decision time and reason |
| **Refunds** | Refund requests: student, amount, where to send it, reason, status (pending/refunded/declined) |
| **Referral payouts** | Referral reward requests: student, amount, where to send it, status (pending/paid/declined) |
| **Exam photos** | One row per photo: time, student, exam session, exam name, reason (start, timer, tab, resume, end), Drive file ID |
| **Visits** | One anonymous row per browser session: time, source, device type, logged in or not, returning or not |
| **Certificates** | Certificates students claimed: ID, date, user, name, score and exam. Anyone can check one at `…/#cert=ID` |

Hidden built-in questions are saved in the `HIDDEN_Q` script property.

Optional: in Apps Script, go to **Triggers → Add trigger → `cleanSessions`, time-driven, daily** to tidy expired logins.

## Security notes

- Passwords are stretched in the browser (PBKDF2, 150,000 rounds) and hashed again on the server with a per-user salt and a secret `PEPPER`. Nobody, including you, can read a password from the sheet.
- 5 wrong logins lock that number for 15 minutes. Reset codes expire after 15 minutes, and 5 wrong codes trigger a lockout.
- Text is sanitised so nobody can inject spreadsheet formulas.
- **The Users sheet holds personal data** (mobile numbers and emails). Don't share the sheet, don't share the admin key, and delete the data when the module ends. Students can delete their own account at any time.
- Gmail allows about 100 emails a day on a free account, which is plenty for reset codes.
